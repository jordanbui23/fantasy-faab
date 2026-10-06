"""Price a waiver claim as the SWAP it really is: one player added, one dropped.

The waiver model finds players whose role just changed. That answers "who is getting more
work", which is a different question from "who should I pay for", and a different question
again from "who would I drop to make room".

A full roster makes the third question unavoidable. Ten starters and five bench players
leave no empty slot, so every claim costs a player. Pricing an add on its own overstates
every claim, because it silently assumes a sixteenth roster spot that does not exist.

So each candidate is priced as a swap. Solve the best lineup as it stands. Then, for each
player who could be dropped, solve it again with the candidate in and that player out. The
best of those is the claim's real value, and its distance from the baseline is what a bid
should be sized from.

That reuses the exact assignment in `model.assign`, so slot eligibility, byes, injuries and
positional depth are all handled by the code that already sets the lineup. Two consequences
follow without any rule being written for them. A third quarterback behind two good ones
scores zero, because no drop makes room for him to start. And a sixth running back who
cannot reach the flex scores zero too, however sharply his snaps rose.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from faab.league import League
from faab.model.lineup import Lineup, recommend_lineup
from faab.model.project import Projection
from faab.model.usage import Usage
from faab.names import Player

# Below this weekly gain a claim buys an option on an injury rather than points, and it is
# priced as one. Above it, the claim pays for itself in the lineup this week.
UPGRADE_THRESHOLD = 0.5

# Statuses that keep a player out entirely, making him the cheapest drop this week and the
# most expensive in the weeks after. Mirrors model.lineup, which owns the same question for
# start/sit, and is repeated here only because the reason is reported rather than applied.
UNAVAILABLE_STATUSES = frozenset({"IR", "OUT", "PUP", "SUS", "NA", "COV", "DNR"})

# Two projections this far apart are reporting different futures, not the same one with
# noise, and the report says so rather than averaging them into a single confident number.
# Calibrated against the measured spread: over 116 available players this project's own
# projection sits within about 2.5 points of Yahoo's on average, so a gap of 3 is outside
# ordinary disagreement.
DISPUTE_POINTS = 3.0


def conservative_points(model: float, market: float | None) -> float:
    """The lower of two independent projections.

    Used to price a claim, because a bid cannot be taken back. When the two disagree the
    cheaper reading is the one that survives being wrong: bidding low on a player who turns
    out good costs a player, while bidding high on a player who turns out bad costs the
    budget for every week that follows.
    """
    if market is None:
        return model
    return min(model, market)


@dataclass(frozen=True)
class Context:
    """Everything needed to re-solve a lineup, held once so a caller cannot vary it."""

    roster: list[Player]
    usage: dict[str, Usage]
    projections: dict[str, Projection]
    byes: set[str]
    week: int
    league: League
    through_week: int
    roster_size: int = 15
    market: dict[str, float] | None = None
    protected: frozenset[str] = frozenset()

    @property
    def is_full(self) -> bool:
        return len(self.roster) >= self.roster_size


@dataclass(frozen=True)
class Swap:
    """One claim, the player it costs, and what the pair is worth."""

    add: Player
    drop: Player | None
    gain: float
    add_points: float
    drop_points: float
    drop_was_starter: bool
    drop_blocked: str = ""
    model_points: float = 0.0
    market_points: float | None = None
    optimistic_gain: float = 0.0

    @property
    def disputed(self) -> bool:
        """Whether the two projections disagree enough to change the decision."""
        if self.market_points is None:
            return False
        return abs(self.model_points - self.market_points) >= DISPUTE_POINTS

    @property
    def is_upgrade(self) -> bool:
        return self.gain >= UPGRADE_THRESHOLD

    @property
    def free(self) -> bool:
        """True when the roster had room and the claim cost nobody."""
        return self.drop is None


def projected_total(lineup: Lineup) -> float:
    """Total projected points of the starters.

    An unrankable starter, meaning a team defense, contributes zero, because no per-player
    statistics exist to project one from. That zero is safe ONLY while the same defense
    appears in both lineups being compared, where it cancels.

    It is not safe when the defense is the player being dropped, and that is not a
    hypothetical: pricing swaps against a real roster, this function rated dropping the only
    team defense as free for all 116 candidates, because their projected zero was lost and the emptied
    DEF slot cost nothing either. `fills_every_slot` exists to reject exactly that, and this
    total must not be compared across lineups that differ in which slots are filled.
    """
    return sum(choice.starter.projected_points for choice in lineup.choices)


def fills_every_slot(lineup: Lineup, baseline_unfilled: int) -> bool:
    """Whether a lineup leaves no slot emptier than the roster already did.

    Yahoo permits an empty slot and scores it zero, so nothing stops a manager fielding one.
    That makes it a trap for a points comparison rather than an impossibility: an unfilled
    slot is invisible to `projected_total`, so dropping the only player at a position reads
    as costless. Requiring a swap to fill what the baseline filled closes that hole without
    needing a projection for a position that has none.
    """
    return len(lineup.unfilled) <= baseline_unfilled


def solve(context: Context, roster: list[Player]) -> Lineup:
    return recommend_lineup(
        roster,
        context.usage,
        context.projections,
        context.byes,
        context.week,
        context.league,
        through_week=context.through_week,
    )


def baseline_lineup(context: Context) -> Lineup:
    """The best lineup from the roster as it stands."""
    return solve(context, context.roster)


def baseline(context: Context) -> float:
    """The best total available from the roster as it stands."""
    return projected_total(baseline_lineup(context))


def player_points(context: Context, player: Player) -> float:
    """This project's own projection for a player."""
    projection = context.projections.get(player.gsis_id) if player.gsis_id else None
    return projection.points if projection else 0.0


