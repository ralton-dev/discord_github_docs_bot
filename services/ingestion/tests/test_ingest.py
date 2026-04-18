"""Unit tests for `services/ingestion/ingest.py`.

Covers pure helpers: `_auth_url`, `iter_chunks`, `splitter_for`.
Tests are hermetic — no network, no DB, no git clone. All filesystem work
happens under `tmp_path`.
"""

from __future__ import annotations

import pathlib

import pytest
from langchain_text_splitters import RecursiveCharacterTextSplitter

import ingest


# ---------------------------------------------------------------------------
# _git_auth_env — token must ride in an Authorization header, NOT in the URL
# ---------------------------------------------------------------------------

class TestGitAuthEnv:
    def test_empty_token_returns_env_unchanged(self) -> None:
        base = {"PATH": "/usr/bin", "HOME": "/tmp"}
        got = ingest._git_auth_env(base, "")
        assert got == base
        # No GIT_CONFIG_* keys get added when there's no token.
        assert "GIT_CONFIG_COUNT" not in got
        assert "GIT_CONFIG_KEY_0" not in got
        assert "GIT_CONFIG_VALUE_0" not in got

    def test_token_sets_extraheader_via_git_config_env(self) -> None:
        base = {"PATH": "/usr/bin"}
        got = ingest._git_auth_env(base, "ghp_abc123")
        assert got["GIT_CONFIG_COUNT"] == "1"
        assert got["GIT_CONFIG_KEY_0"] == "http.extraheader"
        value = got["GIT_CONFIG_VALUE_0"]
        assert value.startswith("Authorization: Basic ")
        # Decode the base64 payload and check it round-trips to
        # "x-access-token:<token>".
        import base64 as _b64

        encoded = value.removeprefix("Authorization: Basic ")
        decoded = _b64.b64decode(encoded).decode("utf-8")
        assert decoded == "x-access-token:ghp_abc123"

    def test_base_env_is_not_mutated(self) -> None:
        base = {"PATH": "/usr/bin"}
        _ = ingest._git_auth_env(base, "tok")
        # Returned env has the keys; caller's env does not.
        assert "GIT_CONFIG_COUNT" not in base

    def test_token_does_not_appear_in_argv_or_url(self) -> None:
        # Sentinel check: whatever _git_auth_env produces, the token value
        # itself only ends up inside the base64-encoded header. Grep the
        # rendered string form.
        got = ingest._git_auth_env({}, "super-secret-token-xyz")
        # The raw token must NOT appear as-is in any value.
        for v in got.values():
            assert "super-secret-token-xyz" not in v


# ---------------------------------------------------------------------------
# _scrub_token — belt-and-braces redaction of accidental token leakage
# ---------------------------------------------------------------------------

class TestScrubToken:
    def test_empty_token_passthrough(self) -> None:
        assert ingest._scrub_token("hello ghp_abc", "") == "hello ghp_abc"

    def test_empty_text_passthrough(self) -> None:
        assert ingest._scrub_token("", "ghp_abc") == ""

    def test_token_substring_redacted(self) -> None:
        text = "fatal: auth failed at https://x-access-token:ghp_abc@github.com/..."
        assert ingest._scrub_token(text, "ghp_abc") == (
            "fatal: auth failed at https://x-access-token:<redacted>@github.com/..."
        )

    def test_multiple_occurrences_all_redacted(self) -> None:
        text = "tok=abc123 tok_again=abc123"
        assert ingest._scrub_token(text, "abc123") == (
            "tok=<redacted> tok_again=<redacted>"
        )


# ---------------------------------------------------------------------------
# iter_chunks
# ---------------------------------------------------------------------------

