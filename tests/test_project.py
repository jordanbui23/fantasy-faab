"""Tests for the usage-based projection.

The central claim is that touchdown luck is removed, so most of these tests are about two
players whose play was identical and whose touchdown counts were not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.model import project  # noqa: E402
from faab.model.project import (  # noqa: E402
    LeagueRates,
    Scoring,
    build_projections,
    build_seasons,
    league_rates,
    shrink,
)

RATES = LeagueRates(
    yards_per_attempt=7.0,
    pass_td_per_attempt=0.05,
    interceptions_per_attempt=0.025,
    yards_per_carry=4.2,
    rush_td_per_carry=0.03,
    catch_rate=0.68,
    yards_per_target=7.6,
    rec_td_per_target=0.05,
)


def _row(week, name="Player One", position="RB", gsis="1", **stats):
    row = {
        "player_id": gsis,
        "player_display_name": name,
        "position": position,
        "team": "SEA",
        "week": str(week),
    }
    row.update({k: str(v) for k, v in stats.items()})
    return row


def _points(rows, through_week=2, scoring=None):
    projections = build_projections(rows, through_week, scoring)
    return {p.name: p.points for p in projections.values()}


# --- shrinkage, the mechanism ---------------------------------------------------


def test_no_opportunities_means_the_league_rate():
    assert shrink(0.0, 0.0, 0.05, 75.0) == pytest.approx(0.05)


def test_a_large_sample_approaches_the_players_own_rate():
    # 10000 carries at 0.10 per carry, against a 0.03 league rate.
    assert shrink(1000.0, 10000.0, 0.03, 75.0) == pytest.approx(0.0995, abs=0.001)


def test_a_small_sample_stays_near_the_league_rate():
    """Five touchdowns in 40 carries is a 0.125 rate, and must not be believed."""
    result = shrink(5.0, 40.0, 0.03, project.RARE_EVENT_PRIOR)
    assert result < 0.045, f"a 40-carry sample moved the rate to {result}"
    assert result > 0.03


def test_shrinkage_is_monotonic_in_the_players_own_total():
    low = shrink(1.0, 40.0, 0.03, 75.0)
    high = shrink(5.0, 40.0, 0.03, 75.0)
    assert high > low


def test_a_zero_prior_trusts_the_player_completely():
    assert shrink(5.0, 40.0, 0.03, 0.0) == pytest.approx(0.125)


def test_a_zero_prior_with_no_opportunities_falls_back_to_the_league_rate():
    assert shrink(0.0, 0.0, 0.03, 0.0) == pytest.approx(0.03)


# --- touchdown luck is removed, which is the whole point -----------------------


def test_identical_volume_with_different_touchdowns_projects_close():
    """The lucky and the unlucky back had the same role, so they must rank together."""
    lucky = [
        _row(1, name="Lucky", gsis="a", carries=20, rushing_yards=80, rushing_tds=3),
        _row(2, name="Lucky", gsis="a", carries=20, rushing_yards=80, rushing_tds=2),
    ]
    unlucky = [
        _row(1, name="Unlucky", gsis="b", carries=20, rushing_yards=80, rushing_tds=0),
        _row(2, name="Unlucky", gsis="b", carries=20, rushing_yards=80, rushing_tds=0),
    ]
    points = _points(lucky + unlucky)
    gap = abs(points["Lucky"] - points["Unlucky"])
    assert gap < 3.0, f"touchdown luck still moved the projection by {gap:.1f} points"


def test_the_lucky_player_still_ranks_slightly_higher():
    """Shrinkage pulls toward the mean without erasing the signal entirely."""
    rows = [
        _row(1, name="Lucky", gsis="a", carries=20, rushing_yards=80, rushing_tds=3),
        _row(2, name="Lucky", gsis="a", carries=20, rushing_yards=80, rushing_tds=2),
        _row(1, name="Unlucky", gsis="b", carries=20, rushing_yards=80, rushing_tds=0),
        _row(2, name="Unlucky", gsis="b", carries=20, rushing_yards=80, rushing_tds=0),
    ]
    points = _points(rows)
    assert points["Lucky"] > points["Unlucky"]


def test_more_volume_beats_more_touchdowns():
    """Volume carries over to next week and a touchdown streak does not."""
    rows = [
        _row(1, name="Workhorse", gsis="a", carries=25, rushing_yards=100, rushing_tds=0),
        _row(2, name="Workhorse", gsis="a", carries=25, rushing_yards=100, rushing_tds=0),
        _row(1, name="Scorer", gsis="b", carries=6, rushing_yards=24, rushing_tds=2),
        _row(2, name="Scorer", gsis="b", carries=6, rushing_yards=24, rushing_tds=2),
    ]
    points = _points(rows)
    assert points["Workhorse"] > points["Scorer"]


# --- volume ---------------------------------------------------------------------


def test_the_most_recent_game_weighs_heaviest():
    rising = [
        _row(1, name="Rising", gsis="a", carries=5, rushing_yards=20),
        _row(2, name="Rising", gsis="a", carries=25, rushing_yards=100),
    ]
    falling = [
        _row(1, name="Falling", gsis="b", carries=25, rushing_yards=100),
        _row(2, name="Falling", gsis="b", carries=5, rushing_yards=20),
    ]
    points = _points(rising + falling)
    assert points["Rising"] > points["Falling"]


def test_volume_averages_over_games_played_not_weeks_elapsed():
    """A missed week must not drag a returning player's projection toward zero."""
    every_week = [
        _row(w, name="Every", gsis="a", carries=20, rushing_yards=80) for w in (1, 2, 3)
    ]
    missed_two = [
        _row(w, name="Missed", gsis="b", carries=20, rushing_yards=80) for w in (1, 3)
    ]
    points = _points(every_week + missed_two, through_week=3)
    assert points["Every"] == pytest.approx(points["Missed"], rel=0.01)


