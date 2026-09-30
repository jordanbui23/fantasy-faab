"""Tests for the nflverse collector.

Network calls are stubbed. Live tests are skipped unless FAAB_LIVE_TESTS=1.
"""

from __future__ import annotations

import gzip
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import cache  # noqa: E402
from faab.collectors import nflverse  # noqa: E402
from faab.model.project import Scoring  # noqa: E402


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


def _csv_bytes(header: str, row: str, min_bytes: int = 600_000) -> bytes:
    """A CSV comfortably above every release's truncation floor."""
    line = row + "\n"
    repeat = max(20, min_bytes // len(line) + 1)
    return (header + "\n" + line * repeat).encode()


def _stub(monkeypatch, body: bytes, seen: dict | None = None):
    def fake_get(url, timeout=None):
        if seen is not None:
            seen["url"] = url
        return _FakeResponse(gzip.compress(body) if url.endswith(".gz") else body)

    monkeypatch.setattr(cache.requests, "get", fake_get)


# --- URL construction ----------------------------------------------------------


def test_weekly_stats_requests_the_right_release(tmp_path, monkeypatch):
    seen = {}
    _stub(monkeypatch, _csv_bytes("season,week,player_id", "2026,2,x"), seen)
    nflverse.load_weekly_player_stats(2026, tmp_path)
    assert seen["url"] == (
        f"{nflverse.RELEASE_BASE}/stats_player/stats_player_week_2026.csv.gz"
    )


def test_snap_counts_requests_the_right_release(tmp_path, monkeypatch):
    seen = {}
    _stub(monkeypatch, _csv_bytes("season,week,player", "2026,2,x"), seen)
    nflverse.load_snap_counts(2026, tmp_path)
    assert seen["url"] == f"{nflverse.RELEASE_BASE}/snap_counts/snap_counts_2026.csv.gz"


def test_injuries_requests_an_uncompressed_release(tmp_path, monkeypatch):
    seen = {}
    _stub(monkeypatch, _csv_bytes("season,week,gsis_id", "2026,2,x"), seen)
    nflverse.load_injuries(2026, tmp_path)
    assert seen["url"] == f"{nflverse.RELEASE_BASE}/injuries/injuries_2026.csv"


def test_games_requests_the_nfldata_url(tmp_path, monkeypatch):
    seen = {}
    _stub(monkeypatch, _csv_bytes("season,week,result", "2026,1,3"), seen)
    nflverse.load_games(tmp_path)
    assert seen["url"] == nflverse.GAMES_URL


def test_the_cache_path_carries_the_tag_and_drops_the_gz_suffix(tmp_path, monkeypatch):
    _stub(monkeypatch, _csv_bytes("season,week,player", "2026,2,x"))
    nflverse.load_snap_counts(2026, tmp_path)
    cached = tmp_path / "nflverse" / "snap_counts" / "snap_counts_2026.csv"
    assert cached.exists()
    assert not cached.with_suffix(".csv.gz").exists()


def test_two_tags_sharing_a_filename_do_not_collide(tmp_path, monkeypatch):
    """A cache keyed only on filename would serve one tag's data as the other's."""
    bodies = {
        "alpha": _csv_bytes("season,week,marker", "2026,2,ALPHA"),
        "beta": _csv_bytes("season,week,marker", "2026,2,BETA"),
    }

    def fake_get(url, timeout=None):
        tag = "alpha" if "/alpha/" in url else "beta"
        return _FakeResponse(bodies[tag])

    monkeypatch.setattr(cache.requests, "get", fake_get)
    first = nflverse._load_release("alpha", "same.csv", tmp_path, 3600, 1000)
    second = nflverse._load_release("beta", "same.csv", tmp_path, 3600, 1000)

    assert first[0]["marker"] == "ALPHA"
    assert second[0]["marker"] == "BETA", "the beta tag was served alpha's cache"


# --- parsing -------------------------------------------------------------------


def test_rows_parse_to_dicts(tmp_path, monkeypatch):
    body = _csv_bytes("season,week,target_share", "2026,2,0.31")
    _stub(monkeypatch, body)
    rows = nflverse.load_weekly_player_stats(2026, tmp_path)
    assert rows[0] == {"season": "2026", "week": "2", "target_share": "0.31"}
    assert len(rows) == body.decode().count("\n") - 1


def test_a_header_only_file_is_refused(tmp_path, monkeypatch):
    """A file with no data rows must fail rather than report an empty week."""
    wide_header = ",".join(f"col_{i}" for i in range(4_000)) + "\n"
    monkeypatch.setattr(
        cache.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(wide_header.encode()),
    )
    with pytest.raises(ValueError, match="parsed to zero rows"):
        nflverse.load_injuries(2026, tmp_path)


def test_a_truncated_release_is_refused(tmp_path, monkeypatch):
    _stub(monkeypatch, b"season,week,player\n2026,2,x\n")
    with pytest.raises(cache.FetchError, match="treating as truncated"):
        nflverse.load_snap_counts(2026, tmp_path)


# --- as_float ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.31", 0.31),
        ("12", 12.0),
        ("-3.5", -3.5),
        ("", 0.0),
        (None, 0.0),
        ("NA", 0.0),
        ("not a number", 0.0),
    ],
)
def test_as_float_coerces_or_defaults(raw, expected):
    assert nflverse.as_float(raw) == expected


