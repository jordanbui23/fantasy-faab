"""Waiver candidates: who to claim, and roughly what to bid.

This is the honest version of the report, not the intended one. The intended model
ranks a candidate against what the other managers can and will spend, which
needs each rival's FAAB balance and each rival's roster holes, and both are Yahoo data
that is gated behind an approval that has not arrived. `docs/RESEARCH.md` records what
those fields are.

What this does instead is find the opportunity changes the field has not priced in yet.
A player whose snap share jumped and whose target share followed is a real role change.
Sleeper's national add count says whether everyone else has already noticed. The
interesting candidate is the one with the role change and the low add count.

Every bid here is a fraction of budget derived from the signal strength alone. It cannot
account for a rival sitting on an untouched budget, so it is a starting point for a
human, not a number to type without thinking.
"""

from __future__ import annotations

from dataclasses import dataclass

from faab.model.usage import Usage

# A snap share has to move by this much to count as a role change rather than noise.
MIN_SNAP_JUMP = 0.12
# Below this snap share a role change does not yet matter, however large the jump.
MIN_SNAP_FLOOR = 0.35

CLAIMABLE_POSITIONS = frozenset({"RB", "WR", "TE", "QB"})

# Add count at which a player counts as fully priced by the public. Sleeper's national
# counts reach the millions for a genuine breakout.
SATURATION_ADDS = 1_000_000

# Below this add count the public has not piled in yet, which is what QUIET reports.
QUIET_ADDS = 100_000

# How much being unnoticed lifts a candidate up the ranking. Deliberately modest: it
# breaks a near tie between two similar role changes and cannot promote a weak one over
# a strong one.
SCARCITY_WEIGHT = 0.15

# Bid as a fraction of budget, by role strength. Retained for the role-strength path only.
BID_FRACTIONS = ((0.80, 0.18), (0.60, 0.10), (0.40, 0.05), (0.0, 0.01))

# The most of a season's budget one claim may take. A budget has to last every remaining
# week, so a single claim that eats a quarter of it needs to be a season-changing player,
# and this model cannot recognise one of those with enough confidence to spend that.
MAX_BID_SHARE = 0.12

# The exclusive weekly gain that earns the maximum bid. Exclusive means the gain this claim
# has over the next best ALTERNATIVE claim, not its gain over the current lineup.
GAIN_FOR_MAX_BID = 4.0

# What a claim is worth when it improves nothing this week. It buys an option on somebody
# getting hurt, which is worth a dollar and not more.
STASH_BID = 1


def exclusive_gain(gain: float, alternatives: list[float]) -> float:
    """The part of a claim's gain that losing it would actually cost.

    A bid buys one player, so the question is never "what is he worth" but "what is he worth
    MORE than whoever I would take instead". When nine kickers all improve the lineup by
    three points, winning any particular one is worth almost nothing, and bidding as though
    it were worth three points is how a season's budget disappears in week 4.

    `alternatives` is every other claim's gain. Pass an empty list when a claim is genuinely
    the only one available, which is the only case where full gain is the right basis.
    """
    best_other = max(alternatives, default=0.0)
    return max(0.0, gain - best_other)

# A bid at or above this share of the budget is a serious one, used to judge which rivals
# could realistically outbid a claim. Set at the top BID_FRACTIONS tier, so "able to
# compete" means able to match the largest bid this model would ever suggest.
SERIOUS_BID_SHARE = 0.18

# How much a fully chased player raises the bid. Ranking and bidding pull in opposite
# directions here, and both are correct: an unnoticed player is the better FIND, and a
# chased player costs more to WIN. Keeping them separate is why this is not one number.
CONTENTION_UPLIFT = 0.60


def role_strength(
    snap_jump: float, snap_share: float, opportunity: float, points: float
) -> float:
    """How big the role change is, on a nominal 0 to 1 scale, ignoring public demand.

    Weighted toward the jump and the resulting share rather than toward points, because
    a role change predicts the next game and a touchdown does not.
    """
    return (
        0.40 * min(snap_jump / 0.40, 1.0)
        + 0.25 * min(snap_share, 1.0)
        + 0.25 * min(opportunity, 1.0)
        + 0.10 * min(points / 20.0, 1.0)
    )


def scarcity(trending_adds: int) -> float:
    """How unnoticed a player still is: 1.0 when nobody has added him, 0.0 when priced."""
    return 1.0 - min(max(trending_adds, 0) / SATURATION_ADDS, 1.0)


def rank_score(strength: float, player_scarcity: float) -> float:
    """The ranking number, which prefers a role change the public has not priced yet."""
    return (1.0 - SCARCITY_WEIGHT) * strength + SCARCITY_WEIGHT * player_scarcity


def rival_contention(rival_budgets: list[int], budget: int) -> float | None:
    """The share of rivals who could outbid a serious claim.

    This is a better contention signal than a league-wide add count, because it measures
    who can actually pay rather than who is interested. A rival with nothing left cannot
    take a player from you however much he wants him.

    Returns None for an empty list so the caller can fall back rather than read zero
    pressure from missing data, which would understate every bid.
    """
    if not rival_budgets:
        return None
    threshold = max(1.0, budget * SERIOUS_BID_SHARE)
    able = sum(1 for rival in rival_budgets if rival >= threshold)
    return able / len(rival_budgets)


