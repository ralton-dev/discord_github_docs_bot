"""Unit tests for `services/rag/webhook.py`.

Covers signature verification (GitHub HMAC + GitLab plain-token) and the
per-key token-bucket rate limiter. Both use injectable clocks so tests are
deterministic with no real time passage.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest

from webhook import RateLimiter, SignatureError, verify_signature


SECRET = "topsecret"
BODY = b'{"ref":"refs/heads/main"}'


def _github_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


class TestVerifySignatureGithub:
    def test_valid_hmac_accepted(self) -> None:
        verify_signature(
            provider="github",
            secret=SECRET,
            body=BODY,
            signature_header=_github_sig(SECRET, BODY),
        )

    def test_wrong_hmac_rejected(self) -> None:
        with pytest.raises(SignatureError, match="signature mismatch"):
            verify_signature(
                provider="github",
                secret=SECRET,
                body=BODY,
                signature_header="sha256=" + "0" * 64,
            )

    def test_missing_sha256_prefix_rejected(self) -> None:
        with pytest.raises(SignatureError, match="invalid signature format"):
            verify_signature(
                provider="github",
                secret=SECRET,
                body=BODY,
                signature_header="deadbeef",
            )

    def test_missing_header_rejected(self) -> None:
        with pytest.raises(SignatureError, match="missing signature header"):
            verify_signature(
                provider="github",
                secret=SECRET,
                body=BODY,
                signature_header=None,
            )

    def test_body_tampering_rejected(self) -> None:
        sig = _github_sig(SECRET, BODY)
        with pytest.raises(SignatureError, match="signature mismatch"):
            verify_signature(
                provider="github",
                secret=SECRET,
                body=BODY + b"X",
                signature_header=sig,
            )


class TestVerifySignatureGitlab:
    def test_valid_token_accepted(self) -> None:
        verify_signature(
            provider="gitlab",
            secret=SECRET,
            body=BODY,
            signature_header=SECRET,
        )

    def test_wrong_token_rejected(self) -> None:
        with pytest.raises(SignatureError, match="signature mismatch"):
            verify_signature(
                provider="gitlab",
                secret=SECRET,
                body=BODY,
                signature_header="other",
            )

    def test_empty_header_rejected(self) -> None:
        with pytest.raises(SignatureError, match="missing signature header"):
            verify_signature(
                provider="gitlab",
                secret=SECRET,
                body=BODY,
                signature_header="",
            )


class TestVerifySignatureMisconfigured:
    def test_empty_secret_rejects_everything(self) -> None:
        with pytest.raises(SignatureError, match="WEBHOOK_SECRET is not configured"):
            verify_signature(
                provider="github",
                secret="",
                body=BODY,
                signature_header=_github_sig("anything", BODY),
            )

    def test_unsupported_provider_rejected(self) -> None:
        with pytest.raises(SignatureError, match="unsupported provider"):
            verify_signature(
                provider="bitbucket",
                secret=SECRET,
                body=BODY,
                signature_header="sha256=abc",
            )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestRateLimiter:
    def test_first_call_allowed(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(interval_secs=60.0, clock=clock)
        allowed, retry = limiter.check("repo-a")
        assert allowed is True
        assert retry == 0.0

    def test_second_call_within_interval_blocked(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(interval_secs=60.0, clock=clock)
        limiter.check("repo-a")
        clock.now += 10.0
        allowed, retry = limiter.check("repo-a")
        assert allowed is False
        assert retry == pytest.approx(50.0)

    def test_call_after_interval_allowed(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(interval_secs=60.0, clock=clock)
        limiter.check("repo-a")
        clock.now += 60.0
        allowed, _ = limiter.check("repo-a")
        assert allowed is True

    def test_different_keys_independent(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(interval_secs=60.0, clock=clock)
        assert limiter.check("repo-a")[0] is True
        assert limiter.check("repo-b")[0] is True
        # Same keys blocked.
        assert limiter.check("repo-a")[0] is False
        assert limiter.check("repo-b")[0] is False
