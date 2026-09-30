"""Report formatting for a phone notification.

One hard constraint shapes everything here: ntfy turns a message over 4,096 bytes into
a file attachment instead of a notification, so a long report silently stops being
glanceable. `render` therefore measures its own output and drops the optional sections
from the least useful upward until it fits, rather than truncating mid-line.

The report is plain text. ntfy renders markdown only when asked, and a lineup reads
better as aligned columns than as bullets on a phone.
"""

from __future__ import annotations

from faab.model.lineup import Lineup
from faab.model.waiver import Candidate as WaiverCandidate

NTFY_MAX_BYTES = 4096

# Left in the message when space runs short, in this order. The lineup and the hard
# exclusions always stay: they are the decisions. Bench detail goes first.
_TRIM_ORDER = ("bench", "close_calls", "warnings")


def _fmt_points(value: float) -> str:
    return f"{value:.1f}"


def _fmt_trend(trend: float | None) -> str:
    if trend is None:
        return ""
    return f" snaps {trend * 100:+.0f}%"


def _lineup_lines(lineup: Lineup) -> list[str]:
    lines = []
    for choice in lineup.display_choices:
        starter = choice.starter
        flag = "?" if starter.warning else " "
        points = _fmt_points(starter.projected_points) if starter.rankable else "--"
        lines.append(
            f"{choice.slot:<5}{flag}{starter.player.name:<20} "
            f"{starter.player.position}/{starter.player.team} {points}"
        )
    for slot in lineup.unfilled:
        lines.append(f"{slot:<5} -- NOBODY ELIGIBLE --")
    return lines


def _blocked_lines(lineup: Lineup) -> list[str]:
    return [
        f"  {c.player.name} ({c.player.position}) {c.blocked}" for c in lineup.blocked
    ]


def _close_call_lines(lineup: Lineup) -> list[str]:
    lines = []
    for choice in lineup.close_calls:
        if choice.runner_up is None:
            continue
        starter, alt = choice.starter, choice.runner_up
        lines.append(
            f"  {choice.slot}: {starter.player.name} "
            f"{_fmt_points(starter.projected_points)}"
            f"{_fmt_trend(starter.snap_trend)}"
        )
        lines.append(
            f"      over {alt.player.name} {_fmt_points(alt.projected_points)}"
            f"{_fmt_trend(alt.snap_trend)}"
        )
    return lines


def _warning_lines(lineup: Lineup) -> list[str]:
    return [
        f"  {choice.starter.player.name}: {choice.starter.warning}"
        for choice in lineup.choices
        if choice.starter.warning
    ]


def _bench_lines(lineup: Lineup, limit: int = 6) -> list[str]:
    return [
        f"  {c.player.name:<20} {c.player.position} {_fmt_points(c.projected_points)}"
        for c in lineup.bench[:limit]
    ]


def render_lineup(lineup: Lineup, sections: tuple[str, ...] = _TRIM_ORDER) -> str:
    """Render the start/sit report, including only the named optional sections."""
    parts: list[str] = [f"WEEK {lineup.week} LINEUP", ""]
    parts.extend(_lineup_lines(lineup))

    blocked = _blocked_lines(lineup)
    if blocked:
        parts.extend(["", "CANNOT PLAY"])
        parts.extend(blocked)

    if "warnings" in sections:
        warnings = _warning_lines(lineup)
        if warnings:
            parts.extend(["", "CHECK BEFORE KICKOFF"])
            parts.extend(warnings)

    if "close_calls" in sections:
        close = _close_call_lines(lineup)
        if close:
            parts.extend(["", "CLOSE CALLS"])
            parts.extend(close)

    if "bench" in sections:
        bench = _bench_lines(lineup)
        if bench:
            parts.extend(["", "BENCH"])
            parts.extend(bench)

    parts.extend(["", "Projected from usage, with touchdown rates pulled to league average."])
    return "\n".join(parts)


def fit_to_ntfy(lineup: Lineup, max_bytes: int = NTFY_MAX_BYTES) -> str:
    """Render the report, dropping optional sections until it fits a notification.

    Returns the fullest version that fits. When even the mandatory sections exceed the
    limit the over-long text is returned unchanged: silently cutting a lineup in half
    would be worse than ntfy converting it to an attachment, which is at least visible.
    """
    sections = list(_TRIM_ORDER)
    while True:
        text = render_lineup(lineup, tuple(sections))
        if len(text.encode("utf-8")) <= max_bytes or not sections:
            return text
        sections.pop(0)


# --- waiver report -------------------------------------------------------------