def bid_for_swap(
    gain: float,
    alternatives: list[float],
    budget: int,
    rival_budgets: list[int] | None = None,
    trending_adds: int = 0,
) -> int:
    """A whole-dollar bid sized from what a claim exclusively adds.

    Three things hold it down, and each corrects a way the earlier version overbid. The
    basis is exclusive gain rather than gain, so a deep position cannot command a premium.
    The share is capped at `MAX_BID_SHARE` of the budget however large the gain, because the
    budget must survive the rest of the season. And a claim that improves nothing now is a
    flat `STASH_BID`, whatever its role trend says.
    """
    if budget <= 0:
        return 0

    exclusive = exclusive_gain(gain, alternatives)
    if gain < 0.5 or exclusive <= 0.0:
        return min(budget, STASH_BID)

    share = min(MAX_BID_SHARE, (exclusive / GAIN_FOR_MAX_BID) * MAX_BID_SHARE)
    from_budgets = rival_contention(rival_budgets or [], budget)
    contested = from_budgets if from_budgets is not None else 1.0 - scarcity(trending_adds)
    raised = min(MAX_BID_SHARE, share * (1.0 + CONTENTION_UPLIFT * contested))
    return max(1, min(budget, round(budget * raised)))


def suggested_bid(
    strength: float,
    trending_adds: int,
    budget: int,
    rival_budgets: list[int] | None = None,
) -> int:
    """A whole-dollar bid from role strength, raised for a contested player.

    Returns zero only when the budget is zero, because there is then nothing to bid.
    Otherwise the floor is one dollar: a zero-dollar claim is legal in Yahoo but wins
    only when nobody else bids, so a player worth claiming is worth a dollar.

    Contention comes from rival budgets when they are known, and from the league-wide add
    count otherwise. The budgets are preferred because they measure ability to pay.
    """
    if budget <= 0:
        return 0

    fraction = BID_FRACTIONS[-1][1]
    for threshold, tier_fraction in BID_FRACTIONS:
        if strength >= threshold:
            fraction = tier_fraction
            break

    from_budgets = rival_contention(rival_budgets or [], budget)
    contested = from_budgets if from_budgets is not None else 1.0 - scarcity(trending_adds)
    raised = fraction * (1.0 + CONTENTION_UPLIFT * contested)
    return max(1, min(budget, round(budget * raised)))



@dataclass(frozen=True)
class Candidate:
    usage: Usage
    snap_jump: float
    snap_share: float
    opportunity_share: float
    weighted_recent_points: float
    trending_adds: int
    role_strength: float
    rank_score: float

    @property
    def name(self) -> str:
        return self.usage.name

    @property
    def position(self) -> str:
        return self.usage.position

    @property
    def team(self) -> str:
        return self.usage.team

    @property
    def unnoticed(self) -> bool:
        """True when the role changed but the wider public has not piled in yet."""
        return self.trending_adds < QUIET_ADDS


def find_candidates(
    usage: dict[str, Usage],
    through_week: int,
    rostered_gsis_ids: set[str],
    trending_by_name: dict[str, int],
    limit: int = 8,
    available_gsis_ids: set[str] | None = None,
) -> list[Candidate]:
    """Rank claimable players by unpriced opportunity change.

    `rostered_gsis_ids` is this team's own roster and is always excluded.

    `available_gsis_ids` is the free-agent pool, pasted from Yahoo's available-players
    page. When supplied, nothing outside it can be recommended. When absent the rival
    rosters are invisible and the sheet will suggest players who cannot be claimed,
    which is severe rather than cosmetic: without it the top of the sheet fills with
    obviously rostered stars, because a player on a good team looks exactly like a free
    agent to usage data. The report states which mode produced it.

    An empty set means an empty pool and yields nothing, which is different from None.
    """
    candidates: list[Candidate] = []

    for gsis_id, entry in usage.items():
        if gsis_id in rostered_gsis_ids:
            continue
        if available_gsis_ids is not None and gsis_id not in available_gsis_ids:
            continue
        if entry.position not in CLAIMABLE_POSITIONS:
            continue

        recent = entry.recent(through_week, count=2)
        if len(recent) < 2:
            continue
        latest, prior = recent[0], recent[1]
        if latest.snap_pct is None or prior.snap_pct is None:
            continue

        snap_jump = latest.snap_pct - prior.snap_pct
        if snap_jump < MIN_SNAP_JUMP or latest.snap_pct < MIN_SNAP_FLOOR:
            continue

        opportunity = entry.opportunity_share(through_week)
        points = entry.weighted_recent_points(through_week)
        adds = trending_by_name.get(entry.name, 0)
        strength = role_strength(snap_jump, latest.snap_pct, opportunity, points)
        candidates.append(
            Candidate(
                usage=entry,
                snap_jump=snap_jump,
                snap_share=latest.snap_pct,
                opportunity_share=opportunity,
                weighted_recent_points=points,
                trending_adds=adds,
                role_strength=strength,
                rank_score=rank_score(strength, scarcity(adds)),
            )
        )

    candidates.sort(key=lambda c: (-c.rank_score, c.name))
    return candidates[:limit]
