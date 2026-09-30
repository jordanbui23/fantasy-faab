"""Tests for the lineup builder.

The two hard exclusions are the reason this module exists, so they get the most cases:
a player on bye and a player ruled out must never reach a starting slot, whatever their
production says.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.league import League  # noqa: E402
from faab.model import lineup as lineup_mod  # noqa: E402
from faab.model.lineup import recommend_lineup  # noqa: E402
from faab.model.project import Projection  # noqa: E402
from faab.model.usage import Usage, WeekUsage  # noqa: E402
from faab.names import Player  # noqa: E402

WEEK = 3
THROUGH = 2


def _player(name, position, team="SEA", gsis_id=None, injury=""):
    return Player(
        name=name,
        position=position,
        team=team,
        gsis_id=gsis_id if gsis_id is not None else name.lower().replace(" ", ""),
        status="ACT",
        injury_status=injury,
    )


def _usage(player, points, snaps=(0.5, 0.5), target_share=0.2, carries=0.0):
    weeks = {}
    for offset, week in enumerate((THROUGH - 1, THROUGH)):
        weeks[week] = WeekUsage(
            week=week,
            points_ppr=points,
            target_share=target_share,
            carries=carries,
            snap_pct=snaps[offset],
        )
    return Usage(
        gsis_id=player.gsis_id,
        name=player.name,
        position=player.position,
        team=player.team,
        weeks=weeks,
    )


def _league(**slots):
    return League(slots=slots or {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})


def _projection(player, points):
    """Ranking now comes from a projection, so the fixtures supply one directly."""
    return Projection(
        gsis_id=player.gsis_id,
        name=player.name,
        position=player.position,
        team=player.team,
        points=points,
        games_played=2,
    )


def _build(roster, points, byes=(), league=None):
    usage = {p.gsis_id: _usage(p, points[p.name]) for p in roster if p.name in points}
    projections = {
        p.gsis_id: _projection(p, points[p.name]) for p in roster if p.name in points
    }
    return recommend_lineup(
        roster,
        usage,
        projections,
        set(byes),
        WEEK,
        league or _league(),
        through_week=THROUGH,
    )


# --- hard exclusions -----------------------------------------------------------


def test_a_player_on_bye_never_starts():
    best = _player("Bye Guy", "RB", team="GB")
    worse = _player("Available Guy", "RB", team="SEA")
    result = _build([best, worse], {"Bye Guy": 30.0, "Available Guy": 1.0}, byes={"GB"})

    starters = [c.starter.player.name for c in result.choices]
    assert "Bye Guy" not in starters
    assert "Available Guy" in starters
    assert [c.player.name for c in result.blocked] == ["Bye Guy"]
    assert "bye" in result.blocked[0].blocked


def test_a_blocked_player_appears_only_in_the_blocked_list():
    """He must be in exactly one place, so he can neither start nor look startable."""
    bye = _player("Bye Guy", "RB", team="GB")
    ok = _player("Available Guy", "RB", team="SEA")
    league = League(slots={"RB": 1})
    result = _build(
        [bye, ok], {"Bye Guy": 30.0, "Available Guy": 1.0}, byes={"GB"}, league=league
    )

    assert [c.player.name for c in result.blocked] == ["Bye Guy"]
    assert "Bye Guy" not in [c.player.name for c in result.bench]
    assert "Bye Guy" not in [c.starter.player.name for c in result.choices]


@pytest.mark.parametrize("status", sorted(lineup_mod.OUT_STATUSES))
def test_a_player_ruled_out_never_starts(status):
    out = _player("Out Guy", "RB", injury=status)
    ok = _player("Fine Guy", "RB")
    result = _build([out, ok], {"Out Guy": 40.0, "Fine Guy": 2.0})

    assert "Out Guy" not in [c.starter.player.name for c in result.choices]
    assert result.blocked[0].blocked == status


@pytest.mark.parametrize("status", sorted(lineup_mod.WARN_STATUSES))
def test_a_questionable_player_still_starts_but_carries_a_warning(status):
    """Doubtful is a risk, not a certainty, so it must not cost a slot silently."""
    risky = _player("Risky Guy", "RB", injury=status)
    result = _build([risky], {"Risky Guy": 20.0})

    starter = result.choices[0].starter
    assert starter.player.name == "Risky Guy"
    assert starter.warning == status
    assert starter.startable


def test_bye_takes_precedence_over_an_injury_designation():
    both = _player("Both Guy", "RB", team="GB", injury="QUESTIONABLE")
    result = _build([both], {"Both Guy": 10.0}, byes={"GB"})
    assert "bye" in result.blocked[0].blocked


# --- slot filling --------------------------------------------------------------


def test_flex_does_not_steal_the_only_eligible_running_back():
    """Filling FLEX first would leave an RB slot empty with an RB on the bench.

    FLEX is declared FIRST here on purpose. A builder that walked the slots in
    declaration order would hand the only back to FLEX, and the RB slot would go
    unfilled while the highest scorer sat in a flex spot.
    """
    rb = _player("Only RB", "RB")
    wr1 = _player("WR One", "WR")
    wr2 = _player("WR Two", "WR")
    wr3 = _player("WR Three", "WR")
    league = League(slots={"FLEX": 1, "RB": 1, "WR": 2})
    result = _build(
        [rb, wr1, wr2, wr3],
        {"Only RB": 50.0, "WR One": 40.0, "WR Two": 30.0, "WR Three": 20.0},
        league=league,
    )

    by_slot = {c.slot: c.starter.player.name for c in result.choices}
    assert by_slot["RB"] == "Only RB", "FLEX took the only back"
    assert by_slot["FLEX"] == "WR Three"
    assert result.unfilled == []


def test_an_unfillable_slot_is_reported_not_skipped_silently():
    result = _build([_player("Lone QB", "QB")], {"Lone QB": 20.0})
    assert "RB" in result.unfilled
    assert "WR" in result.unfilled
    assert len([s for s in result.unfilled if s == "RB"]) == 2


def test_the_highest_scorer_fills_the_slot():
    a = _player("Low", "WR")
    b = _player("High", "WR")
    league = League(slots={"WR": 1})
    result = _build([a, b], {"Low": 5.0, "High": 25.0}, league=league)
    assert result.choices[0].starter.player.name == "High"


def test_two_identically_scored_players_do_not_collide():
    """Index tracking, not equality, must decide who filled a slot."""
    a = _player("Alpha", "WR", gsis_id="a")
    b = _player("Bravo", "WR", gsis_id="b")
    league = League(slots={"WR": 2})
    result = _build([a, b], {"Alpha": 10.0, "Bravo": 10.0}, league=league)

    started = sorted(c.starter.player.name for c in result.choices)
    assert started == ["Alpha", "Bravo"], "one player filled both slots"
    assert result.bench == []


def test_a_player_with_no_usage_is_kept_and_sorts_last():
    known = _player("Known", "WR")
    unknown = _player("Unknown", "WR")
    league = League(slots={"WR": 1})
    result = _build([known, unknown], {"Known": 3.0}, league=league)

    assert result.choices[0].starter.player.name == "Known"
    bench = [c.player.name for c in result.bench]
    assert "Unknown" in bench, "an unjoined name must stay visible"
    assert result.bench[0].warning == "no stats matched"


def test_display_order_is_not_fill_order():
    league = League(slots={"QB": 1, "RB": 1, "FLEX": 1, "DEF": 1})
    roster = [
        _player("Q", "QB"),
        _player("R", "RB"),
        _player("W", "WR"),
        Player(name="Hawks", position="DEF", team="SEA", status="ACT"),
    ]
    result = _build(roster, {"Q": 20.0, "R": 15.0, "W": 10.0}, league=league)
    assert [c.slot for c in result.display_choices] == ["QB", "RB", "FLEX", "DEF"]


# --- close calls ---------------------------------------------------------------


def test_a_close_call_compares_a_starter_to_a_bench_player():
    league = League(slots={"WR": 1})
    result = _build(
        [_player("Starter", "WR"), _player("Benched", "WR")],
        {"Starter": 12.0, "Benched": 11.0},
        league=league,
    )
    close = result.close_calls
    assert len(close) == 1
    assert close[0].starter.player.name == "Starter"
    assert close[0].runner_up is not None
    assert close[0].runner_up.player.name == "Benched"


def test_two_starters_are_never_reported_as_a_close_call_against_each_other():
    """Both are already starting, so there is no decision to make."""
    league = League(slots={"WR": 2})
    result = _build(
        [_player("A", "WR"), _player("B", "WR")],
        {"A": 12.0, "B": 11.5},
        league=league,
    )
    assert result.close_calls == []


def test_a_clear_gap_is_not_a_close_call():
    league = League(slots={"WR": 1})
    result = _build(
        [_player("Clear", "WR"), _player("Distant", "WR")],
        {"Clear": 30.0, "Distant": 2.0},
        league=league,
    )
    assert result.close_calls == []


def test_an_unrankable_position_is_never_a_close_call():
    """Two defenses both score zero here, which is not a two-point decision."""
    league = League(slots={"DEF": 1})
    roster = [
        Player(name="Hawks", position="DEF", team="SEA", status="ACT"),
        Player(name="Packers", position="DEF", team="GB", status="ACT"),
    ]
    result = _build(roster, {}, league=league)
    assert result.close_calls == []
    assert result.choices[0].starter.rankable is False


def test_a_defense_does_not_get_a_no_stats_warning():
    """Its absence from the stats release is by design, not news."""
    league = League(slots={"DEF": 1})
    roster = [Player(name="Hawks", position="DEF", team="SEA", status="ACT")]
    result = _build(roster, {}, league=league)
    assert result.choices[0].starter.warning == ""


def test_no_bench_alternative_means_no_margin():
    league = League(slots={"WR": 1})
    result = _build([_player("Alone", "WR")], {"Alone": 10.0}, league=league)
    assert result.choices[0].margin is None
    assert result.choices[0].is_close is False


# --- optimal assignment, not greedy fill ---------------------------------------


def test_two_multi_position_slots_are_filled_optimally():
    """The counterexample from review, at the lineup level rather than the solver.

    WRRB takes a back or a receiver. FLEX also takes a tight end. Given a back worth 20
    and a tight end worth 19, filling slots in turn hands the back to FLEX, leaves WRRB
    with only an ineligible tight end, and scores 20. Both slots can be filled for 39.
    """
    rb = _player("The Back", "RB")
    te = _player("The End", "TE")
    league = League(slots={"FLEX": 1, "WRRB": 1})
    result = _build([rb, te], {"The Back": 20.0, "The End": 19.0}, league=league)

    assert result.unfilled == [], "a slot was left empty with an eligible player free"
    by_slot = {c.slot: c.starter.player.name for c in result.choices}
    assert by_slot == {"FLEX": "The End", "WRRB": "The Back"}
    assert sum(c.starter.projected_points for c in result.choices) == pytest.approx(39.0)


def test_the_best_player_can_be_moved_to_a_narrower_slot():
    """Total points beat giving the top scorer his most natural slot."""
    rb1 = _player("Big Back", "RB")
    rb2 = _player("Small Back", "RB")
    wr = _player("The Wideout", "WR")
    league = League(slots={"RB": 1, "WRRB": 1, "WR": 1})
    result = _build(
        [rb1, rb2, wr],
        {"Big Back": 30.0, "Small Back": 10.0, "The Wideout": 20.0},
        league=league,
    )

    assert result.unfilled == []
    assert sum(c.starter.projected_points for c in result.choices) == pytest.approx(60.0)
