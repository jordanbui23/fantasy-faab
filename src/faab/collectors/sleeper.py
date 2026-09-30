"""Sleeper API collector.

Sleeper publishes a free, unauthenticated, read-only HTTP API. It cannot modify
anything, so there is no token and no OAuth. Docs: https://docs.sleeper.com/

Sleeper asks callers to stay under 1,000 requests per minute. This module makes at
most two calls per snapshot, and caches the 5 MB player dump on disk.

The trending endpoint reports NATIONAL add and drop velocity across all Sleeper
leagues. It is a proxy for what the field is chasing, not a read on any one league.
Do not let it stand in for a league-specific rival budget model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeGuard

import requests

from faab.cache import is_fresh, write_json_atomic

BASE_URL = "https://api.sleeper.app/v1"
TIMEOUT_SECONDS = 20
PLAYER_CACHE_TTL_SECONDS = 24 * 60 * 60

TRENDING_KINDS = frozenset({"add", "drop"})
MAX_LOOKBACK_HOURS = 168
MAX_LIMIT = 200

# The real dump carries upwards of 11,000 records. A payload far below that is
# truncated or partial, and caching it would poison every later run.
MIN_PLAYER_DUMP_SIZE = 2000


class SleeperError(RuntimeError):
    """Raised when Sleeper returns something unusable."""


def _require_plain_int(name: str, value: Any, low: int, high: int) -> int:
    """Reject a bool or a float that a plain range check would wave through."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}, got {value}")
    return value


def _get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise SleeperError(f"{url}: request failed: {exc}") from exc

    if response.status_code != 200:
        raise SleeperError(f"{url}: HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        raise SleeperError(f"{url}: response was not JSON") from exc


def fetch_trending(
    kind: str = "add",
    lookback_hours: int = 48,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return players trending by add or drop count over the lookback window.

    Each element carries `player_id` and `count`. Sleeper returns no player names
    here, so join against `fetch_players` to get anything human readable.
    """
    if kind not in TRENDING_KINDS:
        raise ValueError(f"kind must be one of {sorted(TRENDING_KINDS)}, got {kind!r}")
    lookback_hours = _require_plain_int("lookback_hours", lookback_hours, 1, MAX_LOOKBACK_HOURS)
    limit = _require_plain_int("limit", limit, 1, MAX_LIMIT)

    payload = _get_json(
        f"players/nfl/trending/{kind}",
        params={"lookback_hours": lookback_hours, "limit": limit},
    )
    if not isinstance(payload, list):
        raise SleeperError(f"trending/{kind}: expected a list, got {type(payload).__name__}")

    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise SleeperError(
                f"trending/{kind}: row {index} is a {type(row).__name__}, expected an object"
            )
        player_id = row.get("player_id")
        if not isinstance(player_id, str) or not player_id:
            raise SleeperError(f"trending/{kind}: row {index} has no usable player_id")
    return payload


def fetch_players(cache_path: Path, max_age_seconds: int = PLAYER_CACHE_TTL_SECONDS) -> dict[str, Any]:
    """Return the full NFL player dump, keyed by Sleeper player id.

    The dump is roughly 5 MB and changes slowly, so it is cached on disk. Sleeper's
    docs ask that it not be polled. Pass `max_age_seconds=0` to force a refresh.

    A cache that is unreadable, not an object, or implausibly small is discarded and
    refetched rather than returned, so a truncated download cannot silently strip
    metadata off every later candidate.
    """
    if is_fresh(cache_path, max_age_seconds):
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cached = None
        if _is_usable_dump(cached):
            return cached

    payload = _get_json("players/nfl")
    if not isinstance(payload, dict):
        raise SleeperError(
            f"players/nfl: expected an object, got {type(payload).__name__}"
        )
    if len(payload) < MIN_PLAYER_DUMP_SIZE:
        raise SleeperError(
            f"players/nfl: only {len(payload)} records, expected at least "
            f"{MIN_PLAYER_DUMP_SIZE}; treating as truncated"
        )

    write_json_atomic(cache_path, payload)
    return payload


def _is_usable_dump(candidate: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(candidate, dict) and len(candidate) >= MIN_PLAYER_DUMP_SIZE


def fetch_trending_players(
    cache_path: Path,
    kind: str = "add",
    lookback_hours: int = 48,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Trending players joined to name, position, NFL team and injury status.

    A player id Sleeper trends but does not carry in its own dump is kept, with
    `name` set to None, so a silent drop never shrinks the candidate list. The
    trending call already rejects a row with no usable id, so every element here
    carries a real `player_id`.
    """
    trending = fetch_trending(kind=kind, lookback_hours=lookback_hours, limit=limit)
    players = fetch_players(cache_path)

    joined = []
    for row in trending:
        player_id = row["player_id"]
        meta = players.get(player_id)
        if not isinstance(meta, dict):
            meta = {}
        joined.append(
            {
                "player_id": player_id,
                "count": row.get("count"),
                "name": meta.get("full_name"),
                "position": meta.get("position"),
                "team": meta.get("team"),
                "injury_status": meta.get("injury_status"),
                "years_exp": meta.get("years_exp"),
            }
        )
    return joined
