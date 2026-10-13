"""Tests for pricing a waiver claim as an add/drop swap.

The load-bearing case is the one that was wrong first: dropping the only player at a
position empties a slot, which a points comparison cannot see.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.league import League  # noqa: E402
from faab.model import need  # noqa: E402
from faab.model.project import Projection  # noqa: E402
from faab.model.usage import Usage  # noqa: E402
from faab.model import waiver  # noqa: E402
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


# --- what a bid pays for -----------------------------------------------------------


def test_a_fallback_is_a_lesser_claim_at_the_same_position():
    assert waiver.replacement_value([]) == 0.0
    assert waiver.replacement_value([5.0]) == 5.0
    assert waiver.replacement_value([3.0, 9.0, 7.0]) == 7.0


def test_a_deep_group_of_similar_claims_each_cost_a_dollar():
    """Losing one of five near-equal claims costs a point, not twenty."""
    values = [20.0, 19.5, 19.0, 18.8, 18.5]
    for value in values:
        others = [v for v in values if v != value]
        bid = waiver.bid_for_claim(value, waiver.replacement_value(others), 100, 5.0, [100] * 9)
        assert bid == 1


def test_a_unique_upgrade_earns_a_real_bid():
    assert 5 <= waiver.bid_for_claim(20.0, 0.0, 100, 5.0, [100] * 9) <= 12


def test_no_bid_may_exceed_the_share_cap():
    """A budget must survive the rest of the season, however large one gain looks."""
    assert waiver.bid_for_claim(500.0, 0.0, 100, 5.0, [100] * 9) <= 12


def test_a_claim_that_improves_nothing_costs_a_dollar():
    assert waiver.bid_for_claim(0.0, 0.0, 100, 5.0, [100] * 9) == 1


def test_a_zero_budget_bids_nothing():
    assert waiver.bid_for_claim(20.0, 0.0, 0, 5.0, [100] * 9) == 0


def test_one_good_week_is_worth_far_less_over_a_season():
    """A bye fill priced against one week took nine dollars for a kicker."""
    one_week = waiver.bid_for_claim(8.0, 0.0, 100, 1.0, [100] * 9)
    season = waiver.bid_for_claim(8.0, 0.0, 100, 5.7, [100] * 9)
    assert one_week == 12 and season < one_week / 1.5


def test_the_league_market_caps_a_bid():
    assert waiver.market_cap([0, 5]) is None
    assert waiver.market_cap([0, 0, 3]) == 4
    assert waiver.bid_for_claim(20.0, 0.0, 100, 5.0, [100] * 9, cap=4) == 4
    assert waiver.bid_for_claim(0.0, 0.0, 100, 5.0, [100] * 9, cap=4) == 1


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


# --- keepers are never the drop -----------------------------------------------------


def test_a_protected_player_is_never_the_drop():
    league = League(slots={"WR": 1})
    context = _context({("Starter", "WR"): 12.0, ("Keeper", "WR"): 3.0}, league=league)
    candidate = _add(context, _player("Better", "WR", team="BUF"), 15.0)
    keeper = next(p for p in context.roster if p.name == "Keeper")

    assert need.best_swap(context, candidate).drop == keeper
    swap = need.best_swap(replace(context, protected=frozenset({keeper.marker})), candidate)
    assert swap.drop is not None and swap.drop.name == "Starter"


def test_when_every_drop_is_protected_the_claim_is_worth_nothing():
    league = League(slots={"WR": 1})
    context = _context({("Keeper", "WR"): 3.0}, league=league)
    candidate = _add(context, _player("Better", "WR", team="BUF"), 15.0)
    keeper = context.roster[0]
    swap = need.best_swap(replace(context, protected=frozenset({keeper.marker})), candidate)
    assert swap.drop is None and swap.gain == 0.0 and not swap.is_upgrade



# --- a claim is valued over the rest of the season ---------------------------------


def _season(roster_points, *, horizon, byes_by_week=None, league=None, roster_size=None, long_run=None):
    """A context valued over `horizon`, the first week being the one being played."""
    context = _context(roster_points, league=league, roster_size=roster_size)
    return replace(
        context,
        week=horizon[0],
        horizon=tuple(horizon),
        byes=set((byes_by_week or {}).get(horizon[0], ())),
        byes_by_week=byes_by_week or {},
        long_run=long_run or {},
    )


def test_filling_a_bye_is_worth_that_week_only():
    league = League(slots={"K": 1})
    context = _season({("Leg", "K"): 8.0}, horizon=(5, 6, 7), byes_by_week={5: {"SEA"}},
                      league=league, roster_size=2)
    candidate = _add(context, _player("Stream", "K", team="BUF"), 7.0)
    swap = need.best_swap(context, candidate)
    assert swap.week_gain == pytest.approx(7.0)
    assert swap.gain == pytest.approx(7.0)


def test_a_better_player_gains_in_every_week():
    league = League(slots={"K": 1})
    context = _season({("Leg", "K"): 8.0}, horizon=(5, 6), league=league, roster_size=2)
    candidate = _add(context, _player("Boomer", "K", team="BUF"), 10.0)
    assert need.best_swap(context, candidate).gain == pytest.approx(2.0 + 2.0 * need.WEEKLY_DECAY)


def test_an_injured_starter_is_not_a_free_drop_over_the_season():
    """Priced on this week alone, a hurt starter scores zero and looks free to release."""
    league = League(slots={"WR": 3})
    roster = [_player("Star Hurt", "WR", status="OUT"), _player("Steady", "WR"), _player("Spare", "WR")]
    projections = {p.gsis_id: _projection(p, pts) for p, pts in zip(roster, (14.0, 9.0, 4.0))}
    base = need.Context(roster=roster, usage={}, projections=projections, byes=set(), week=5,
                        league=league, through_week=4, roster_size=3)
    candidate = _add(base, _player("Waiver Guy", "WR", team="BUF"), 6.0)

    assert need.best_swap(base, candidate).drop.name == "Star Hurt"
    season = replace(base, horizon=(5, 6, 7, 8))
    assert need.best_swap(season, candidate).drop.name == "Spare"


def test_a_long_term_status_holds_for_several_weeks():
    context = replace(_context({("Hurt", "WR"): 9.0}), week=5, horizon=tuple(range(5, 12)))
    held = replace(context.roster[0], injury_status="IR")
    short = replace(held, injury_status="OUT")
    out_weeks = [w for w in context.weeks if need.status_in(context, held, w)]
    assert out_weeks == list(range(5, 5 + need.LONG_TERM_WEEKS))
    assert [w for w in context.weeks if need.status_in(context, short, w)] == [5]


def test_a_questionable_starter_counts_at_his_odds_of_playing():
    league = League(slots={"QB": 1})
    context = _context({("Shaky", "QB"): 20.0}, league=league, roster_size=2)
    shaky = replace(context.roster[0], injury_status="QUESTIONABLE")
    context = replace(context, roster=[shaky])
    assert need.baseline(context).this_week == pytest.approx(20.0 * need.PLAY_ODDS["QUESTIONABLE"])

    backup = _add(context, _player("Backup", "QB", team="BUF"), 10.0)
    swap = need.best_swap(context, backup)
    assert swap.gain == pytest.approx((1 - need.PLAY_ODDS["QUESTIONABLE"]) * 10.0)


def test_a_recent_role_drifts_back_to_the_long_run_one():
    context = _season({("Fill In", "RB"): 12.0}, horizon=(5, 6, 7))
    gsis = context.roster[0].gsis_id
    context = replace(context, long_run={gsis: replace(context.projections[gsis], points=4.0)})
    assert context.projections_for(5)[gsis].points == pytest.approx(12.0)
    keep = need.ROLE_PERSISTENCE ** 2
    assert context.projections_for(7)[gsis].points == pytest.approx(keep * 12.0 + (1 - keep) * 4.0)
    expected = sum(
        need.WEEKLY_DECAY ** k * (need.ROLE_PERSISTENCE ** k * 12.0 + (1 - need.ROLE_PERSISTENCE ** k) * 4.0)
        for k in range(3)
    )
    assert need.season_value(context, context.roster).value == pytest.approx(expected)


def test_pruned_drop_search_matches_trying_every_drop():
    """The drop search stops early on a bound; it must still find the same best drop."""
    league = League(slots={"QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "K": 1})
    points = {
        ("Q One", "QB"): 18.0, ("R One", "RB"): 14.0, ("R Two", "RB"): 11.0,
        ("R Three", "RB"): 9.5, ("W One", "WR"): 13.0, ("W Two", "WR"): 10.0,
        ("W Three", "WR"): 7.0, ("K One", "K"): 8.0, ("Q Two", "QB"): 12.0,
    }
    context = _season(points, horizon=tuple(range(5, 11)), league=league,
                      byes_by_week={6: {"SEA"}, 8: {"BUF"}})
    teams = {"R Two": "BUF", "W Three": "BUF", "Q Two": "BUF"}
    context = replace(context, roster=[replace(p, team=teams.get(p.name, p.team)) for p in context.roster])
    start = need.baseline(context)
    for name, position, pts in (("New Back", "RB", 10.5), ("New Wide", "WR", 9.0),
                                ("New Kick", "K", 8.5), ("Long Shot", "WR", 3.0)):
        candidate = _add(context, _player(name, position, team="NYJ"), pts)
        swap = need.best_swap(context, candidate)
        exhaustive = max(
            (
                max(0.0, need.season_value(context, [*(p for p in context.roster if p is not held), candidate]).value - start.value),
                -need.player_points(context, held),
                held.name,
            )
            for held in context.roster
        )
        assert swap.gain == pytest.approx(exhaustive[0])
        if exhaustive[0] > 0:
            assert swap.drop.name == exhaustive[2]


def test_open_positions_name_this_weeks_holes():
    league = League(slots={"QB": 1, "WR": 1})
    context = _context({("Wideout", "WR"): 9.0}, league=league, roster_size=3)
    assert need.open_positions(context, ("QB", "WR", "K")) == {"QB"}
    shaky = replace(context.roster[0], injury_status="DOUBTFUL")
    qb = _player("Signal", "QB")
    context = replace(context, roster=[shaky, qb], projections={**context.projections, qb.gsis_id: _projection(qb, 15.0)})
    assert need.open_positions(context, ("QB", "WR", "K")) == {"WR"}


def test_a_lower_market_number_holds_in_later_weeks_too():
    league = League(slots={"WR": 1})
    context = _season({("Held", "WR"): 2.0}, horizon=(5, 6), league=league, roster_size=2)
    context = replace(context, market={})
    held = context.roster[0]
    candidate = _add(context, _player("Hyped", "WR", team="BUF"), 10.0, market=6.0)
    context.market[candidate.gsis_id] = 6.0
    context = replace(
        context,
        long_run={held.gsis_id: context.projections[held.gsis_id],
                  candidate.gsis_id: context.projections[candidate.gsis_id]},
    )
    swap = need.best_swap(context, candidate)
    assert swap.gain == pytest.approx(4.0 + need.WEEKLY_DECAY * 4.0)
    assert swap.optimistic_gain == pytest.approx(8.0 + need.WEEKLY_DECAY * 8.0)