def test_only_the_last_three_games_count():
    rows = [
        _row(1, name="Old", gsis="a", carries=40, rushing_yards=200),
        _row(2, name="Old", gsis="a", carries=5, rushing_yards=20),
        _row(3, name="Old", gsis="a", carries=5, rushing_yards=20),
        _row(4, name="Old", gsis="a", carries=5, rushing_yards=20),
    ]
    seasons = build_seasons(rows, 4)
    assert seasons["a"].per_game("carries") == pytest.approx(5.0)


def test_a_week_after_the_cutoff_is_ignored():
    rows = [
        _row(1, name="X", gsis="a", carries=10, rushing_yards=40),
        _row(3, name="X", gsis="a", carries=40, rushing_yards=200),
    ]
    seasons = build_seasons(rows, 2)
    assert list(seasons["a"].weeks) == [1]


def test_a_row_without_a_player_id_is_skipped():
    rows = [_row(1, gsis="", carries=10)]
    assert build_seasons(rows, 2) == {}


def test_a_row_with_a_non_numeric_week_is_skipped():
    rows = [_row("bye", gsis="a", carries=10)]
    assert build_seasons(rows, 2) == {}


# --- the cutoff binds the league rates too, not only the player totals ---------


def test_a_later_week_cannot_change_an_earlier_projection():
    """League rates must read the same window as the player totals.

    Calibrating them on every row lets week 3 inform a week 2 projection. Nothing in the
    output shows it, and every backtest then reads better than it should.
    """
    early = [
        _row(1, name="Back", gsis="a", carries=20, rushing_yards=80, rushing_tds=1),
        _row(2, name="Back", gsis="a", carries=20, rushing_yards=80, rushing_tds=1),
    ]
    future = [
        _row(3, name="Other", gsis=f"z{i}", carries=100, rushing_yards=900, rushing_tds=40)
        for i in range(5)
    ]
    without_future = build_projections(early, 2)["a"].points
    with_future = build_projections(early + future, 2)["a"].points
    assert with_future == pytest.approx(without_future), (
        f"week 3 leaked into a week 2 projection: {with_future} against {without_future}"
    )


def test_rows_through_drops_later_and_unusable_weeks():
    rows = [_row(1, gsis="a"), _row(3, gsis="a"), _row("bye", gsis="a")]
    assert [project.week_of(r) for r in project.rows_through(rows, 2)] == [1]


# --- league rate calibration ---------------------------------------------------


