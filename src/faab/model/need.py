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

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

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

# The odds a player with each designation plays. Sourced in docs/RESEARCH.md section 7i.
PLAY_ODDS = {"QUESTIONABLE": 0.70, "DOUBTFUL": 0.05}

# Statuses that keep a player out for several games rather than one, held for
# LONG_TERM_WEEKS. Section 7i of docs/RESEARCH.md has the injured-reserve minimum.
LONG_TERM_STATUSES = frozenset({"IR", "PUP", "NA", "SUS"})
LONG_TERM_WEEKS = 4

# Each week ahead counts this much less than the one before. Roles revert and injured
# starters come back, so a projection drawn from recent games describes the next game best.
WEEKLY_DECAY = 0.85

# How much of a recent role survives each week ahead; the rest drifts back to the player's
# season-long role. A back who took over for an injured starter keeps the job only until
# the starter returns, and the same projection must not be paid for in every later week.
ROLE_PERSISTENCE = 0.75


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
    horizon: tuple[int, ...] = ()
    byes_by_week: Mapping[int, set[str]] = field(default_factory=dict)
    decay: float = WEEKLY_DECAY
    long_run: Mapping[str, Projection] = field(default_factory=dict)
    persistence: float = ROLE_PERSISTENCE
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    @property
    def is_full(self) -> bool:
        return len(self.roster) >= self.roster_size

    @property
    def weeks(self) -> tuple[int, ...]:
        """The weeks a claim is valued over. An empty horizon means this week alone."""
        return self.horizon or (self.week,)

    @property
    def effective_weeks(self) -> float:
        """The horizon's total weight, in this-week equivalents."""
        return sum(self.weight(week) for week in self.weeks)

    def weight(self, week: int) -> float:
        return self.decay ** max(0, week - self.week)

    def projections_for(self, week: int) -> Mapping[str, Projection]:
        """This week's projections, blended toward the long-run role for later weeks."""
        if week <= self.week or not self.long_run:
            return self.projections
        key = ("blend", week)
        if key not in self._cache:
            keep = self.persistence ** (week - self.week)
            self._cache[key] = _Blend(self.projections, self.long_run, keep)
        return self._cache[key]

    def byes_in(self, week: int) -> set[str]:
        if week == self.week:
            return self.byes
        return set(self.byes_by_week.get(week, ()))


class _Blend(Mapping):
    """Projections blended on lookup, since a lineup reads a few dozen of several hundred."""

    def __init__(
        self, recent: Mapping[str, Projection], long_run: Mapping[str, Projection], keep: float
    ) -> None:
        self._recent, self._long_run, self._keep = recent, long_run, keep
        self._memo: dict[str, Projection] = {}

    def __getitem__(self, gsis_id: str) -> Projection:
        if gsis_id not in self._memo:
            projection = self._recent[gsis_id]
            settled = self._long_run.get(gsis_id)
            self._memo[gsis_id] = (
                projection
                if settled is None
                else replace(
                    projection,
                    points=self._keep * projection.points + (1.0 - self._keep) * settled.points,
                )
            )
        return self._memo[gsis_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._recent)

    def __len__(self) -> int:
        return len(self._recent)


@dataclass(frozen=True)
class SeasonValue:
    """A roster's value over the horizon, and the parts a swap is checked against."""

    value: float
    this_week: float
    unfilled: tuple[int, ...]


