"""Tests for pricing a waiver claim as an add/drop swap.

The load-bearing case is the one that was wrong first: dropping the only player at a
position empties a slot, which a points comparison cannot see.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.league import League  # noqa: E402
from faab.model import need  # noqa: E402
from faab.model.project import Projection  # noqa: E402
from faab.model.usage import Usage  # noqa: E402
from faab.model.waiver import bid_for_swap, exclusive_gain  # noqa: E402
from faab.names import Player  # noqa: E402


def _player(name, position, team="SEA", status=""):
    return Player(
        gsis_id=name.lower().replace(" ", "-"),
        name=name,
        position=position,
        team=team,
        status="ACT",
        injury_status=status,
    )


def _projection(player, points):
    return Projection(
        gsis_id=player.gsis_id,
        name=player.name,
        position=player.position,
        team=player.team,
        points=points,
        games_played=3,
    )


def _context(roster_points, league=None, market=None, roster_size=None, byes=()):
    """Build a context from a mapping of (name, position) to projected points."""
    roster = [_player(name, position) for name, position in roster_points]
    projections = {
        p.gsis_id: _projection(p, roster_points[(p.name, p.position)]) for p in roster
    }
    resolved = league or League(slots={"QB": 1, "WR": 1, "FLEX": 1})
    return need.Context(
        roster=roster,
        usage={},
        projections=projections,
        byes=set(byes),
        week=4,
        league=resolved,
        through_week=3,
        roster_size=roster_size if roster_size is not None else len(roster),
        market=market,
    )


def _add(context, player, points, market=None):
    """Register a candidate in the context's projections and return him."""
    context.projections[player.gsis_id] = _projection(player, points)
    if market is not None and context.market is not None:
        context.market[player.gsis_id] = market
    return player


# --- a full roster makes every claim a swap ------------------------------------


def test_a_roster_with_room_costs_nobody():
    context = _context(
        {("Starter", "QB"): 20.0, ("Wideout", "WR"): 10.0}, roster_size=5
    )
    candidate = _add(context, _player("Better", "WR"), 15.0)
    swap = need.best_swap(context, candidate)
    assert swap.free
    assert swap.drop is None
    assert swap.gain > 0


def test_a_full_roster_names_the_player_it_costs():
    context = _context(
        {("Starter", "QB"): 20.0, ("Weak", "WR"): 4.0, ("Spare", "WR"): 5.0}
    )
    candidate = _add(context, _player("Better", "WR"), 15.0)
    swap = need.best_swap(context, candidate)
    assert not swap.free
    assert swap.drop is not None
    assert swap.gain > 0


def test_the_weakest_player_is_the_one_dropped():
    context = _context(
        {("Starter", "QB"): 20.0, ("Weak", "WR"): 2.0, ("Spare", "WR"): 9.0}
    )
    candidate = _add(context, _player("Better", "WR"), 15.0)
    swap = need.best_swap(context, candidate)
    assert swap.drop.name == "Weak"


# --- the bug that was found against a real roster ------------------------------


def test_dropping_the_only_player_at_a_slot_is_refused():
    """Emptying a slot is invisible to a points total, so it must be rejected outright.

    Priced against a real roster, every one of 116 candidates chose to drop the only team
    defense, because its projected zero was lost and the emptied DEF slot cost nothing.
    """
    league = League(slots={"WR": 1, "DEF": 1})
    context = _context({("Wideout", "WR"): 8.0, ("Hawks", "DEF"): 0.0}, league=league)
    candidate = _add(context, _player("Better", "WR"), 20.0)
    swap = need.best_swap(context, candidate)
    assert swap.drop is not None
    assert swap.drop.name != "Hawks", "the defense was dropped and the DEF slot emptied"


def test_a_slot_with_cover_can_still_have_a_player_dropped():
    league = League(slots={"WR": 1, "DEF": 1})
    context = _context(
        {("Wideout", "WR"): 8.0, ("Hawks", "DEF"): 0.0, ("Jets", "DEF"): 0.0},
        league=league,
    )
    candidate = _add(context, _player("Better", "WR"), 20.0)
    swap = need.best_swap(context, candidate)
    assert swap.gain > 0


# --- positional depth needs no rule of its own --------------------------------


def test_a_third_quarterback_behind_two_good_ones_gains_nothing():
    """No drop makes room for him to start, so the swap search finds zero."""
    league = League(slots={"QB": 1, "WR": 1})
    context = _context(
        {("Ace", "QB"): 22.0, ("Backup", "QB"): 20.0, ("Wideout", "WR"): 9.0},
        league=league,
    )
    candidate = _add(context, _player("Third", "QB"), 18.0)
    swap = need.best_swap(context, candidate)
    assert swap.gain == pytest.approx(0.0)
    assert not swap.is_upgrade


def test_a_quarterback_who_beats_the_incumbent_does_gain():
    league = League(slots={"QB": 1, "WR": 1})
    context = _context({("Ace", "QB"): 15.0, ("Wideout", "WR"): 9.0}, league=league)
    candidate = _add(context, _player("Elite", "QB"), 25.0)
    swap = need.best_swap(context, candidate)
    assert swap.gain > 0
    assert swap.is_upgrade


