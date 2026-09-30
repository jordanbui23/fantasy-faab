"""Project points for the coming week from usage, not from points already scored.

The reports used to rank on a recency-weighted average of points a player had ALREADY
scored. Checked against Yahoo's own projections on a real roster, that metric agreed on
two contested lineup decisions and got two wrong, and the pattern was clear: it works
where a role changed and fails where two good players differ by luck rather than usage.

Touchdowns are the luck. Early in 2026, two quarterbacks threw for nearly identical
yardage, while one scored passing touchdowns well above the league rate and the other well
below it. Ranking on points scored therefore preferred the first by a wide margin, for play
that was nearly identical.

So this module separates the two things a points total mixes together. VOLUME, meaning
attempts, carries and targets, is a real property of a player's role and carries over to
next week. EFFICIENCY per opportunity is partly skill and mostly noise in a short sample,
so it is pulled toward the league rate by an amount that depends on how much has been
seen. Touchdown rate gets the heaviest pull of all, because it is the noisiest term and
the one that most distorts a ranking.

Rates are calibrated from the season being played rather than hardcoded, so no rate can
silently go stale. The constants in `league_rates` are fallbacks for a zero denominator,
which happens for an empty season or a small fixture and never in a real week.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from faab.collectors.nflverse import as_float

# Recency weights over the games a player has actually played, most recent first. Volume
# moves when a role changes, so the latest game counts most.
VOLUME_WEIGHTS = (3.0, 2.0, 1.0)

# Shrinkage priors, in opportunities. A player's own rate is trusted in proportion to how
# many opportunities back it.
#
# The two priors split by how rare the event is, not by what it is called. EFFICIENCY_PRIOR
# governs quantities that happen on most opportunities, meaning yards per carry, yards per
# target, yards per attempt and catch rate. RARE_EVENT_PRIOR governs quantities that happen
# on a few percent of them, meaning every touchdown rate and the interception rate. A rare
# event needs far more opportunities before its observed rate means anything, and that is
# the only reason there are two numbers here.
#
# The Beta-Binomial argument below applies to the rare events, which are proportions. Yards
# per opportunity is a continuous mean and the same reasoning does not transfer, so its
# prior rests on the grid search alone.
#
# Two independent arguments put the touchdown prior in the hundreds, not the dozens.
#
# Treating touchdowns as Beta-Binomial, the prior strength that matches the real spread
# between players is p(1-p)/variance. Rushing touchdown rate averages about 0.03 per carry
# and the between-player spread is roughly 0.01, which gives 0.03 * 0.97 / 0.0001, close to
# 290. A rate observed over 40 carries carries almost no information next to that.
#
# Measured against Yahoo's own projections for 13 players, mean absolute error fell
# monotonically as the rare-event prior rose, from 2.14 at 75 to 1.92 at 400 and 1.91 at
# 600, while an efficiency prior of 25 beat every heavier value. Reproduce with the grid in
# docs/RESEARCH.md section 7c. 400 is chosen over 600 because it captures nearly all of the
# gain without sitting at the edge of the range that was searched.
#
# That error figure is IN-SAMPLE and must not be quoted as accuracy. The same 13 players
# chose the prior and then scored it, so it measures fit rather than prediction. Treat it as
# weak evidence for the order of magnitude, and re-check it against a week these priors
# never saw.
#
# The practical consequence is deliberate: at 20 carries a game, a back needs most of a
# season before his own touchdown rate outweighs the league's. Until then this projection is
# close to volume times efficiency, which is the intended behaviour.
EFFICIENCY_PRIOR = 25.0
RARE_EVENT_PRIOR = 400.0

KICKER_POSITION = "K"
UNPROJECTABLE_POSITIONS = frozenset({"DEF", "DST"})

# Yahoo's default distance scoring, also used by model.usage for the same reason: the
# nflverse release reports zero PPR points for every kicker.
KICKER_BUCKETS: dict[str, float] = {
    "fg_made_0_19": 3.0,
    "fg_made_20_29": 3.0,
    "fg_made_30_39": 3.0,
    "fg_made_40_49": 4.0,
    "fg_made_50_59": 5.0,
    "fg_made_60_": 5.0,
    "pat_made": 1.0,
}


@dataclass(frozen=True)
class Scoring:
    """League scoring. Defaults match nflverse's `fantasy_points_ppr` on the common terms.

    Every regular-season QB, RB, WR and TE line without a two-point conversion or a return
    touchdown reproduces the published value within 0.01 under these values.
    `test_live_default_scoring_reproduces_fantasy_points_ppr` in tests/test_nflverse.py
    checks that against the live release.

    Two-point conversions and return touchdowns are not modelled, so a line holding either
    will not reproduce exactly. Both are rare, and neither is projectable from volume.
    """

    pass_yards: float = 0.04
    pass_td: float = 4.0
    interception: float = -2.0
    rush_yards: float = 0.1
    rush_td: float = 6.0
    reception: float = 1.0
    rec_yards: float = 0.1
    rec_td: float = 6.0
    fumble_lost: float = -2.0


@dataclass(frozen=True)
class LeagueRates:
    """Per-opportunity rates for the season being played.

    These are what a player's own rates are pulled toward. Calibrating them from the
    season rather than hardcoding them means the model cannot go stale against a rule
    change or a scoring environment shift.
    """

    yards_per_attempt: float
    pass_td_per_attempt: float
    interceptions_per_attempt: float
    yards_per_carry: float
    rush_td_per_carry: float
    catch_rate: float
    yards_per_target: float
    rec_td_per_target: float


def league_rates(stats: list[dict[str, str]]) -> LeagueRates:
    """Calibrate per-opportunity rates from every row in the season."""

    def total(column: str) -> float:
        return sum(as_float(row.get(column)) for row in stats)

    attempts = total("attempts")
    carries = total("carries")
    targets = total("targets")

    def rate(numerator: str, denominator: float, fallback: float) -> float:
        return total(numerator) / denominator if denominator > 0 else fallback

    return LeagueRates(
        yards_per_attempt=rate("passing_yards", attempts, 7.0),
        pass_td_per_attempt=rate("passing_tds", attempts, 0.05),
        interceptions_per_attempt=rate("passing_interceptions", attempts, 0.025),
        yards_per_carry=rate("rushing_yards", carries, 4.2),
        rush_td_per_carry=rate("rushing_tds", carries, 0.03),
        catch_rate=rate("receptions", targets, 0.65),
        yards_per_target=rate("receiving_yards", targets, 7.5),
        rec_td_per_target=rate("receiving_tds", targets, 0.05),
    )


def shrink(total: float, opportunities: float, league_rate: float, prior: float) -> float:
    """Blend a player's own rate toward the league rate, by how much has been seen.

    With no opportunities this returns the league rate exactly. As opportunities grow past
    the prior, it approaches the player's own rate. This is the whole mechanism by which a
    two-game touchdown streak stops dominating a ranking.
    """
    if prior <= 0:
        return total / opportunities if opportunities > 0 else league_rate
    return (total + prior * league_rate) / (opportunities + prior)


@dataclass
class PlayerSeason:
    """One player's season totals and per-game volume, assembled from weekly rows."""

    gsis_id: str
    name: str = ""
    position: str = ""
    team: str = ""
    weeks: dict[int, dict[str, float]] = field(default_factory=dict)

    def totals(self, column: str) -> float:
        return sum(week.get(column, 0.0) for week in self.weeks.values())

    def games_played(self) -> int:
        return len(self.weeks)

    def per_game(self, column: str) -> float:
        """Recency-weighted average per game played.

        Averaged over games PLAYED, never over weeks elapsed. A missed week would
        otherwise drag a returning player's projected volume toward zero, and whether he
        plays at all is the lineup builder's question, not this one.
        """
        if not self.weeks:
            return 0.0
        recent = [self.weeks[w] for w in sorted(self.weeks, reverse=True)]
        recent = recent[: len(VOLUME_WEIGHTS)]
        weights = VOLUME_WEIGHTS[: len(recent)]
        weighted = sum(w * week.get(column, 0.0) for w, week in zip(weights, recent))
        return weighted / sum(weights)