def _write(root: pathlib.Path, rel: str, content: str) -> pathlib.Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestIterChunks:
    def test_skips_files_in_skip_dirs(self, tmp_path: pathlib.Path) -> None:
        # One file inside each skip dir + one we expect to be picked up.
        _write(tmp_path, ".git/config", "keep out")
        _write(tmp_path, "node_modules/lib/index.js", "module.exports = {};")
        _write(tmp_path, "__pycache__/foo.py", "print('x')")
        _write(tmp_path, "src/app.py", "print('hi')")

        rels = {row[0] for row in ingest.iter_chunks(tmp_path)}
        # Only src/app.py should survive.
        assert rels == {"src/app.py"}

    def test_skips_unsupported_extensions(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "image.png", "binary-ish")
        _write(tmp_path, "archive.zip", "zipdata")
        _write(tmp_path, "notes.md", "# notes")
        _write(tmp_path, "script.py", "print('ok')")

        rels = {row[0] for row in ingest.iter_chunks(tmp_path)}
        assert rels == {"notes.md", "script.py"}

    def test_skips_files_larger_than_max_file_bytes(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shrink the cap so we don't have to materialise a 500 KB file.
        monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 100)
        _write(tmp_path, "small.py", "print('x')")
        _write(tmp_path, "big.py", "x = '" + ("a" * 500) + "'\n")  # >100 bytes

        rels = {row[0] for row in ingest.iter_chunks(tmp_path)}
        assert rels == {"small.py"}

    def test_emits_expected_tuple_shape(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path, "app.py", "print('hello world')\n")
        rows = list(ingest.iter_chunks(tmp_path))
        assert rows, "expected at least one chunk row"
        for row in rows:
            assert isinstance(row, tuple)
            assert len(row) == 5
            rel, idx, chunk, ctype, language = row
            assert isinstance(rel, str)
            assert isinstance(idx, int)
            assert isinstance(chunk, str)
            assert ctype in {"code", "markdown"}
            assert language is None or isinstance(language, str)

    def test_python_file_classified_as_code_python(
        self, tmp_path: pathlib.Path
    ) -> None:
        _write(tmp_path, "module.py", "def f():\n    return 42\n")
        rows = list(ingest.iter_chunks(tmp_path))
        assert rows, "expected at least one chunk for module.py"
        rel, idx, _chunk, ctype, language = rows[0]
        assert rel == "module.py"
        assert idx == 0
        assert ctype == "code"
        assert language == "py"

    def test_markdown_file_classified_as_markdown_no_language(
        self, tmp_path: pathlib.Path
    ) -> None:
        _write(tmp_path, "README.md", "# Hello\n\nSome prose.\n")
        rows = list(ingest.iter_chunks(tmp_path))
        assert rows, "expected at least one chunk for README.md"
        rel, _idx, _chunk, ctype, language = rows[0]
        assert rel == "README.md"
        assert ctype == "markdown"
        assert language is None

    def test_chunk_indexes_are_sequential_from_zero(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Long-ish markdown to force multiple chunks via the generic splitter.
        long_text = ("paragraph line with enough text. " * 200) + "\n"
        _write(tmp_path, "long.md", long_text)
        rows = [r for r in ingest.iter_chunks(tmp_path) if r[0] == "long.md"]
        indices = [r[1] for r in rows]
        assert indices == list(range(len(indices)))


# ---------------------------------------------------------------------------
# splitter_for
# ---------------------------------------------------------------------------

class TestSplitterFor:
    def test_known_code_extension_returns_language_aware_splitter(self) -> None:
        sp = ingest.splitter_for(pathlib.Path("foo.py"))
        assert isinstance(sp, RecursiveCharacterTextSplitter)
        # A language-aware python splitter uses python-specific separators;
        # the generic fallback does not. Compare to the generic splitter.
        generic = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
        assert sp._separators != generic._separators

    def test_unknown_extension_returns_generic_splitter(self) -> None:
        sp = ingest.splitter_for(pathlib.Path("notes.md"))
        assert isinstance(sp, RecursiveCharacterTextSplitter)
        generic = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
        # Generic fallback shares the default separators list.
        assert sp._separators == generic._separators

    def test_every_mapped_extension_returns_language_aware_splitter(self) -> None:
        generic = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
        for ext in ingest.EXT_LANG:
            sp = ingest.splitter_for(pathlib.Path(f"file{ext}"))
            assert isinstance(sp, RecursiveCharacterTextSplitter)
            assert sp._separators != generic._separators, (
                f"expected language-aware splitter for {ext}"
            )