def market_points(context: Context, player: Player) -> float | None:
    """Yahoo's projection for a player, when one was pasted."""
    if not context.market or not player.gsis_id:
        return None
    return context.market.get(player.gsis_id)


def starter_ids(context: Context, roster: list[Player]) -> set[str]:
    return {
        choice.starter.player.gsis_id
        for choice in solve(context, roster).choices
        if choice.starter.player.gsis_id
    }


def blocked_reason(context: Context, player: Player) -> str:
    """Why a player cannot play this week, if he cannot.

    Named in the report rather than folded into a number, because a bye is a cheap drop
    this week and an expensive one in every week after.
    """
    if player.team and player.team.upper() in context.byes:
        return f"{player.team} on bye"
    status = (player.injury_status or "").upper()
    return status if status in UNAVAILABLE_STATUSES else ""


def valued_at(context: Context, player: Player, points: float) -> Context:
    """A copy of `context` in which one player is projected at `points`.

    Used to re-price a single candidate without disturbing anyone else, so the same solver
    answers the same question against two different opinions of one player.
    """
    existing = context.projections.get(player.gsis_id) if player.gsis_id else None
    if existing is None:
        return context
    return replace(
        context,
        projections={**context.projections, player.gsis_id: replace(existing, points=points)},
    )


def best_swap(context: Context, candidate: Player, base: float | None = None) -> Swap:
    """The most valuable way to fit `candidate` onto the roster.

    Searched against the CONSERVATIVE valuation, meaning the lower of this project's own
    projection and Yahoo's, because that is the number a bid is safe to rest on. The gain
    under this project's own projection is reported alongside as `optimistic_gain`, so a
    disagreement is visible rather than resolved by whichever number happens to be used.

    Tries every possible drop and keeps the best. Gain is never negative, because declining
    the claim is always available: a swap that would lose points is reported as worth zero
    rather than recommended.

    Ties break toward dropping the lowest-projected player, so the report names the drop a
    reader would expect when two choices are worth the same.
    """
    start = baseline_lineup(context)
    if base is None:
        base = projected_total(start)
    baseline_unfilled = len(start.unfilled)
    held_starters = {
        choice.starter.player.gsis_id
        for choice in start.choices
        if choice.starter.player.gsis_id
    }

    model = player_points(context, candidate)
    market = market_points(context, candidate)
    priced = conservative_points(model, market)
    pricing = valued_at(context, candidate, priced)

    def gain_for(ctx: Context, roster: list[Player]) -> tuple[float, bool]:
        lineup = solve(ctx, roster)
        return max(0.0, projected_total(lineup) - base), fills_every_slot(
            lineup, baseline_unfilled
        )

    common = dict(
        add=candidate,
        add_points=priced,
        model_points=model,
        market_points=market,
    )

    if not context.is_full:
        gain, _ = gain_for(pricing, [*context.roster, candidate])
        optimistic, _ = gain_for(context, [*context.roster, candidate])
        return Swap(
            drop=None,
            gain=gain,
            drop_points=0.0,
            drop_was_starter=False,
            optimistic_gain=optimistic,
            **common,
        )

    best: Swap | None = None
    for held in context.roster:
        if held.gsis_id and held.gsis_id == candidate.gsis_id:
            continue
        if held.marker in context.protected:
            # A keeper carries next season's value, which no weekly gain can outbid.
            continue
        remaining = [p for p in context.roster if p is not held]
        gain, legal = gain_for(pricing, [*remaining, candidate])
        if not legal:
            # Dropping this player empties a slot nothing else can fill, which reads as
            # free to a points comparison and is not.
            continue
        optimistic, _ = gain_for(context, [*remaining, candidate])
        drop_points = player_points(context, held)
        swap = Swap(
            drop=held,
            gain=gain,
            drop_points=drop_points,
            drop_was_starter=bool(held.gsis_id) and held.gsis_id in held_starters,
            drop_blocked=blocked_reason(context, held),
            optimistic_gain=optimistic,
            **common,
        )
        if best is None or (swap.gain, -swap.drop_points) > (best.gain, -best.drop_points):
            best = swap

    if best is not None:
        return best
    return Swap(
        drop=None,
        gain=0.0,
        drop_points=0.0,
        drop_was_starter=False,
        optimistic_gain=0.0,
        **common,
    )


def best_per_position(swaps: list[Swap], limit: int = 2) -> list[Swap]:
    """Keep only the strongest claims at each position, preserving rank order.

    Twenty kickers who all beat the same weak starter are one finding, not twenty claims, and
    listing each of them buries every other position. `exclusive_gain` already prices them at
    a dollar; this stops them filling the page as well.
    """
    kept: list[Swap] = []
    seen: dict[str, int] = {}
    for swap in swaps:
        position = swap.add.position or "?"
        if seen.get(position, 0) >= limit:
            continue
        seen[position] = seen.get(position, 0) + 1
        kept.append(swap)
    return kept
