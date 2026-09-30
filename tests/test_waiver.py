"""Tests for waiver candidate selection and bid sizing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import report  # noqa: E402
from faab.model import waiver  # noqa: E402
from faab.model.usage import Usage, WeekUsage  # noqa: E402

THROUGH = 2


def _usage(
    gsis_id,
    name,
    position="RB",
    snaps=(0.20, 0.80),
    carries=(4.0, 18.0),
    target_share=(0.05, 0.10),
    points=(2.0, 14.0),
):
    weeks = {}
    for offset, week in enumerate((THROUGH - 1, THROUGH)):
        weeks[week] = WeekUsage(
            week=week,
            points_ppr=points[offset],
            carries=carries[offset],
            target_share=target_share[offset],
            snap_pct=snaps[offset],
        )
    return Usage(
        gsis_id=gsis_id, name=name, position=position, team="SEA", weeks=weeks
    )


def _find(entries, rostered=(), trending=None, **kwargs):
    return waiver.find_candidates(
        {u.gsis_id: u for u in entries},
        through_week=THROUGH,
        rostered_gsis_ids=set(rostered),
        trending_by_name=trending or {},
        **kwargs,
    )


# --- filtering -----------------------------------------------------------------


def test_a_real_role_change_is_surfaced():
    found = _find([_usage("a", "Riser")])
    assert [c.name for c in found] == ["Riser"]
    assert found[0].snap_jump == pytest.approx(0.60)


def test_a_player_already_on_the_roster_is_excluded():
    assert _find([_usage("a", "Mine")], rostered=["a"]) == []


def test_a_snap_jump_below_the_noise_floor_is_ignored():
    small = _usage("a", "Noise", snaps=(0.70, 0.70 + waiver.MIN_SNAP_JUMP - 0.01))
    assert _find([small]) == []


def test_a_jump_at_the_noise_floor_is_kept():
    exact = _usage("a", "Edge", snaps=(0.50, 0.50 + waiver.MIN_SNAP_JUMP))
    assert len(_find([exact])) == 1


def test_a_big_jump_to_a_still_small_role_is_ignored():
    """Two per cent to twenty is a large jump and still not a startable role."""
    tiny = _usage("a", "Backup", snaps=(0.02, 0.20))
    assert _find([tiny]) == []


def test_a_player_with_only_one_week_is_ignored():
    single = Usage(
        gsis_id="a",
        name="Rookie",
        position="RB",
        weeks={THROUGH: WeekUsage(week=THROUGH, snap_pct=0.9)},
        team="SEA",
    )
    assert _find([single]) == []


def test_a_player_with_no_snap_data_is_ignored():
    no_snaps = _usage("a", "Ghost", snaps=(None, None))
    assert _find([no_snaps]) == []


@pytest.mark.parametrize("position", ["K", "DEF", "LB", "CB"])
def test_a_non_claimable_position_is_ignored(position):
    assert _find([_usage("a", "Specialist", position=position)]) == []


def test_a_week_after_the_cutoff_is_not_consulted():
    entry = _usage("a", "Riser")
    entry.weeks[THROUGH + 1] = WeekUsage(week=THROUGH + 1, snap_pct=0.99)
    found = waiver.find_candidates(
        {"a": entry},
        through_week=THROUGH,
        rostered_gsis_ids=set(),
        trending_by_name={},
    )
    assert found[0].snap_share == pytest.approx(0.80)


# --- ranking -------------------------------------------------------------------


def test_the_bigger_role_change_ranks_higher():
    small = _usage("a", "Small", snaps=(0.50, 0.65))
    big = _usage("b", "Big", snaps=(0.20, 0.90))
    assert [c.name for c in _find([small, big])] == ["Big", "Small"]


def test_the_limit_is_respected():
    entries = [_usage(str(i), f"Player {i}") for i in range(20)]
    assert len(_find(entries, limit=5)) == 5


def test_ranking_is_deterministic_for_identical_scores():
    a = _usage("a", "Bravo")
    b = _usage("b", "Alpha")
    assert [c.name for c in _find([a, b])] == ["Alpha", "Bravo"]


def test_a_quiet_candidate_is_flagged_and_a_chased_one_is_not():
    entries = [_usage("a", "Quiet Guy"), _usage("b", "Chased Guy")]
    found = _find(entries, trending={"Chased Guy": 4_000_000, "Quiet Guy": 500})
    by_name = {c.name: c for c in found}
    assert by_name["Quiet Guy"].unnoticed is True
    assert by_name["Chased Guy"].unnoticed is False


def test_a_missing_trending_entry_counts_as_zero_adds():
    found = _find([_usage("a", "Unknown To Sleeper")], trending={})
    assert found[0].trending_adds == 0
    assert found[0].unnoticed is True


# --- bids ----------------------------------------------------------------------


def test_bid_scales_with_the_role_strength():
    low = waiver.suggested_bid(0.10, 0, 100)
    high = waiver.suggested_bid(0.90, 0, 100)
    assert high > low


def test_bid_scales_with_the_budget():
    assert waiver.suggested_bid(0.90, 0, 200) > waiver.suggested_bid(0.90, 0, 100)


def test_bid_rises_for_a_contested_player():
    """Ranking prefers the unnoticed player; winning a chased one costs more."""
    quiet = waiver.suggested_bid(0.90, 0, 100)
    chased = waiver.suggested_bid(0.90, waiver.SATURATION_ADDS, 100)
    assert chased > quiet


def test_bid_is_never_zero_when_there_is_a_budget():
    """A zero-dollar claim wins only when nobody else bids."""
    assert waiver.suggested_bid(0.0, 0, 100) >= 1
    assert waiver.suggested_bid(0.0, 0, 1) >= 1


def test_bid_is_zero_when_the_budget_is_spent():
    """A one-dollar floor would exceed a spent budget."""
    assert waiver.suggested_bid(0.95, 0, 0) == 0


def test_bid_never_exceeds_the_budget():
    for strength in (0.0, 0.25, 0.5, 0.75, 1.0):
        for adds in (0, waiver.QUIET_ADDS, waiver.SATURATION_ADDS * 3):
            bid = waiver.suggested_bid(strength, adds, 100)
            assert 1 <= bid <= 100, f"{strength} {adds} gave {bid}"


def test_a_tiny_budget_still_yields_a_whole_dollar():
    assert waiver.suggested_bid(0.95, 0, 3) >= 1


# --- scarcity affects the ranking, not only the label --------------------------


def test_scarcity_is_one_for_an_unnoticed_player_and_zero_when_saturated():
    assert waiver.scarcity(0) == pytest.approx(1.0)
    assert waiver.scarcity(waiver.SATURATION_ADDS) == pytest.approx(0.0)
    assert waiver.scarcity(waiver.SATURATION_ADDS * 5) == pytest.approx(0.0)
    assert waiver.scarcity(-50) == pytest.approx(1.0)


def test_a_quiet_player_outranks_a_chased_player_of_equal_role_strength():
    """The stated goal is unpriced changes, so demand must move the order."""
    quiet = _usage("a", "Quiet Guy")
    chased = _usage("b", "Chased Guy")
    found = _find(
        [quiet, chased],
        trending={"Chased Guy": waiver.SATURATION_ADDS, "Quiet Guy": 0},
    )
    assert [c.name for c in found] == ["Quiet Guy", "Chased Guy"]


def test_scarcity_cannot_promote_a_clearly_weaker_candidate():
    """It breaks near ties only, so a much better role change still wins."""
    weak_quiet = _usage("a", "Weak Quiet", snaps=(0.50, 0.63), carries=(1.0, 2.0))
    strong_chased = _usage("b", "Strong Chased", snaps=(0.20, 0.95))
    found = _find(
        [weak_quiet, strong_chased],
        trending={"Strong Chased": waiver.SATURATION_ADDS * 2, "Weak Quiet": 0},
    )
    assert found[0].name == "Strong Chased"


# --- rendering -----------------------------------------------------------------


def test_render_reports_an_empty_week_as_a_valid_answer():
    text = report.render_waivers([], week=4, budget=100, bids={})
    assert "Nothing to claim is a valid answer" in text


def test_render_lists_the_bid_and_the_signal():
    found = _find([_usage("a", "Riser")])
    bids = {"Riser": 18}
    text = report.render_waivers(found, week=4, budget=100, bids=bids)
    assert "$18 Riser" in text
    assert "snaps 80%" in text
    assert "+60%" in text


def test_render_warns_about_the_stopgap_limitation():
    found = _find([_usage("a", "Riser")])
    text = report.render_waivers(found, 4, 100, {"Riser": 5}, stopgap=True)
    assert "already be rostered" in text
    assert "already be rostered" not in report.render_waivers(
        found, 4, 100, {"Riser": 5}, stopgap=False
    )


def test_waiver_fit_drops_the_lowest_ranked_claims_first():
    entries = [_usage(str(i), f"Player Number {i}") for i in range(8)]
    found = _find(entries)
    bids = {c.name: 5 for c in found}
    fitted = report.fit_waivers_to_ntfy(found, 4, 100, bids, max_bytes=300)

    assert len(fitted.encode("utf-8")) <= 300
    assert found[0].name in fitted, "the top claim must survive trimming"
    assert found[-1].name not in fitted


def test_a_real_sized_waiver_sheet_fits_a_notification():
    entries = [_usage(str(i), f"Player Number {i}") for i in range(8)]
    found = _find(entries)
    bids = {c.name: 12 for c in found}
    text = report.fit_waivers_to_ntfy(found, 4, 100, bids)
    assert len(text.encode("utf-8")) <= report.NTFY_MAX_BYTES
