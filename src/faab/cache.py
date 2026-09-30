"""Atomic writes and a TTL disk cache, shared by every collector.

Two properties matter here and both are enforced by tests in `tests/test_cache.py`,
which fail when the mechanism is removed.

Atomicity: a write goes to a temporary file in the same directory and is moved into
place with `os.replace`, so a reader either sees the previous content or the new
content, never a partial file, and a failed write leaves the previous content intact.

Freshness: a negative age, which a future mtime produces under clock skew, does not
count as fresh. Otherwise a stale artifact pins itself past its TTL.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

DEFAULT_TIMEOUT_SECONDS = 60
GZIP_MAGIC = b"\x1f\x8b"


class FetchError(RuntimeError):
    """Raised when a remote fetch returns something unusable."""


def write_bytes_atomic(target: Path, payload: bytes) -> None:
    """Replace `target` with `payload`, never leaving a partial file behind."""
    _write_atomic(target, payload, lambda data, fp: fp.write(data), text_mode=False)


def write_json_atomic(target: Path, payload: Any) -> None:
    """Replace `target` with `payload` serialized as JSON."""
    _write_atomic(target, payload, json.dump, text_mode=True)



def _write_atomic(target: Path, payload: Any, serialize: Callable[[Any, Any], None], text_mode: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w" if text_mode else "wb") as tmp_file:
            serialize(payload, tmp_file)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def is_fresh(path: Path, max_age_seconds: int) -> bool:
    """True when `path` exists and its mtime is within `max_age_seconds`."""
    if max_age_seconds <= 0:
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < max_age_seconds


def fetch_text_cached(
    url: str,
    cache_path: Path,
    max_age_seconds: int,
    min_bytes: int = 1,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Return the body of `url` as UTF-8 text, cached on disk under `cache_path`.

    A gzipped body is decompressed before caching, detected by its magic bytes rather
    than by the URL suffix, so a server that applies its own content encoding cannot
    cause a double decompression. The cache therefore always holds plain text.

    A body smaller than `min_bytes` is refused as truncated and never cached, because
    a short file here silently shrinks a candidate list rather than failing.
    """
    if is_fresh(cache_path, max_age_seconds):
        try:
            cached = cache_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            cached = ""
        if len(cached.encode("utf-8")) >= min_bytes:
            return cached

    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise FetchError(f"{url}: request failed: {exc}") from exc
    if response.status_code != 200:
        raise FetchError(f"{url}: HTTP {response.status_code}")

    body = response.content
    if body[:2] == GZIP_MAGIC:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise FetchError(f"{url}: gzip body would not decompress: {exc}") from exc
    elif url.endswith(".gz"):
        raise FetchError(
            f"{url}: URL ends in .gz but the body carries no gzip magic bytes"
        )

    if len(body) < min_bytes:
        raise FetchError(
            f"{url}: {len(body)} bytes, expected at least {min_bytes}; "
            "treating as truncated"
        )

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FetchError(f"{url}: body was not UTF-8: {exc}") from exc

    write_bytes_atomic(cache_path, text.encode("utf-8"))
    return text