@dataclass(frozen=True)
class Swap:
    """One claim, the player it costs, and what the pair is worth.

    `gain` is the weighted points the pair adds over the whole horizon. `week_gain` is this
    week's part of it, which can be negative when a claim pays off later.
    """

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
    week_gain: float = 0.0

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
    DEF slot cost nothing either. `best_swap` rejects exactly that by comparing empty slots
    week by week, and this total must not be compared across lineups that differ in them.
    """
    return sum(choice.starter.projected_points for choice in lineup.choices)


def solve(context: Context, roster: list[Player], week: int | None = None) -> Lineup:
    target = context.week if week is None else week
    return recommend_lineup(
        roster,
        context.usage,
        context.projections_for(target),
        context.byes_in(target),
        target,
        context.league,
        through_week=context.through_week,
    )


def status_in(context: Context, player: Player, week: int) -> str:
    """The designation that still applies in `week`.

    This week's report applies in full. A long-term status holds for LONG_TERM_WEEKS, and
    anything shorter is assumed cleared by the following week.
    """
    status = (player.injury_status or "").upper()
    if week == context.week:
        return status
    if status in LONG_TERM_STATUSES and week < context.week + LONG_TERM_WEEKS:
        return status
    return ""


def _as_of(context: Context, roster: list[Player], week: int) -> list[Player]:
    if week == context.week:
        return roster
    return [
        player
        if status_in(context, player, week) == (player.injury_status or "").upper()
        else replace(player, injury_status=status_in(context, player, week))
        for player in roster
    ]


def _week_state(context: Context, roster: list[Player], week: int) -> frozenset[str]:
    """Who cannot play in `week`. Two later weeks with the same answer score the same."""
    byes = {team.upper() for team in context.byes_in(week)}
    return frozenset(
        player.marker
        for player in roster
        if (player.team and player.team.upper() in byes)
        or status_in(context, player, week) in UNAVAILABLE_STATUSES
    )


def _week_value(context: Context, roster: list[Player], week: int) -> tuple[float, int]:
    """Expected points from the best lineup in `week`, and how many slots it leaves empty.

    A starter listed questionable or doubtful this week is weighed by his odds of playing:
    the lineup loses, in proportion, what replacing him from the bench would cost. Each
    one is treated on his own, which is close enough for the two or three a roster carries.
    """
    players = _as_of(context, roster, week)
    lineup = solve(context, players, week)
    full = projected_total(lineup)
    expected = full
    if week == context.week:
        for choice in lineup.choices:
            starter = choice.starter.player
            odds = PLAY_ODDS.get((starter.injury_status or "").upper())
            if odds is None:
                continue
            without = projected_total(solve(context, [p for p in players if p is not starter], week))
            expected -= (1.0 - odds) * max(0.0, full - without)
    return expected, len(lineup.unfilled)


def season_value(context: Context, roster: list[Player]) -> SeasonValue:
    """The roster's weighted points over the horizon."""
    seen: dict[tuple[frozenset[str], int], tuple[float, int]] = {}
    value = this_week = 0.0
    unfilled: list[int] = []
    for week in context.weeks:
        if week == context.week:
            points, empty = _week_value(context, roster, week)
            this_week = points
        else:
            # Without a long-run role every later week projects alike, so weeks with the same
            # absences can share one solve.
            key = (_week_state(context, roster, week), week if context.long_run else 0)
            if key not in seen:
                seen[key] = _week_value(context, roster, week)
            points, empty = seen[key]
        value += context.weight(week) * points
        unfilled.append(empty)
    return SeasonValue(value=value, this_week=this_week, unfilled=tuple(unfilled))


def baseline_lineup(context: Context) -> Lineup:
    """The best lineup from the roster as it stands."""
    return solve(context, context.roster)


def baseline(context: Context) -> SeasonValue:
    """The roster's value as it stands, over the horizon."""
    return season_value(context, context.roster)