@dataclass(frozen=True)
class Projection:
    """Projected points, with the parts that produced them.

    The components are kept so a recommendation can be explained and re-derived months
    later without re-running anything.
    """

    gsis_id: str
    name: str
    position: str
    team: str
    points: float
    games_played: int
    pass_attempts: float = 0.0
    carries: float = 0.0
    targets: float = 0.0
    projected_receptions: float = 0.0
    projected_touchdowns: float = 0.0

    @property
    def opportunities(self) -> float:
        """Every chance to produce, including a quarterback's throws."""
        return self.pass_attempts + self.carries + self.targets


_STAT_COLUMNS = (
    "attempts",
    "carries",
    "targets",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "rushing_yards",
    "rushing_tds",
    "receptions",
    "receiving_yards",
    "receiving_tds",
    "fumbles_lost_total",
    *KICKER_BUCKETS,
)


def build_seasons(
    stats: list[dict[str, str]], through_week: int
) -> dict[str, PlayerSeason]:
    """Group weekly rows by player, ignoring anything after the cutoff."""
    seasons: dict[str, PlayerSeason] = {}
    for row in stats:
        gsis_id = row.get("player_id") or ""
        week = week_of(row)
        if not gsis_id or week is None or week > through_week:
            continue

        season = seasons.get(gsis_id)
        if season is None:
            season = PlayerSeason(gsis_id=gsis_id)
            seasons[gsis_id] = season
        season.name = row.get("player_display_name") or season.name
        season.position = (row.get("position") or season.position).upper()
        season.team = (row.get("team") or season.team).upper()
        season.weeks[week] = {
            column: as_float(row.get(column)) for column in _STAT_COLUMNS
        }
    return seasons


