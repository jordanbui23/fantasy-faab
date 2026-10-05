"""nflverse collector.

nflverse publishes NFL data as versioned files on GitHub releases, and recommends
linking to those release URLs for direct access. This module reads the CSV variants
with the standard library rather than going through `nfl_data_py`, for two reasons.

The wrapper pins `pandas<2.0`, which has no Python 3.12 wheel, so it cannot install
here. And the host it was built on has a glibc older than 2.28, so modern numpy and
pyarrow have no compatible manylinux wheel and fall back to a source build that fails.
Reading CSV needs neither.

Update cadence, from nflverse's own data schedule: play-by-play and derived player
stats nightly after each game day, snap counts at 0, 6, 12 and 18 UTC subject to Pro
Football Reference, and schedules every five minutes in season. A Tuesday morning run
therefore includes Monday night.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any

from faab.cache import fetch_text_cached, is_fresh

RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60

# A real single-season file is hundreds of kilobytes. These floors exist to catch a
# truncated download, which would otherwise read as "that player had no snaps".
_MIN_BYTES = {
    "stats_player_week": 200_000,
    "snap_counts": 100_000,
    "injuries": 20_000,
    "depth_charts": 100_000,
    "players": 500_000,
    "games": 100_000,
}


def _parse_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _load_release(
    tag: str,
    filename: str,
    cache_dir: Path,
    max_age_seconds: int,
    min_bytes: int,
) -> list[dict[str, str]]:
    url = f"{RELEASE_BASE}/{tag}/{filename}"
    # The tag is part of the cache path because two release tags may publish the same
    # filename, and a collision would serve one tag's data as the other's.
    cache_path = cache_dir / "nflverse" / tag / filename.removesuffix(".gz")
    text = fetch_text_cached(url, cache_path, max_age_seconds, min_bytes=min_bytes)
    rows = _parse_csv(text)
    if not rows:
        raise ValueError(f"{url}: parsed to zero rows")
    return rows


def load_weekly_player_stats(
    season: int, cache_dir: Path, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> list[dict[str, str]]:
    """Per-player, per-week stats including target_share, air_yards_share and wopr."""
    return _load_release(
        "stats_player",
        f"stats_player_week_{season}.csv.gz",
        cache_dir,
        max_age_seconds,
        _MIN_BYTES["stats_player_week"],
    )


def load_snap_counts(
    season: int, cache_dir: Path, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> list[dict[str, str]]:
    """Game-level snap counts from Pro Football Reference, with offense_pct."""
    return _load_release(
        "snap_counts",
        f"snap_counts_{season}.csv.gz",
        cache_dir,
        max_age_seconds,
        _MIN_BYTES["snap_counts"],
    )


def load_injuries(
    season: int, cache_dir: Path, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> list[dict[str, str]]:
    """Weekly injury report rows, including practice status and game designation."""
    return _load_release(
        "injuries",
        f"injuries_{season}.csv",
        cache_dir,
        max_age_seconds,
        _MIN_BYTES["injuries"],
    )


def load_depth_charts(
    season: int, cache_dir: Path, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> list[dict[str, str]]:
    """Weekly team depth charts, which show a promotion before the box score does."""
    return _load_release(
        "depth_charts",
        f"depth_charts_{season}.csv",
        cache_dir,
        max_age_seconds,
        _MIN_BYTES["depth_charts"],
    )


def load_players(
    cache_dir: Path, max_age_seconds: int = 7 * 24 * 60 * 60
) -> list[dict[str, str]]:
    """Player directory with cross-platform ids, used to join Yahoo to nflverse."""
    return _load_release(
        "players", "players.csv", cache_dir, max_age_seconds, _MIN_BYTES["players"]
    )


def load_cached_games(cache_dir: Path, max_age_seconds: int) -> list[dict[str, str]]:
    """The last schedule fetched, when it is younger than `max_age_seconds`."""
    cache_path = cache_dir / "nflverse" / "games.csv"
    if not is_fresh(cache_path, max_age_seconds):
        raise ValueError(f"{cache_path}: missing, or older than {max_age_seconds // 3600} hours")
    rows = _parse_csv(cache_path.read_text(encoding="utf-8"))
    if not rows:
        raise ValueError(f"{cache_path}: parsed to zero rows")
    return rows


def load_games(
    cache_dir: Path, max_age_seconds: int = 60 * 60
) -> list[dict[str, str]]:
    """Every scheduled and completed game, used to resolve the current week."""
    cache_path = cache_dir / "nflverse" / "games.csv"
    text = fetch_text_cached(
        GAMES_URL, cache_path, max_age_seconds, min_bytes=_MIN_BYTES["games"]
    )
    rows = _parse_csv(text)
    if not rows:
        raise ValueError(f"{GAMES_URL}: parsed to zero rows")
    return rows


def as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a CSV cell to a finite float. A blank or unusable cell becomes `default`.

    nflverse leaves a stat blank rather than writing zero when a player did not record
    it, and `float("")` raises, so every numeric read goes through here.

    A non-finite value is rejected rather than passed through. `float("nan")` and
    `float("inf")` both parse, and either one silently wins or loses every comparison
    it takes part in, which would reorder a ranking without any error.
    """
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _week_of(row: dict[str, str]) -> int | None:
    """The row's week as an int, or None when it is absent or not a plain number.

    `csv.DictReader` fills a short row's missing trailing fields with None rather than
    with the empty string, so this cannot assume a string.
    """
    raw = row.get("week")
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    return int(raw)


def _has_result(row: dict[str, str]) -> bool:
    """True when the game has a final margin. A tie is 0, which is falsy, so compare."""
    return isinstance(row.get("result"), str) and row["result"].strip() != ""


def completed_week(games: list[dict[str, str]], season: int) -> int:
    """The highest week in `season` where EVERY scheduled game has a final result.

    Zero when no week is complete. A week with some games still to play is excluded on
    purpose: its per-player stats are partial, and ranking a waiver candidate on a
    partial week reads a player who has not played yet as having no opportunity.
    """
    played: dict[int, int] = {}
    scheduled: dict[int, int] = {}
    for row in games:
        if row.get("season") != str(season):
            continue
        week = _week_of(row)
        if week is None:
            continue
        scheduled[week] = scheduled.get(week, 0) + 1
        if _has_result(row):
            played[week] = played.get(week, 0) + 1

    finished = [
        week for week, total in scheduled.items() if played.get(week, 0) == total
    ]
    return max(finished, default=0)


def upcoming_week(games: list[dict[str, str]], season: int) -> int:
    """The lowest week in `season` that still has a game without a result.

    This is the week a start/sit report is about. Zero when the season is over.
    """
    pending = []
    for row in games:
        if row.get("season") != str(season) or _has_result(row):
            continue
        week = _week_of(row)
        if week is not None:
            pending.append(week)
    return min(pending, default=0)


def teams_on_bye(games: list[dict[str, str]], season: int, week: int) -> set[str]:
    """Teams with no game in `week`, derived from the teams that do have one.

    A player on one of these teams cannot score, so the lineup builder must never
    start him. This is computed rather than hardcoded because bye weeks move annually.
    """
    all_teams: set[str] = set()
    playing: set[str] = set()
    for row in games:
        if row.get("season") != str(season):
            continue
        home, away = row.get("home_team"), row.get("away_team")
        for team in (home, away):
            if isinstance(team, str) and team:
                all_teams.add(team)
                if _week_of(row) == week:
                    playing.add(team)
    if not playing:
        return set()
    return all_teams - playing