def test_league_rates_are_calibrated_from_the_data():
    rows = [
        _row(1, gsis="a", carries=100, rushing_yards=500, rushing_tds=5),
        _row(1, gsis="b", targets=100, receptions=70, receiving_yards=800, receiving_tds=10),
    ]
    rates = league_rates(rows)
    assert rates.yards_per_carry == pytest.approx(5.0)
    assert rates.rush_td_per_carry == pytest.approx(0.05)
    assert rates.catch_rate == pytest.approx(0.70)
    assert rates.yards_per_target == pytest.approx(8.0)
    assert rates.rec_td_per_target == pytest.approx(0.10)


def test_league_rates_fall_back_when_a_denominator_is_zero():
    """An empty season must not divide by zero."""
    rates = league_rates([])
    assert rates.yards_per_carry > 0
    assert rates.catch_rate > 0


# --- scoring ------------------------------------------------------------------


def test_the_reception_value_changes_a_receivers_projection():
    rows = [
        _row(1, name="Catcher", position="WR", gsis="a", targets=10, receptions=7,
             receiving_yards=80),
        _row(2, name="Catcher", position="WR", gsis="a", targets=10, receptions=7,
             receiving_yards=80),
    ]
    full = _points(rows, scoring=Scoring(reception=1.0))["Catcher"]
    half = _points(rows, scoring=Scoring(reception=0.5))["Catcher"]
    standard = _points(rows, scoring=Scoring(reception=0.0))["Catcher"]
    assert full > half > standard


def test_the_passing_touchdown_value_changes_a_quarterbacks_projection():
    rows = [
        _row(w, name="Passer", position="QB", gsis="a", attempts=35,
             passing_yards=250, passing_tds=2)
        for w in (1, 2)
    ]
    four = _points(rows, scoring=Scoring(pass_td=4.0))["Passer"]
    six = _points(rows, scoring=Scoring(pass_td=6.0))["Passer"]
    assert six > four


def test_a_lost_fumble_costs_points():
    clean = [_row(w, name="Clean", gsis="a", carries=20, rushing_yards=80) for w in (1, 2)]
    loose = [
        _row(w, name="Loose", gsis="b", carries=20, rushing_yards=80,
             fumbles_lost_total=2)
        for w in (1, 2)
    ]
    points = _points(clean + loose)
    assert points["Clean"] > points["Loose"]


def test_an_interception_costs_a_quarterback_points():
    clean = [
        _row(w, name="Clean", position="QB", gsis="a", attempts=35, passing_yards=250)
        for w in (1, 2)
    ]
    loose = [
        _row(w, name="Loose", position="QB", gsis="b", attempts=35, passing_yards=250,
             passing_interceptions=3)
        for w in (1, 2)
    ]
    points = _points(clean + loose)
    assert points["Clean"] > points["Loose"]


def test_points_are_never_negative():
    """A projection is used as an assignment weight, where a negative would misrank."""
    rows = [
        _row(w, name="Awful", position="QB", gsis="a", attempts=5, passing_yards=0,
             passing_interceptions=4, fumbles_lost_total=3)
        for w in (1, 2)
    ]
    assert _points(rows)["Awful"] >= 0.0


# --- positions ----------------------------------------------------------------


def test_a_kicker_is_projected_from_his_own_scoring():
    rows = [
        _row(w, name="Boot", position="K", gsis="a", fg_made_40_49=2, pat_made=3)
        for w in (1, 2)
    ]
    assert _points(rows)["Boot"] == pytest.approx(2 * 4 + 3)


def test_a_defense_is_not_projected():
    rows = [_row(1, name="Hawks", position="DEF", gsis="a")]
    assert build_projections(rows, 2) == {}


def test_a_player_with_no_volume_projects_zero():
    rows = [_row(w, name="Ghost", gsis="a") for w in (1, 2)]
    assert _points(rows)["Ghost"] == pytest.approx(0.0, abs=0.01)


def test_components_are_kept_for_explanation():
    rows = [
        _row(w, name="Back", gsis="a", carries=20, rushing_yards=80, targets=4,
             receptions=3, receiving_yards=30, rushing_tds=1)
        for w in (1, 2)
    ]
    projection = build_projections(rows, 2)["a"]
    assert projection.carries == pytest.approx(20.0)
    assert projection.targets == pytest.approx(4.0)
    assert projection.opportunities == pytest.approx(24.0)
    assert projection.projected_touchdowns > 0
    assert projection.games_played == 2