def project_player(
    season: PlayerSeason, rates: LeagueRates, scoring: Scoring
) -> Projection:
    """Project one player's points for a single coming game."""
    if season.position == KICKER_POSITION:
        return _project_kicker(season)

    attempts = season.per_game("attempts")
    carries = season.per_game("carries")
    targets = season.per_game("targets")

    total_attempts = season.totals("attempts")
    total_carries = season.totals("carries")
    total_targets = season.totals("targets")

    yards_per_attempt = shrink(
        season.totals("passing_yards"), total_attempts, rates.yards_per_attempt, EFFICIENCY_PRIOR
    )
    pass_td_rate = shrink(
        season.totals("passing_tds"),
        total_attempts,
        rates.pass_td_per_attempt,
        RARE_EVENT_PRIOR,
    )
    interception_rate = shrink(
        season.totals("passing_interceptions"),
        total_attempts,
        rates.interceptions_per_attempt,
        RARE_EVENT_PRIOR,
    )
    yards_per_carry = shrink(
        season.totals("rushing_yards"), total_carries, rates.yards_per_carry, EFFICIENCY_PRIOR
    )
    rush_td_rate = shrink(
        season.totals("rushing_tds"), total_carries, rates.rush_td_per_carry, RARE_EVENT_PRIOR
    )
    catch_rate = shrink(
        season.totals("receptions"), total_targets, rates.catch_rate, EFFICIENCY_PRIOR
    )
    yards_per_target = shrink(
        season.totals("receiving_yards"), total_targets, rates.yards_per_target, EFFICIENCY_PRIOR
    )
    rec_td_rate = shrink(
        season.totals("receiving_tds"), total_targets, rates.rec_td_per_target, RARE_EVENT_PRIOR
    )

    receptions = targets * catch_rate
    touchdowns = (
        attempts * pass_td_rate + carries * rush_td_rate + targets * rec_td_rate
    )
    fumbles = (
        season.totals("fumbles_lost_total") / season.games_played()
        if season.games_played()
        else 0.0
    )

    points = (
        attempts * yards_per_attempt * scoring.pass_yards
        + attempts * pass_td_rate * scoring.pass_td
        + attempts * interception_rate * scoring.interception
        + carries * yards_per_carry * scoring.rush_yards
        + carries * rush_td_rate * scoring.rush_td
        + receptions * scoring.reception
        + targets * yards_per_target * scoring.rec_yards
        + targets * rec_td_rate * scoring.rec_td
        + fumbles * scoring.fumble_lost
    )

    return Projection(
        gsis_id=season.gsis_id,
        name=season.name,
        position=season.position,
        team=season.team,
        points=max(0.0, points),
        games_played=season.games_played(),
        pass_attempts=attempts,
        carries=carries,
        targets=targets,
        projected_receptions=receptions,
        projected_touchdowns=touchdowns,
    )


def _project_kicker(season: PlayerSeason) -> Projection:
    """Kickers get a recency-weighted average of their own scoring.

    There is no opportunity metric here worth modelling: the nflverse release carries no
    field goal attempts by distance for a coming game, and a kicker's volume is decided by
    how far his offense advances.
    """
    points = sum(
        weight * season.per_game(column) for column, weight in KICKER_BUCKETS.items()
    )
    return Projection(
        gsis_id=season.gsis_id,
        name=season.name,
        position=season.position,
        team=season.team,
        points=max(0.0, points),
        games_played=season.games_played(),
    )


def week_of(row: dict[str, str]) -> int | None:
    """The row's week, or None when it carries no usable week."""
    raw = row.get("week") or ""
    return int(raw) if raw.isdigit() else None


def rows_through(stats: list[dict[str, str]], through_week: int) -> list[dict[str, str]]:
    """Only the rows at or before the cutoff.

    The per-player totals and the league rates must read the same window. Calibrating the
    league rates on every row would let a later week inform an earlier projection, which
    silently inflates any backtest and cannot be seen in its output.
    """
    return [
        row
        for row in stats
        if (week := week_of(row)) is not None and week <= through_week
    ]


def build_projections(
    stats: list[dict[str, str]],
    through_week: int,
    scoring: Scoring | None = None,
) -> dict[str, Projection]:
    """Project every player who has played, keyed by `gsis_id`."""
    eligible = rows_through(stats, through_week)
    rates = league_rates(eligible)
    resolved = scoring or Scoring()
    return {
        gsis_id: project_player(season, rates, resolved)
        for gsis_id, season in build_seasons(eligible, through_week).items()
        if season.position not in UNPROJECTABLE_POSITIONS
    }