def open_positions(context: Context, positions: tuple[str, ...]) -> set[str]:
    """Positions this week's lineup has a hole at: an empty slot, or a starter who may sit."""
    lineup = baseline_lineup(context)
    needed = {
        position
        for slot in lineup.unfilled
        for position in positions
        if context.league.accepts(slot, position)
    }
    for choice in lineup.choices:
        if (choice.starter.player.injury_status or "").upper() in PLAY_ODDS:
            needed.add(choice.starter.player.position)
    return needed & set(positions)


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
    answers the same question against two different opinions of one player. His long-run
    role is scaled by the same ratio, so a lower opinion holds in later weeks too rather
    than drifting back to the higher one.
    """
    existing = context.projections.get(player.gsis_id) if player.gsis_id else None
    if existing is None or points == existing.points:
        return context
    long_run = context.long_run
    settled = long_run.get(player.gsis_id)
    if settled is not None:
        ratio = points / existing.points if existing.points > 0 else 0.0
        long_run = {**long_run, player.gsis_id: replace(settled, points=settled.points * ratio)}
    return replace(
        context,
        projections={**context.projections, player.gsis_id: replace(existing, points=points)},
        long_run=long_run,
    )


def _losses(context: Context, start: SeasonValue) -> list[tuple[float, Player]]:
    """What dropping each unprotected player costs the roster, cheapest first.

    Computed once per context, because it does not depend on who is being added.
    A keeper is left out, since he carries next season's value no weekly gain can outbid.
    """
    if "losses" not in context._cache:
        rows = [
            (
                max(0.0, start.value - season_value(context, [p for p in context.roster if p is not held]).value),
                held,
            )
            for held in context.roster
            if held.marker not in context.protected
        ]
        rows.sort(key=lambda row: row[0])
        context._cache["losses"] = rows
    return context._cache["losses"]


def _ceiling(context: Context, candidate: Player) -> float:
    """The most adding `candidate` could ever be worth: starting him every week he plays.

    A lineup that gains him loses at most his points once he is removed again, so this
    bounds the gain. This week it is scaled by the starters who may sit, because he can
    stand in for each of them.
    """
    stand_ins = sum(
        1.0 - PLAY_ODDS[status]
        for player in context.roster
        if (status := (player.injury_status or "").upper()) in PLAY_ODDS
    )
    byes_cache: dict[int, set[str]] = {}
    total = 0.0
    for week in context.weeks:
        byes = byes_cache.setdefault(week, {team.upper() for team in context.byes_in(week)})
        if (candidate.team and candidate.team.upper() in byes) or status_in(
            context, candidate, week
        ) in UNAVAILABLE_STATUSES:
            continue
        projection = context.projections_for(week).get(candidate.gsis_id or "")
        points = projection.points if projection else 0.0
        total += context.weight(week) * points * (max(1.0, stand_ins) if week == context.week else 1.0)
    return total


def best_swap(context: Context, candidate: Player, base: SeasonValue | None = None) -> Swap:
    """The most valuable way to fit `candidate` onto the roster, over the whole horizon.

    Searched against the CONSERVATIVE valuation, meaning the lower of this project's own
    projection and Yahoo's, because that is the number a bid is safe to rest on. The gain
    under this project's own projection is reported alongside as `optimistic_gain`, so a
    disagreement is visible rather than resolved by whichever number happens to be used.

    Tries every possible drop and keeps the best. Gain is never negative, because declining
    the claim is always available: a swap that would lose points is reported as worth zero
    rather than recommended. A drop that leaves any week with a slot the roster could have
    filled is refused, since an empty slot reads as free to a points total.

    Ties break toward dropping the lowest-projected player, so the report names the drop a
    reader would expect when two choices are worth the same.
    """
    start = base if base is not None else baseline(context)
    held_starters = starter_ids(context, context.roster)

    model = player_points(context, candidate)
    market = market_points(context, candidate)
    priced = conservative_points(model, market)
    pricing = valued_at(context, candidate, priced)

    def evaluate(ctx: Context, roster: list[Player]) -> tuple[SeasonValue, bool]:
        season = season_value(ctx, roster)
        legal = all(after <= before for after, before in zip(season.unfilled, start.unfilled))
        return season, legal

    def optimistic_for(roster: list[Player], conservative: float) -> float:
        if market is None or priced == model:
            return conservative
        season, _ = evaluate(context, roster)
        return max(0.0, season.value - start.value)

    common = dict(
        add=candidate,
        add_points=priced,
        model_points=model,
        market_points=market,
    )

    if not context.is_full:
        roster = [*context.roster, candidate]
        season, _ = evaluate(pricing, roster)
        gain = max(0.0, season.value - start.value)
        return Swap(
            drop=None,
            gain=gain,
            drop_points=0.0,
            drop_was_starter=False,
            optimistic_gain=optimistic_for(roster, gain),
            week_gain=season.this_week - start.this_week,
            **common,
        )

    best: Swap | None = None
    ceiling = _ceiling(pricing, candidate)
    for loss, held in _losses(context, start):
        if held.gsis_id and held.gsis_id == candidate.gsis_id:
            continue
        if best is not None and ceiling - loss < best.gain:
            # Drops are in rising order of cost, so no later one can do better either.
            break
        roster = [*(p for p in context.roster if p is not held), candidate]
        season, legal = evaluate(pricing, roster)
        if not legal:
            continue
        gain = max(0.0, season.value - start.value)
        drop_points = player_points(context, held)
        swap = Swap(
            drop=held,
            gain=gain,
            drop_points=drop_points,
            drop_was_starter=bool(held.gsis_id) and held.gsis_id in held_starters,
            drop_blocked=blocked_reason(context, held),
            week_gain=season.this_week - start.this_week,
            **common,
        )
        if best is None or (swap.gain, -swap.drop_points) > (best.gain, -best.drop_points):
            best = swap

    if best is not None:
        assert best.drop is not None
        roster = [*(p for p in context.roster if p is not best.drop), candidate]
        return replace(best, optimistic_gain=optimistic_for(roster, best.gain))
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
    listing each of them buries every other position. Their bids are already a dollar against
    each other; this stops them filling the page as well.
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
