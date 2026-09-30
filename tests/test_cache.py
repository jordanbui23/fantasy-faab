"""Tests for the shared atomic-write and TTL cache layer.

The atomicity and freshness tests here are the enforcement for the claims in
`cache.py`'s docstring. Each fails when the mechanism it describes is removed,
which is verified by hand rather than asserted in prose.

Network calls are stubbed. No test in this file touches the network.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import cache  # noqa: E402


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


# --- atomicity -----------------------------------------------------------------


def test_json_write_preserves_old_target_on_failure(tmp_path, monkeypatch):
    """A failed write must leave the PREVIOUS content intact."""
    target = tmp_path / "out.json"
    target.write_text('{"generation": 1}')

    def explode(payload, fp):
        fp.write('{"generation": 2, "trunc')
        raise RuntimeError("serialization blew up")

    monkeypatch.setattr(cache.json, "dump", explode)
    with pytest.raises(RuntimeError, match="serialization blew up"):
        cache.write_json_atomic(target, {"generation": 2})

    assert json.loads(target.read_text()) == {"generation": 1}
    assert list(tmp_path.glob("*.tmp")) == [], "temp file was left behind"


def test_json_write_target_never_holds_partial_content(tmp_path, monkeypatch):
    """Mid-write a reader must see the old file, never a half-written one."""
    target = tmp_path / "out.json"
    target.write_text('{"generation": 1}')
    observed = []
    real_dump = cache.json.dump

    def watching_dump(payload, fp):
        result = real_dump(payload, fp)
        fp.flush()
        observed.append(target.read_text())
        return result

    monkeypatch.setattr(cache.json, "dump", watching_dump)
    cache.write_json_atomic(target, {"generation": 2})

    assert observed == ['{"generation": 1}'], "target changed before the write finished"
    assert json.loads(target.read_text()) == {"generation": 2}


def test_bytes_write_preserves_old_target_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    target.write_bytes(b"old,content\n")

    real_replace = cache.os.replace

    def failing_replace(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(cache.os, "replace", failing_replace)
    with pytest.raises(OSError, match="rename failed"):
        cache.write_bytes_atomic(target, b"new,content\n")

    monkeypatch.setattr(cache.os, "replace", real_replace)
    assert target.read_bytes() == b"old,content\n"
    assert list(tmp_path.glob("*.tmp")) == [], "temp file was left behind"


def test_bytes_write_round_trips(tmp_path):
    target = tmp_path / "a" / "b" / "out.csv"
    cache.write_bytes_atomic(target, b"week,player\n3,Somebody\n")
    assert target.read_bytes() == b"week,player\n3,Somebody\n"
    assert list(target.parent.glob("*.tmp")) == []


def test_writes_create_missing_parent_dirs(tmp_path):
    cache.write_json_atomic(tmp_path / "x" / "y" / "f.json", {"ok": True})
    assert json.loads((tmp_path / "x" / "y" / "f.json").read_text()) == {"ok": True}


# --- freshness -----------------------------------------------------------------


def test_missing_file_is_not_fresh(tmp_path):
    assert not cache.is_fresh(tmp_path / "absent", 3600)


def test_zero_or_negative_ttl_is_never_fresh(tmp_path):
    path = tmp_path / "f"
    path.write_text("x")
    assert not cache.is_fresh(path, 0)
    assert not cache.is_fresh(path, -1)


def test_recent_file_is_fresh(tmp_path):
    path = tmp_path / "f"
    path.write_text("x")
    assert cache.is_fresh(path, 3600)


def test_old_file_is_not_fresh(tmp_path):
    path = tmp_path / "f"
    path.write_text("x")
    stale = cache.time.time() - 7200
    os.utime(path, (stale, stale))
    assert not cache.is_fresh(path, 3600)


def test_future_mtime_is_not_fresh(tmp_path):
    """Clock skew must not pin a stale artifact past its TTL."""
    path = tmp_path / "f"
    path.write_text("x")
    future = cache.time.time() + 86_400
    os.utime(path, (future, future))
    assert not cache.is_fresh(path, 3600)


# --- fetch_text_cached ---------------------------------------------------------


def test_fetch_writes_then_reuses_cache(tmp_path, monkeypatch):
    calls = []
    body = b"week,player\n3,Somebody\n" * 50

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResponse(body)

    monkeypatch.setattr(cache.requests, "get", fake_get)
    target = tmp_path / "nested" / "f.csv"

    first = cache.fetch_text_cached("https://x/f.csv", target, 3600, min_bytes=10)
    assert first == body.decode()
    assert target.read_bytes() == body

    second = cache.fetch_text_cached("https://x/f.csv", target, 3600, min_bytes=10)
    assert second == first
    assert len(calls) == 1, "a fresh cache must not trigger a second request"


def test_fetch_decompresses_a_gz_url(tmp_path, monkeypatch):
    plain = b"week,player\n3,Somebody\n" * 50
    monkeypatch.setattr(
        cache.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(gzip.compress(plain)),
    )
    target = tmp_path / "f.csv"

    text = cache.fetch_text_cached("https://x/f.csv.gz", target, 3600, min_bytes=10)
    assert text == plain.decode()
    assert target.read_bytes() == plain, "the cache must hold plain text, not gzip"


def test_fetch_rejects_a_gz_url_whose_body_is_not_gzip(tmp_path, monkeypatch):
    """A .gz URL serving plain bytes means something intercepted the response."""
    monkeypatch.setattr(
        cache.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(b"this is not gzip at all" * 50),
    )
    with pytest.raises(cache.FetchError, match="no gzip magic bytes"):
        cache.fetch_text_cached("https://x/f.csv.gz", tmp_path / "f.csv", 3600)


def test_fetch_rejects_a_corrupt_gzip_body(tmp_path, monkeypatch):
    """Magic bytes present but the stream is broken."""
    monkeypatch.setattr(
        cache.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(cache.GZIP_MAGIC + b"\x08garbage" * 50),
    )
    with pytest.raises(cache.FetchError, match="would not decompress"):
        cache.fetch_text_cached("https://x/f.csv.gz", tmp_path / "f.csv", 3600)


def test_fetch_decompresses_by_magic_bytes_not_url_suffix(tmp_path, monkeypatch):
    """A server that already decoded the encoding must not be decompressed twice."""
    plain = b"week,player\n3,Somebody\n" * 50
    monkeypatch.setattr(
        cache.requests, "get", lambda url, timeout=None: _FakeResponse(gzip.compress(plain))
    )
    text = cache.fetch_text_cached(
        "https://x/f.csv", tmp_path / "f.csv", 3600, min_bytes=10
    )
    assert text == plain.decode(), "a gzip body on a plain URL must still decompress"


def test_fetch_rejects_non_200(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cache.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(b"nope", status_code=502),
    )
    with pytest.raises(cache.FetchError, match="HTTP 502"):
        cache.fetch_text_cached("https://x/f.csv", tmp_path / "f.csv", 3600)


def test_fetch_wraps_request_exception(tmp_path, monkeypatch):
    def boom(url, timeout=None):
        raise cache.requests.Timeout("too slow")

    monkeypatch.setattr(cache.requests, "get", boom)
    with pytest.raises(cache.FetchError, match="request failed"):
        cache.fetch_text_cached("https://x/f.csv", tmp_path / "f.csv", 3600)


def test_fetch_rejects_a_short_body(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cache.requests, "get", lambda url, timeout=None: _FakeResponse(b"week,player\n")
    )
    with pytest.raises(cache.FetchError, match="treating as truncated"):
        cache.fetch_text_cached(
            "https://x/f.csv", tmp_path / "f.csv", 3600, min_bytes=100_000
        )


def test_fetch_does_not_cache_a_rejected_body(tmp_path, monkeypatch):
    target = tmp_path / "f.csv"
    monkeypatch.setattr(
        cache.requests, "get", lambda url, timeout=None: _FakeResponse(b"tiny")
    )
    with pytest.raises(cache.FetchError):
        cache.fetch_text_cached("https://x/f.csv", target, 3600, min_bytes=1000)
    assert not target.exists(), "a rejected body must never reach the cache"


def test_fetch_refetches_a_short_cache(tmp_path, monkeypatch):
    """A cache below the floor is discarded, not served."""
    body = b"week,player\n3,Somebody\n" * 50
    monkeypatch.setattr(
        cache.requests, "get", lambda url, timeout=None: _FakeResponse(body)
    )
    target = tmp_path / "f.csv"
    target.write_text("truncated")

    assert cache.fetch_text_cached("https://x/f.csv", target, 3600, min_bytes=100) == body.decode()


def test_fetch_passes_the_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["timeout"] = timeout
        return _FakeResponse(b"week,player\n" * 50)

    monkeypatch.setattr(cache.requests, "get", fake_get)
    cache.fetch_text_cached("https://x/f.csv", tmp_path / "f.csv", 3600, min_bytes=10)
    assert seen["timeout"] == cache.DEFAULT_TIMEOUT_SECONDS


def test_fetch_rejects_non_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cache.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(b"\xff\xfe invalid " * 50),
    )
    with pytest.raises(cache.FetchError, match="not UTF-8"):
        cache.fetch_text_cached("https://x/f.csv", tmp_path / "f.csv", 3600, min_bytes=10)
