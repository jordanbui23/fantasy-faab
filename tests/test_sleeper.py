"""Tests for the Sleeper collector.

Network calls are stubbed. Two tests hit the live API and are skipped unless
FAAB_LIVE_TESTS=1, so the suite stays offline by default.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import cache as cache_mod  # noqa: E402
from faab.collectors import sleeper  # noqa: E402


class _FakeResponse:
    def __init__(self, payload, status_code=200, valid_json=True):
        self._payload = payload
        self.status_code = status_code
        self._valid_json = valid_json

    def json(self):
        if not self._valid_json:
            raise ValueError("not json")
        return self._payload


def _plausible_dump(**overrides):
    """A player dump large enough to clear the truncation floor."""
    dump = {
        str(i): {"full_name": f"Filler {i}", "position": "WR", "team": "SEA"}
        for i in range(sleeper.MIN_PLAYER_DUMP_SIZE)
    }
    dump.update(overrides)
    return dump


# --- fetch_trending: argument validation ---------------------------------------


def test_fetch_trending_rejects_bad_kind():
    with pytest.raises(ValueError, match="kind must be one of"):
        sleeper.fetch_trending(kind="steal")


@pytest.mark.parametrize("hours", [0, -1, sleeper.MAX_LOOKBACK_HOURS + 1])
def test_fetch_trending_rejects_out_of_range_lookback(hours):
    with pytest.raises(ValueError, match="lookback_hours must be"):
        sleeper.fetch_trending(lookback_hours=hours)


@pytest.mark.parametrize("limit", [0, -5, sleeper.MAX_LIMIT + 1])
def test_fetch_trending_rejects_out_of_range_limit(limit):
    with pytest.raises(ValueError, match="limit must be"):
        sleeper.fetch_trending(limit=limit)


@pytest.mark.parametrize("bad", [True, False, 48.5, "48", None])
def test_fetch_trending_rejects_non_int_lookback(bad):
    """A bool passes a plain range check, because bool subclasses int."""
    with pytest.raises(ValueError, match="lookback_hours must be an int"):
        sleeper.fetch_trending(lookback_hours=bad)


@pytest.mark.parametrize("bad", [True, 10.0, "10"])
def test_fetch_trending_rejects_non_int_limit(bad):
    with pytest.raises(ValueError, match="limit must be an int"):
        sleeper.fetch_trending(limit=bad)


def test_fetch_trending_validation_runs_before_any_request(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("a request was made despite invalid arguments")

    monkeypatch.setattr(sleeper.requests, "get", explode)
    with pytest.raises(ValueError):
        sleeper.fetch_trending(lookback_hours=True)


# --- fetch_trending: response validation ---------------------------------------


def test_fetch_trending_passes_params(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return _FakeResponse([{"player_id": "1234", "count": 9}])

    monkeypatch.setattr(sleeper.requests, "get", fake_get)
    rows = sleeper.fetch_trending(kind="drop", lookback_hours=72, limit=10)

    assert rows == [{"player_id": "1234", "count": 9}]
    assert seen["url"].endswith("/players/nfl/trending/drop")
    assert seen["params"] == {"lookback_hours": 72, "limit": 10}
    assert seen["timeout"] == sleeper.TIMEOUT_SECONDS


def test_non_200_raises(monkeypatch):
    monkeypatch.setattr(
        sleeper.requests, "get", lambda *a, **k: _FakeResponse(None, status_code=503)
    )
    with pytest.raises(sleeper.SleeperError, match="HTTP 503"):
        sleeper.fetch_trending()


def test_request_exception_becomes_sleeper_error(monkeypatch):
    def boom(*a, **k):
        raise sleeper.requests.ConnectionError("no route")

    monkeypatch.setattr(sleeper.requests, "get", boom)
    with pytest.raises(sleeper.SleeperError, match="request failed"):
        sleeper.fetch_trending()


def test_non_json_raises(monkeypatch):
    monkeypatch.setattr(
        sleeper.requests, "get", lambda *a, **k: _FakeResponse(None, valid_json=False)
    )
    with pytest.raises(sleeper.SleeperError, match="not JSON"):
        sleeper.fetch_trending()


def test_trending_non_list_raises(monkeypatch):
    monkeypatch.setattr(
        sleeper.requests, "get", lambda *a, **k: _FakeResponse({"oops": True})
    )
    with pytest.raises(sleeper.SleeperError, match="expected a list"):
        sleeper.fetch_trending()


@pytest.mark.parametrize("row", ["not a dict", 42, None, ["nested"]])
def test_trending_non_object_row_raises(monkeypatch, row):
    monkeypatch.setattr(
        sleeper.requests,
        "get",
        lambda *a, **k: _FakeResponse([{"player_id": "1234", "count": 1}, row]),
    )
    with pytest.raises(sleeper.SleeperError, match="row 1 is a"):
        sleeper.fetch_trending()


@pytest.mark.parametrize(
    "row", [{"count": 3}, {"player_id": None}, {"player_id": ""}, {"player_id": 1234}]
)
def test_trending_row_without_usable_id_raises(monkeypatch, row):
    monkeypatch.setattr(sleeper.requests, "get", lambda *a, **k: _FakeResponse([row]))
    with pytest.raises(sleeper.SleeperError, match="row 0 has no usable player_id"):
        sleeper.fetch_trending()


# --- fetch_players: caching ----------------------------------------------------


def test_fetch_players_writes_and_reuses_cache(tmp_path, monkeypatch):
    calls = []
    payload = _plausible_dump()

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        return _FakeResponse(payload)

    monkeypatch.setattr(sleeper.requests, "get", fake_get)
    cache = tmp_path / "nested" / "players.json"

    assert sleeper.fetch_players(cache) == payload
    assert json.loads(cache.read_text()) == payload

    assert sleeper.fetch_players(cache) == payload
    assert len(calls) == 1, "a fresh cache must not trigger a second request"


def test_fetch_players_refetches_when_ttl_is_zero(tmp_path, monkeypatch):
    calls = []
    payload = _plausible_dump()
    monkeypatch.setattr(
        sleeper.requests,
        "get",
        lambda *a, **k: (calls.append(1), _FakeResponse(payload))[1],
    )
    cache = tmp_path / "players.json"

    sleeper.fetch_players(cache)
    sleeper.fetch_players(cache, max_age_seconds=0)
    assert len(calls) == 2


def test_fetch_players_refetches_when_cache_is_older_than_ttl(tmp_path, monkeypatch):
    calls = []
    payload = _plausible_dump()
    monkeypatch.setattr(
        sleeper.requests,
        "get",
        lambda *a, **k: (calls.append(1), _FakeResponse(payload))[1],
    )
    cache = tmp_path / "players.json"
    sleeper.fetch_players(cache)

    stale = cache_mod.time.time() - 10_000
    os.utime(cache, (stale, stale))
    sleeper.fetch_players(cache, max_age_seconds=3600)
    assert len(calls) == 2


def test_future_mtime_does_not_count_as_fresh(tmp_path, monkeypatch):
    """Clock skew must not pin a stale dump past its TTL."""
    calls = []
    payload = _plausible_dump()
    monkeypatch.setattr(
        sleeper.requests,
        "get",
        lambda *a, **k: (calls.append(1), _FakeResponse(payload))[1],
    )
    cache = tmp_path / "players.json"
    sleeper.fetch_players(cache)

    future = cache_mod.time.time() + 86_400
    os.utime(cache, (future, future))
    assert not cache_mod.is_fresh(cache, 3600)

    sleeper.fetch_players(cache, max_age_seconds=3600)
    assert len(calls) == 2


def test_fetch_players_refetches_on_corrupt_cache(tmp_path, monkeypatch):
    payload = _plausible_dump()
    monkeypatch.setattr(sleeper.requests, "get", lambda *a, **k: _FakeResponse(payload))
    cache = tmp_path / "players.json"
    cache.write_text("{ this is not json")

    assert sleeper.fetch_players(cache) == payload


def test_fetch_players_refetches_on_truncated_cache(tmp_path, monkeypatch):
    """A cache that parses but is far too small must not be served."""
    payload = _plausible_dump()
    monkeypatch.setattr(sleeper.requests, "get", lambda *a, **k: _FakeResponse(payload))
    cache = tmp_path / "players.json"
    cache.write_text(json.dumps({"1234": {"full_name": "Lonely"}}))

    assert sleeper.fetch_players(cache) == payload


def test_fetch_players_rejects_truncated_response(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sleeper.requests, "get", lambda *a, **k: _FakeResponse({"1234": {}})
    )
    with pytest.raises(sleeper.SleeperError, match="treating as truncated"):
        sleeper.fetch_players(tmp_path / "players.json")


def test_fetch_players_does_not_cache_a_rejected_response(tmp_path, monkeypatch):
    cache = tmp_path / "players.json"
    monkeypatch.setattr(
        sleeper.requests, "get", lambda *a, **k: _FakeResponse({"1234": {}})
    )
    with pytest.raises(sleeper.SleeperError):
        sleeper.fetch_players(cache)
    assert not cache.exists(), "a rejected payload must never reach the cache"


def test_fetch_players_rejects_non_object_response(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sleeper.requests, "get", lambda *a, **k: _FakeResponse(["a", "list"])
    )
    with pytest.raises(sleeper.SleeperError, match="expected an object"):
        sleeper.fetch_players(tmp_path / "players.json")


# --- the join ------------------------------------------------------------------


def test_fetch_trending_players_keeps_unmatched_players(tmp_path, monkeypatch):
    dump = _plausible_dump(
        p_known={"full_name": "Player One", "position": "RB", "team": "SEA"}
    )

    def fake_get(url, params=None, timeout=None):
        if "trending" in url:
            return _FakeResponse(
                [
                    {"player_id": "p_known", "count": 40},
                    {"player_id": "p_missing", "count": 12},
                ]
            )
        return _FakeResponse(dump)

    monkeypatch.setattr(sleeper.requests, "get", fake_get)
    rows = sleeper.fetch_trending_players(tmp_path / "players.json")

    assert len(rows) == 2, "an unmatched player id must not be dropped"
    assert rows[0]["name"] == "Player One"
    assert rows[0]["position"] == "RB"
    assert rows[1]["player_id"] == "p_missing"
    assert rows[1]["name"] is None
    assert rows[1]["count"] == 12


def test_fetch_trending_players_survives_a_non_object_record(tmp_path, monkeypatch):
    dump = _plausible_dump(p_weird="this should be an object")

    def fake_get(url, params=None, timeout=None):
        if "trending" in url:
            return _FakeResponse([{"player_id": "p_weird", "count": 5}])
        return _FakeResponse(dump)

    monkeypatch.setattr(sleeper.requests, "get", fake_get)
    rows = sleeper.fetch_trending_players(tmp_path / "players.json")

    assert len(rows) == 1
    assert rows[0]["player_id"] == "p_weird"
    assert rows[0]["count"] == 5
    assert rows[0]["name"] is None


def test_fetch_trending_players_preserves_order(tmp_path, monkeypatch):
    dump = _plausible_dump()

    def fake_get(url, params=None, timeout=None):
        if "trending" in url:
            return _FakeResponse(
                [{"player_id": str(i), "count": 100 - i} for i in range(5)]
            )
        return _FakeResponse(dump)

    monkeypatch.setattr(sleeper.requests, "get", fake_get)
    rows = sleeper.fetch_trending_players(tmp_path / "players.json")
    assert [r["count"] for r in rows] == [100, 99, 98, 97, 96]


# --- live ----------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("FAAB_LIVE_TESTS") != "1",
    reason="set FAAB_LIVE_TESTS=1 to hit the live Sleeper API",
)
def test_live_trending_returns_players():
    rows = sleeper.fetch_trending(kind="add", lookback_hours=48, limit=5)
    assert len(rows) == 5
    assert all(isinstance(r["player_id"], str) and r["player_id"] for r in rows)
    assert all(isinstance(r["count"], int) for r in rows)


@pytest.mark.skipif(
    os.environ.get("FAAB_LIVE_TESTS") != "1",
    reason="set FAAB_LIVE_TESTS=1 to hit the live Sleeper API",
)
def test_live_player_dump_clears_the_truncation_floor(tmp_path):
    dump = sleeper.fetch_players(tmp_path / "players.json")
    assert len(dump) >= sleeper.MIN_PLAYER_DUMP_SIZE