def test_as_float_honours_a_custom_default():
    assert nflverse.as_float("", default=-1.0) == -1.0
    assert nflverse.as_float("0.5", default=-1.0) == 0.5


def test_as_float_does_not_swallow_a_real_zero():
    """A recorded zero and a blank must not become indistinguishable."""
    assert nflverse.as_float("0", default=99.0) == 0.0
    assert nflverse.as_float("", default=99.0) == 99.0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_as_float_rejects_a_non_finite_value(raw):
    """nan and inf both parse, then silently win or lose every comparison."""
    assert nflverse.as_float(raw, default=-1.0) == -1.0


# --- week resolution -----------------------------------------------------------


def _game(season, week, result, home="SEA", away="SF"):
    return {
        "season": str(season),
        "week": str(week),
        "result": result,
        "home_team": home,
        "away_team": away,
    }


def test_completed_week_needs_every_game_final():
    """A week with a game still to play must not count; its stats are partial."""
    games = [
        _game(2026, 1, "3"),
        _game(2026, 1, "-7", "GB", "CHI"),
        _game(2026, 2, "10"),
        _game(2026, 2, "", "GB", "CHI"),
    ]
    assert nflverse.completed_week(games, 2026) == 1


def test_completed_week_counts_a_zero_result_as_played():
    """A tie has result 0, which is falsy, and must still count."""
    assert nflverse.completed_week([_game(2026, 5, "0")], 2026) == 5


def test_completed_week_ignores_other_seasons():
    games = [_game(2025, 17, "10"), _game(2026, 1, "3")]
    assert nflverse.completed_week(games, 2026) == 1


def test_completed_week_is_zero_before_the_season_starts():
    assert nflverse.completed_week([_game(2026, 1, "")], 2026) == 0
    assert nflverse.completed_week([], 2026) == 0


def test_completed_week_ignores_a_non_numeric_week():
    games = [_game(2026, 1, "3"), {"season": "2026", "week": "WC", "result": "7"}]
    assert nflverse.completed_week(games, 2026) == 1


def test_completed_week_survives_a_short_row():
    """csv.DictReader fills a short row's missing fields with None, not ''."""
    games = [_game(2026, 1, "3"), {"season": "2026", "week": None, "result": None}]
    assert nflverse.completed_week(games, 2026) == 1


def test_upcoming_week_is_the_lowest_week_still_pending():
    games = [_game(2026, 1, "3"), _game(2026, 2, ""), _game(2026, 3, "")]
    assert nflverse.upcoming_week(games, 2026) == 2


def test_upcoming_week_is_zero_when_the_season_is_over():
    assert nflverse.upcoming_week([_game(2026, 1, "3")], 2026) == 0


def test_upcoming_week_ignores_other_seasons():
    games = [_game(2025, 1, ""), _game(2026, 4, "")]
    assert nflverse.upcoming_week(games, 2026) == 4


def test_teams_on_bye_are_those_absent_from_the_week():
    games = [
        _game(2026, 1, "3", "SEA", "SF"),
        _game(2026, 1, "7", "GB", "CHI"),
        _game(2026, 2, "", "SEA", "GB"),
    ]
    assert nflverse.teams_on_bye(games, 2026, 2) == {"SF", "CHI"}


