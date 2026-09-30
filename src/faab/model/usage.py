"""Per-player weekly usage, assembled from the nflverse releases.

Two releases have to be joined and they use different player ids. Weekly stats key on
`player_id`, which is a gsis id. Snap counts key on `pfr_player_id`, because they come
from Pro Football Reference. The player directory carries both, so it is the bridge.
Everything downstream keys on gsis id.

Why usage and not a projection: a projection needs a model of the coming game. What
this module gives instead is what actually happened, at the level that predicts the
next game better than points do. Snap share and target share persist week to week;
touchdowns do not. A report built on this must say so rather than calling it a
projection, which is why `weighted_recent_points` is named for what it is, a weighted recent
average.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from faab.collectors.nflverse import as_float

# Most recent week weighted heaviest. Three weeks is enough to see a role change and
# short enough not to average a promotion away.
RECENCY_WEIGHTS = (3.0, 2.0, 1.0)

SKILL_POSITIONS = frozenset({"RB", "WR", "TE"})

# Positions with no per-player fantasy points in the nflverse release, so no ranking is
# possible from this source. A team defense is not a player at all. A kicker IS in the
# release but scores zero PPR, so kicker points are computed below instead.
UNRANKABLE_POSITIONS = frozenset({"DEF", "DST"})

# Yahoo's default kicker scoring. nflverse reports fantasy_points_ppr as 0 for every
# kicker, so these points are computed from the distance buckets rather than read.
KICKER_POINTS_BY_BUCKET: dict[str, float] = {
    "fg_made_0_19": 3.0,
    "fg_made_20_29": 3.0,
    "fg_made_30_39": 3.0,
    "fg_made_40_49": 4.0,
    "fg_made_50_59": 5.0,
    "fg_made_60_": 5.0,
    "pat_made": 1.0,
}


def kicker_points(row: dict[str, str]) -> float:
    """Fantasy points for one kicker week, under Yahoo's default distance scoring."""
    return sum(
        weight * as_float(row.get(column))
        for column, weight in KICKER_POINTS_BY_BUCKET.items()
    )



@dataclass(frozen=True)
class WeekUsage:
    week: int
    points_ppr: float = 0.0
    targets: float = 0.0
    target_share: float = 0.0
    carries: float = 0.0
    air_yards_share: float = 0.0
    wopr: float = 0.0
    snap_pct: float | None = None


@dataclass
class Usage:
    gsis_id: str
    name: str
    position: str
    team: str
    weeks: dict[int, WeekUsage] = field(default_factory=dict)

    def played(self) -> list[int]:
        return sorted(self.weeks)

    def recent(self, through_week: int, count: int = 3) -> list[WeekUsage]:
        """The most recent `count` weeks at or before `through_week`, newest first."""
        selected = [w for w in self.played() if w <= through_week]
        return [self.weeks[w] for w in reversed(selected[-count:])]

    def weighted_recent_points(self, through_week: int) -> float:
        """A recency-weighted average of PPR points over the last three weeks played.

        Not a projection. It carries no opponent, no game script and no injury news.
        A player with no weeks played scores zero, which correctly sorts him below
        anyone with evidence.
        """
        recent = self.recent(through_week)
        if not recent:
            return 0.0
        weights = RECENCY_WEIGHTS[: len(recent)]
        total = sum(w * u.points_ppr for w, u in zip(weights, recent))
        return total / sum(weights)

    def snap_trend(self, through_week: int) -> float | None:
        """Change in snap share between the two most recent weeks played.

        None when either week has no snap data, which is normal for a kicker or a
        defense and for a player who has not played twice.
        """
        recent = self.recent(through_week, count=2)
        if len(recent) < 2:
            return None
        latest, prior = recent[0].snap_pct, recent[1].snap_pct
        if latest is None or prior is None:
            return None
        return latest - prior

    def opportunity_share(self, through_week: int) -> float:
        """Latest-week share of the team's passing opportunity, or carries volume.

        Returns target share for a pass catcher and a carries-derived figure for a
        back, on the same nominal 0..1 scale so a FLEX decision can compare them.
        Twenty carries is treated as a full workload.
        """
        recent = self.recent(through_week, count=1)
        if not recent:
            return 0.0
        latest = recent[0]
        if self.position == "RB":
            return max(latest.target_share, min(latest.carries / 20.0, 1.0))
        return latest.target_share


def build_usage(
    weekly_stats: list[dict[str, str]],
    snap_counts: list[dict[str, str]],
    players: list[dict[str, str]],
    through_week: int,
) -> dict[str, Usage]:
    """Assemble per-player usage keyed by gsis id, for weeks up to `through_week`.

    A week after `through_week` is excluded rather than trusted, because a week still
    in progress reports a player who has not kicked off yet as having no opportunity.
    """
    pfr_to_gsis = {
        row["pfr_id"]: row["gsis_id"]
        for row in players
        if row.get("pfr_id") and row.get("gsis_id")
    }

    usage: dict[str, Usage] = {}
    weekly_rows: dict[tuple[str, int], dict[str, str]] = {}

    for row in weekly_stats:
        gsis_id = row.get("player_id") or ""
        week_raw = row.get("week") or ""
        if not gsis_id or not week_raw.isdigit():
            continue
        week = int(week_raw)
        if week > through_week:
            continue
        weekly_rows[(gsis_id, week)] = row
        entry = usage.get(gsis_id)
        if entry is None:
            entry = Usage(
                gsis_id=gsis_id,
                name=row.get("player_display_name") or row.get("player_name") or "",
                position=(row.get("position") or "").upper(),
                team=(row.get("team") or "").upper(),
            )
            usage[gsis_id] = entry
        elif week >= max(entry.weeks, default=0):
            # Keep the most recent team, so a midseason trade is reflected.
            entry.team = (row.get("team") or entry.team).upper()

        entry.weeks[week] = WeekUsage(
            week=week,
            points_ppr=(
                kicker_points(row)
                if entry.position == "K"
                else as_float(row.get("fantasy_points_ppr"))
            ),
            targets=as_float(row.get("targets")),
            target_share=as_float(row.get("target_share")),
            carries=as_float(row.get("carries")),
            air_yards_share=as_float(row.get("air_yards_share")),
            wopr=as_float(row.get("wopr")),
        )

    for row in snap_counts:
        gsis_id = pfr_to_gsis.get(row.get("pfr_player_id") or "")
        week_raw = row.get("week") or ""
        if not gsis_id or not week_raw.isdigit():
            continue
        week = int(week_raw)
        if week > through_week:
            continue
        entry = usage.get(gsis_id)
        if entry is None or week not in entry.weeks:
            continue
        existing = entry.weeks[week]
        entry.weeks[week] = WeekUsage(
            week=existing.week,
            points_ppr=existing.points_ppr,
            targets=existing.targets,
            target_share=existing.target_share,
            carries=existing.carries,
            air_yards_share=existing.air_yards_share,
            wopr=existing.wopr,
            snap_pct=as_float(row.get("offense_pct")),
        )

    return usage
