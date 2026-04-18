"""Webhook helpers for the rag service.

Two independent pieces kept out of ``app.py`` so they can be unit-tested in
isolation:

- :func:`verify_signature` — validates inbound webhook authenticity. GitHub
  signs the raw request body with HMAC-SHA256 under a shared secret and puts
  the hex digest in ``X-Hub-Signature-256``; GitLab instead sends the shared
  secret verbatim in ``X-Gitlab-Token``. Both are constant-time-compared.
- :class:`RateLimiter` — an in-process per-key token bucket with a single
  token and a configurable refill interval. Used to cap webhook-driven
  ingestion at one run per repo per ``interval_secs`` seconds, matching the
  "prevent storms" intent in plan 15.

Both helpers accept a ``clock`` callable so tests can drive time
deterministically without sleeping.

Caveat: the rate-limiter is **in-process**. It does not persist across rag
pod restarts nor coordinate across replicas. For the homelab-scale MVP this
is acceptable — with ``ragOrchestrator.replicas=2``, the worst case is two
bursts within the window, still bounded. Flagged in docs/webhooks.md as a
future-hardening item (likely a Redis-backed bucket or a persistent
Kubernetes lease).
"""

from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class SignatureError(ValueError):
    """Raised when a webhook request fails authentication."""


def verify_signature(
    *,
    provider: str,
    secret: str,
    body: bytes,
    signature_header: str | None,
) -> None:
    """Validate a webhook signature. Raises :class:`SignatureError` on mismatch.

    Parameters
    ----------
    provider:
        Either ``"github"`` or ``"gitlab"``. Any other value raises.
    secret:
        The per-instance shared secret (``WEBHOOK_SECRET`` env var). Empty
        string is treated as misconfiguration — every request is rejected
        so a missing secret cannot accidentally open the endpoint.
    body:
        Raw request body bytes (must match exactly what the sender signed).
    signature_header:
        Value of the provider-specific signature header. For GitHub this is
        ``X-Hub-Signature-256`` formatted ``sha256=<hex>``. For GitLab it is
        ``X-Gitlab-Token`` — the secret is sent verbatim.
    """
    if not secret:
        # Be explicit: a misconfigured deployment must never silently
        # accept unauthenticated webhooks.
        raise SignatureError("WEBHOOK_SECRET is not configured on this instance")

    if signature_header is None or signature_header == "":
        raise SignatureError("missing signature header")

    if provider == "github":
        # Format is "sha256=<hex>"; reject anything else so we don't fall
        # back to a weaker scheme by accident.
        if not signature_header.startswith("sha256="):
            raise SignatureError("invalid signature format")
        provided = signature_header.removeprefix("sha256=")
        expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            raise SignatureError("signature mismatch")
        return

    if provider == "gitlab":
        # GitLab ships the secret verbatim (no HMAC). compare_digest keeps
        # the check constant-time even though that's mostly belt-and-braces
        # for a plain-string match.
        if not hmac.compare_digest(signature_header, secret):
            raise SignatureError("signature mismatch")
        return

    raise SignatureError(f"unsupported provider: {provider}")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """Single-token bucket; next allowed call is at ``next_at``."""
    next_at: float


class RateLimiter:
    """Per-key token bucket with one token every ``interval_secs`` seconds.

    Thread-safe — :class:`RateLimiter` is shared across FastAPI request
    handlers (which run in a threadpool for sync endpoints) so mutations to
    the internal dict are guarded by a lock. The lock is held only for the
    microseconds needed to read/update a single entry.

    Parameters
    ----------
    interval_secs:
        Minimum seconds between allowed calls for the same key. Plan 15
        specifies 60s; exposed as a parameter so tests can use a tiny
        interval + a controllable clock.
    clock:
        Callable returning current time in seconds. Defaults to
        :func:`time.monotonic` in production; tests inject a mutable stub.
    """

    def __init__(
        self,
        *,
        interval_secs: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval_secs
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Atomically test-and-consume a token for ``key``.

        Returns
        -------
        (allowed, retry_after_secs)
            ``allowed`` is True iff the call is permitted (and the next call
            is booked for ``now + interval``). On False, ``retry_after_secs``
            is the number of seconds the caller should wait before retrying
            (rounded up via ``math.ceil`` only at the presentation layer —
            here it's a float so the caller can format as needed).
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now >= bucket.next_at:
                self._buckets[key] = _Bucket(next_at=now + self._interval)
                return True, 0.0
            return False, max(0.0, bucket.next_at - now)