def test_teams_on_bye_is_empty_for_a_week_with_no_games():
    """An out-of-range week must not report every team as on bye."""
    games = [_game(2026, 1, "3", "SEA", "SF")]
    assert nflverse.teams_on_bye(games, 2026, 99) == set()


def test_teams_on_bye_is_empty_when_everyone_plays():
    games = [_game(2026, 1, "", "SEA", "SF"), _game(2026, 1, "", "GB", "CHI")]
    assert nflverse.teams_on_bye(games, 2026, 1) == set()


# --- live ----------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("FAAB_LIVE_TESTS") != "1",
    reason="set FAAB_LIVE_TESTS=1 to hit the live nflverse releases",
)
def test_live_weekly_stats_carry_the_opportunity_columns(tmp_path):
    rows = nflverse.load_weekly_player_stats(2026, tmp_path)
    assert len(rows) > 500
    for column in ("target_share", "air_yards_share", "wopr", "carries", "targets"):
        assert column in rows[0], f"{column} is missing from the release"


@pytest.mark.skipif(
    os.environ.get("FAAB_LIVE_TESTS") != "1",
    reason="set FAAB_LIVE_TESTS=1 to hit the live nflverse releases",
)
def test_live_snap_counts_carry_offense_pct(tmp_path):
    rows = nflverse.load_snap_counts(2026, tmp_path)
    assert len(rows) > 500
    assert "offense_pct" in rows[0]


@pytest.mark.skipif(
    os.environ.get("FAAB_LIVE_TESTS") != "1",
    reason="set FAAB_LIVE_TESTS=1 to hit the live nflverse releases",
)
def test_live_completed_week_is_plausible(tmp_path):
    week = nflverse.completed_week(nflverse.load_games(tmp_path), 2026)
    assert 0 <= week <= 22


@pytest.mark.skipif(
    os.environ.get("FAAB_LIVE_TESTS") != "1",
    reason="set FAAB_LIVE_TESTS=1 to hit the live nflverse releases",
)
def test_live_default_scoring_reproduces_fantasy_points_ppr(tmp_path):
    """The Scoring defaults are nflverse's PPR formula, on every line they can model."""
    s = Scoring()

    def f(row, column):
        return nflverse.as_float(row.get(column))

    unmodelled = (
        "passing_2pt_conversions",
        "rushing_2pt_conversions",
        "receiving_2pt_conversions",
        "special_teams_tds",
    )
    release = nflverse.load_weekly_player_stats(2026, tmp_path)
    used = (
        "passing_yards", "passing_tds", "passing_interceptions", "rushing_yards",
        "rushing_tds", "receptions", "receiving_yards", "receiving_tds",
        "sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost",
        "fantasy_points_ppr", *unmodelled,
    )
    missing = [column for column in used if column not in release[0]]
    assert not missing, f"columns missing from the release: {missing}"

    rows = [
        row
        for row in release
        if row.get("season_type") == "REG"
        and row.get("position") in ("QB", "RB", "WR", "TE")
        and not any(f(row, column) for column in unmodelled)
    ]
    assert len(rows) > 500
    scoring_rows = [row for row in rows if f(row, "fantasy_points_ppr") > 0]
    assert len(scoring_rows) > len(rows) // 2, "most lines should score, or the check is vacuous"

    mismatched = []
    for row in rows:
        points = (
            f(row, "passing_yards") * s.pass_yards
            + f(row, "passing_tds") * s.pass_td
            + f(row, "passing_interceptions") * s.interception
            + f(row, "rushing_yards") * s.rush_yards
            + f(row, "rushing_tds") * s.rush_td
            + f(row, "receptions") * s.reception
            + f(row, "receiving_yards") * s.rec_yards
            + f(row, "receiving_tds") * s.rec_td
            + (
                f(row, "sack_fumbles_lost")
                + f(row, "rushing_fumbles_lost")
                + f(row, "receiving_fumbles_lost")
            )
            * s.fumble_lost
        )
        if abs(points - f(row, "fantasy_points_ppr")) > 0.011:
            mismatched.append((row.get("week"), row.get("position"), round(points, 2)))
    assert not mismatched, mismatched[:5]