def test_a_worse_player_is_never_a_recommended_swap():
    league = League(slots={"WR": 1})
    context = _context({("Wideout", "WR"): 15.0, ("Spare", "WR"): 9.0}, league=league)
    candidate = _add(context, _player("Worse", "WR"), 2.0)
    swap = need.best_swap(context, candidate)
    assert swap.gain == pytest.approx(0.0)


# --- the conservative valuation ------------------------------------------------


def test_the_lower_projection_is_used_to_price():
    assert need.conservative_points(12.0, 5.0) == pytest.approx(5.0)
    assert need.conservative_points(5.0, 12.0) == pytest.approx(5.0)


def test_a_missing_market_number_falls_back_to_the_model():
    assert need.conservative_points(9.0, None) == pytest.approx(9.0)


def test_a_disagreement_is_flagged_and_both_numbers_kept():
    league = League(slots={"WR": 1})
    context = _context(
        {("Wideout", "WR"): 5.0, ("Spare", "WR"): 4.0}, league=league, market={}
    )
    candidate = _add(context, _player("Disputed", "WR"), 14.0, market=6.0)
    swap = need.best_swap(context, candidate)
    assert swap.disputed
    assert swap.model_points == pytest.approx(14.0)
    assert swap.market_points == pytest.approx(6.0)
    assert swap.optimistic_gain > swap.gain


def test_close_projections_are_not_flagged_as_disputed():
    league = League(slots={"WR": 1})
    context = _context(
        {("Wideout", "WR"): 5.0, ("Spare", "WR"): 4.0}, league=league, market={}
    )
    candidate = _add(context, _player("Agreed", "WR"), 10.0, market=9.0)
    swap = need.best_swap(context, candidate)
    assert not swap.disputed


# --- what a claim exclusively adds --------------------------------------------


def test_a_claim_with_an_identical_alternative_is_worth_nothing_extra():
    assert exclusive_gain(3.8, [3.8, 3.5]) == pytest.approx(0.0)


def test_a_claim_with_no_alternative_keeps_its_whole_gain():
    assert exclusive_gain(3.8, []) == pytest.approx(3.8)


def test_exclusive_gain_is_the_margin_over_the_next_best():
    assert exclusive_gain(5.0, [1.0, 0.5]) == pytest.approx(4.0)


def test_nine_similar_claims_each_cost_a_dollar():
    """Winning any one of a deep group is worth almost nothing, whatever its raw gain."""
    gains = [3.82, 3.54, 2.63, 2.33, 2.29]
    for gain in gains:
        others = [g for g in gains if g != gain]
        assert bid_for_swap(gain, others, 100, [100] * 9) == 1


def test_a_unique_upgrade_earns_a_real_bid():
    bid = bid_for_swap(5.0, [0.4, 0.2], 100, [100] * 9)
    assert 5 <= bid <= 12


def test_no_bid_may_exceed_the_share_cap():
    """A budget must survive the rest of the season, however large one gain looks."""
    bid = bid_for_swap(500.0, [], 100, [100] * 9)
    assert bid <= 12


def test_a_claim_that_improves_nothing_costs_a_dollar():
    assert bid_for_swap(0.0, [1.0], 100, [100] * 9) == 1


def test_a_zero_budget_bids_nothing():
    assert bid_for_swap(5.0, [], 0, [100] * 9) == 0


# --- readability ---------------------------------------------------------------


def test_only_the_best_claims_at_each_position_survive():
    swaps = [
        need.Swap(add=_player(f"K{i}", "K"), drop=None, gain=5.0 - i, add_points=0,
                  drop_points=0, drop_was_starter=False)
        for i in range(5)
    ]
    kept = need.best_per_position(swaps, limit=2)
    assert [s.add.name for s in kept] == ["K0", "K1"]


def test_capping_one_position_does_not_hide_another():
    swaps = [
        need.Swap(add=_player("K1", "K"), drop=None, gain=5.0, add_points=0,
                  drop_points=0, drop_was_starter=False),
        need.Swap(add=_player("K2", "K"), drop=None, gain=4.0, add_points=0,
                  drop_points=0, drop_was_starter=False),
        need.Swap(add=_player("K3", "K"), drop=None, gain=3.0, add_points=0,
                  drop_points=0, drop_was_starter=False),
        need.Swap(add=_player("W1", "WR"), drop=None, gain=1.0, add_points=0,
                  drop_points=0, drop_was_starter=False),
    ]
    kept = need.best_per_position(swaps, limit=2)
    assert "W1" in [s.add.name for s in kept]


def test_a_blocked_drop_is_named_rather_than_hidden():
    league = League(slots={"WR": 1})
    context = _context(
        {("Wideout", "WR"): 9.0, ("Benched", "WR"): 8.0},
        league=league,
        byes=("SEA",),
    )
    candidate = _add(context, _player("Better", "WR", team="BUF"), 15.0)
    swap = need.best_swap(context, candidate)
    assert swap.drop is not None
    assert "bye" in swap.drop_blocked