def render_waivers(
    candidates: list[WaiverCandidate],
    week: int,
    budget: int,
    bids: dict[str, int],
    stopgap: bool = True,
) -> str:
    """Render the waiver claim sheet.

    `bids` maps a candidate name to its suggested dollar amount, computed by the caller
    so this function stays presentation only.
    """
    parts = [f"WEEK {week} WAIVERS", ""]

    if not candidates:
        parts.append("No role changes above the noise floor this week.")
        parts.extend(["", "Nothing to claim is a valid answer. Keep the budget."])
        return "\n".join(parts)

    for rank, candidate in enumerate(candidates, start=1):
        bid = bids.get(candidate.name, 1)
        quiet = " QUIET" if candidate.unnoticed else ""
        parts.append(
            f"{rank}. ${bid} {candidate.name} "
            f"{candidate.position}/{candidate.team}{quiet}"
        )
        parts.append(
            f"   snaps {candidate.snap_share * 100:.0f}% "
            f"({candidate.snap_jump * 100:+.0f}%), "
            f"opp {candidate.opportunity_share * 100:.0f}%, "
            f"{candidate.weighted_recent_points:.1f} pts"
        )

    parts.extend(["", f"Budget ${budget}. QUIET means the public has not piled in."])
    if stopgap:
        parts.append("Some may already be rostered: no league data yet.")
    return "\n".join(parts)


def fit_waivers_to_ntfy(
    candidates: list[WaiverCandidate],
    week: int,
    budget: int,
    bids: dict[str, int],
    stopgap: bool = True,
    max_bytes: int = NTFY_MAX_BYTES,
) -> str:
    """Render the waiver sheet, dropping the lowest-ranked claims until it fits."""
    shown = list(candidates)
    while True:
        text = render_waivers(shown, week, budget, bids, stopgap)
        if len(text.encode("utf-8")) <= max_bytes or not shown:
            return text
        shown.pop()


def render_swaps(
    swaps: list,
    week: int,
    budget: int,
    bids: dict[str, int],
    rivals_able: int = 0,
    rival_count: int = 0,
    market_covers_roster: bool = True,
) -> str:
    """Render waiver claims as add/drop pairs.

    A full roster makes every claim a swap, so naming only the player to add hides half the
    decision and all of its cost. Each line carries the drop, both projections where they
    disagree, and the net weekly gain of the pair.
    """
    parts = [f"WEEK {week} WAIVERS", ""]
    parts.append(f"Budget ${budget}." + (
        f" {rivals_able} of {rival_count} rivals can outbid." if rival_count else ""
    ))
    parts.append("")

    # A claim worth a fraction of a point a week is not worth a roster move, and listing it
    # invites exactly the question this sheet should answer: why would I do that.
    upgrades = [s for s in swaps if s.is_upgrade]
    if not upgrades:
        parts.append("No claim improves the lineup enough to matter. Hold the budget.")
    for position, swap in enumerate(upgrades, start=1):
        bid = bids.get(swap.add.name, 1)
        parts.append(
            f"{position}. ${bid} ADD {swap.add.name} {swap.add.position}/{swap.add.team}"
        )
        if swap.drop is None:
            parts.append("       DROP nobody, roster has room")
        else:
            role = "starter" if swap.drop_was_starter else "bench"
            blocked = f", {swap.drop_blocked}" if swap.drop_blocked else ""
            parts.append(
                f"       DROP {swap.drop.name} {swap.drop.position}/{swap.drop.team}"
                f" ({role}{blocked}, {swap.drop_points:.1f})"
            )
        line = f"       net {swap.gain:+.1f}/wk"
        if swap.market_points is not None:
            line += f"  mine {swap.model_points:.1f} vs Yahoo {swap.market_points:.1f}"
        parts.append(line)
        if swap.disputed:
            parts.append(
                f"       DISPUTED: worth {swap.optimistic_gain:+.1f}/wk on my number"
            )

    parts.append("")
    parts.append("Bids priced on the lower projection and on what each claim adds")
    parts.append("over the next best alternative, not over your current lineup.")
    if not market_covers_roster:
        parts.append("No Yahoo numbers for your own roster, so a drop is valued on my")
        parts.append("projection alone. That overstates a claim when I underrate the drop.")
    return "\n".join(parts)


def fit_swaps_to_ntfy(
    swaps: list,
    week: int,
    budget: int,
    bids: dict[str, int],
    rivals_able: int = 0,
    rival_count: int = 0,
    market_covers_roster: bool = True,
    max_bytes: int = NTFY_MAX_BYTES,
) -> str:
    """Render the swap sheet, dropping the lowest-value claims until it fits."""
    shown = list(swaps)
    while True:
        text = render_swaps(
            shown, week, budget, bids, rivals_able, rival_count, market_covers_roster
        )
        if len(text.encode("utf-8")) <= max_bytes or not shown:
            return text
        shown.pop()
