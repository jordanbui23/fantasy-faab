"""Start/sit: fill the league's slots from the roster, and name the close calls.

Ordering is by `model.project`, which estimates points for the COMING week from volume
and league-average touchdown rates. Ranking on points already scored came first and
measured worse against Yahoo's own projections. The figures live in `model.project` and
`docs/RESEARCH.md` section 7c, so no copy of them can go stale here.

The two checks that earn this module its place are not the ordering. They are the hard
exclusions: a player whose team is on bye cannot score, and a player ruled out cannot
score. Both are easy to miss by hand on a Sunday morning and both cost a whole slot.
"""

from __future__ import annotations

from dataclasses import dataclass

from faab.league import League
from faab.model.assign import assign_slots
from faab.model.project import Projection
from faab.model.usage import UNRANKABLE_POSITIONS, Usage
from faab.names import Player

# Sleeper's injury_status values that mean the player will not play. DOUBTFUL is not
# here: it is a real risk but not a certainty, so it becomes a warning instead.
OUT_STATUSES = frozenset({"OUT", "IR", "PUP", "SUS", "COV", "NA", "DNR"})
WARN_STATUSES = frozenset({"DOUBTFUL", "QUESTIONABLE"})

# Two starters within this many weighted points are a judgment call, not a ranking.
CLOSE_CALL_POINTS = 2.5

# Display order for the rendered lineup, which is not the fill order. Slots are filled
# most-constrained first so FLEX cannot steal the only eligible back, but they read
# naturally in this order.
DISPLAY_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "WRRB", "SUPERFLEX", "K", "DEF")


@dataclass(frozen=True)
class Candidate:
    player: Player
    projected_points: float
    snap_trend: float | None
    opportunity_share: float
    weeks_played: int
    blocked: str = ""
    warning: str = ""
    rankable: bool = True

    @property
    def startable(self) -> bool:
        return not self.blocked


@dataclass(frozen=True)
class SlotChoice:
    slot: str
    starter: Candidate
    runner_up: Candidate | None
    margin: float | None

    @property
    def is_close(self) -> bool:
        """A close call needs a real alternative and two comparable numbers.

        An unrankable position such as a team defense has no points to compare, so it
        is never a close call however small the arithmetic difference looks.
        """
        if self.margin is None or self.runner_up is None:
            return False
        if not (self.starter.rankable and self.runner_up.rankable):
            return False
        return self.margin < CLOSE_CALL_POINTS


@dataclass(frozen=True)
class Lineup:
    week: int
    choices: list[SlotChoice]
    bench: list[Candidate]
    blocked: list[Candidate]
    unfilled: list[str]

    @property
    def close_calls(self) -> list[SlotChoice]:
        return [c for c in self.choices if c.is_close]

    @property
    def display_choices(self) -> list[SlotChoice]:
        """Choices in reading order rather than fill order."""
        rank = {slot: i for i, slot in enumerate(DISPLAY_ORDER)}
        return sorted(
            self.choices,
            key=lambda c: (
                rank.get(c.slot, len(DISPLAY_ORDER)),
                -c.starter.projected_points,
            ),
        )


def build_candidates(
    roster: list[Player],
    usage: dict[str, Usage],
    projections: dict[str, Projection],
    byes: set[str],
    through_week: int,
) -> list[Candidate]:
    """Score every roster player and record why one cannot start.

    A player with no usage record is kept rather than dropped. He sorts last, and being
    visible on the bench is how the owner notices a name that failed to join.
    """
    candidates: list[Candidate] = []
    for player in roster:
        entry = usage.get(player.gsis_id) if player.gsis_id else None
        status = (player.injury_status or "").upper()
        rankable = player.position not in UNRANKABLE_POSITIONS

        blocked = ""
        if player.team and player.team.upper() in byes:
            blocked = f"{player.team} on bye"
        elif status in OUT_STATUSES:
            blocked = status

        warning = ""
        if not blocked and status in WARN_STATUSES:
            warning = status
        elif not blocked and entry is None and rankable:
            # An unrankable position has no stats by design, so its absence is not news.
            warning = "no stats matched"

        projection = projections.get(player.gsis_id) if player.gsis_id else None
        candidates.append(
            Candidate(
                player=player,
                projected_points=projection.points if projection else 0.0,
                snap_trend=entry.snap_trend(through_week) if entry else None,
                opportunity_share=(
                    entry.opportunity_share(through_week) if entry else 0.0
                ),
                weeks_played=len(entry.recent(through_week, count=99)) if entry else 0,
                blocked=blocked,
                warning=warning,
                rankable=rankable,
            )
        )
    return candidates


def recommend_lineup(
    roster: list[Player],
    usage: dict[str, Usage],
    projections: dict[str, Projection],
    byes: set[str],
    week: int,
    league: League,
    through_week: int | None = None,
) -> Lineup:
    """Fill every slot with the best startable player, most constrained slot first.

    `week` is the week being played. `through_week` is the last week with complete
    stats, which defaults to the week before. The two differ because a lineup is set
    before the games it covers.
    """
    stats_week = week - 1 if through_week is None else through_week
    candidates = build_candidates(roster, usage, projections, byes, stats_week)

    available = sorted(
        (c for c in candidates if c.startable),
        key=lambda c: (-c.projected_points, -c.opportunity_share, c.player.name),
    )
    blocked = [c for c in candidates if not c.startable]

    slots = league.slot_order()
    eligibility = [
        [league.accepts(slot, c.player.position) for c in available] for slot in slots
    ]
    assignment = assign_slots(
        slots, [c.projected_points for c in available], eligibility
    )

    filled = [(slot, player) for slot, player in zip(slots, assignment) if player >= 0]
    unfilled = [slot for slot, player in zip(slots, assignment) if player < 0]
    used = {player for _, player in filled}
    bench = [c for index, c in enumerate(available) if index not in used]

    # A close call is a starter against the best player left on the BENCH. Comparing one
    # starter to another produced pairs the owner cannot act on, because both were
    # already starting.
    choices: list[SlotChoice] = []
    for slot, index in filled:
        starter = available[index]
        alternatives = [c for c in bench if league.accepts(slot, c.player.position)]
        runner_up = alternatives[0] if alternatives else None
        margin = (
            starter.projected_points - runner_up.projected_points
            if runner_up is not None
            else None
        )
        choices.append(
            SlotChoice(slot=slot, starter=starter, runner_up=runner_up, margin=margin)
        )

    return Lineup(
        week=week, choices=choices, bench=bench, blocked=blocked, unfilled=unfilled
    )
