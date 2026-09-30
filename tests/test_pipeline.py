"""Tests for usage assembly, roster parsing, report rendering and ntfy delivery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import notify, report, roster  # noqa: E402
from faab.league import League  # noqa: E402
from faab.model.lineup import recommend_lineup  # noqa: E402
from faab.model.project import Projection  # noqa: E402
from faab.model.usage import Usage, WeekUsage, build_usage, kicker_points  # noqa: E402
from faab.names import NameIndex, Player  # noqa: E402


# --- usage ---------------------------------------------------------------------


def _usage_with(points_by_week):
    return Usage(
        gsis_id="x",
        name="X",
        position="WR",
        team="SEA",
        weeks={w: WeekUsage(week=w, points_ppr=p) for w, p in points_by_week.items()},
    )


def test_weighted_recent_points_weights_the_most_recent_week_heaviest():
    usage = _usage_with({1: 0.0, 2: 0.0, 3: 12.0})
    # weights 3,2,1 newest first: (3*12 + 2*0 + 1*0) / 6
    assert usage.weighted_recent_points(3) == pytest.approx(6.0)


def test_weighted_recent_points_ignores_a_week_after_the_cutoff():
    usage = _usage_with({1: 10.0, 2: 10.0, 3: 100.0})
    assert usage.weighted_recent_points(2) == pytest.approx(10.0)


def test_weighted_recent_points_uses_only_the_last_three_weeks_played():
    usage = _usage_with({1: 100.0, 2: 0.0, 3: 0.0, 4: 0.0})
    assert usage.weighted_recent_points(4) == pytest.approx(0.0)


def test_weighted_recent_points_is_zero_without_any_week():
    assert _usage_with({}).weighted_recent_points(5) == 0.0


def test_snap_trend_is_none_without_two_weeks_of_snap_data():
    usage = Usage(
        gsis_id="x",
        name="X",
        position="WR",
        team="SEA",
        weeks={1: WeekUsage(week=1, snap_pct=0.5)},
    )
    assert usage.snap_trend(1) is None


def test_snap_trend_is_the_change_between_the_last_two_weeks():
    usage = Usage(
        gsis_id="x",
        name="X",
        position="WR",
        team="SEA",
        weeks={
            1: WeekUsage(week=1, snap_pct=0.20),
            2: WeekUsage(week=2, snap_pct=0.75),
        },
    )
    assert usage.snap_trend(2) == pytest.approx(0.55)


def test_snap_trend_is_none_when_one_week_lacks_snaps():
    usage = Usage(
        gsis_id="x",
        name="X",
        position="WR",
        team="SEA",
        weeks={
            1: WeekUsage(week=1, snap_pct=None),
            2: WeekUsage(week=2, snap_pct=0.75),
        },
    )
    assert usage.snap_trend(2) is None


def test_opportunity_share_uses_carries_for_a_back():
    usage = Usage(
        gsis_id="x",
        name="X",
        position="RB",
        team="SEA",
        weeks={2: WeekUsage(week=2, carries=20.0, target_share=0.05)},
    )
    assert usage.opportunity_share(2) == pytest.approx(1.0)


def test_opportunity_share_for_a_back_is_capped_at_one():
    usage = Usage(
        gsis_id="x",
        name="X",
        position="RB",
        team="SEA",
        weeks={2: WeekUsage(week=2, carries=40.0)},
    )
    assert usage.opportunity_share(2) == pytest.approx(1.0)


def test_opportunity_share_uses_target_share_for_a_receiver():
    usage = Usage(
        gsis_id="x",
        name="X",
        position="WR",
        team="SEA",
        weeks={2: WeekUsage(week=2, target_share=0.31, carries=20.0)},
    )
    assert usage.opportunity_share(2) == pytest.approx(0.31)


def test_kicker_points_use_distance_buckets():
    row = {
        "fg_made_20_29": "1",
        "fg_made_40_49": "1",
        "fg_made_50_59": "1",
        "pat_made": "2",
    }
    assert kicker_points(row) == pytest.approx(3 + 4 + 5 + 2)


def test_kicker_points_are_zero_for_a_blank_row():
    assert kicker_points({}) == 0.0


def test_build_usage_joins_snaps_through_the_player_directory():
    stats = [
        {
            "player_id": "00-1",
            "player_display_name": "Snap Guy",
            "position": "WR",
            "team": "SEA",
            "week": "2",
            "fantasy_points_ppr": "14.5",
            "target_share": "0.28",
        }
    ]
    snaps = [{"pfr_player_id": "PfrA", "week": "2", "offense_pct": "0.82"}]
    players = [{"pfr_id": "PfrA", "gsis_id": "00-1"}]

    usage = build_usage(stats, snaps, players, through_week=2)
    assert usage["00-1"].weeks[2].snap_pct == pytest.approx(0.82)
    assert usage["00-1"].weeks[2].points_ppr == pytest.approx(14.5)


def test_build_usage_computes_kicker_points_rather_than_reading_zero():
    stats = [
        {
            "player_id": "00-K",
            "player_display_name": "Kicker Guy",
            "position": "K",
            "team": "SEA",
            "week": "2",
            "fantasy_points_ppr": "0",
            "fg_made_40_49": "2",
            "pat_made": "3",
        }
    ]
    usage = build_usage(stats, [], [], through_week=2)
    assert usage["00-K"].weeks[2].points_ppr == pytest.approx(2 * 4 + 3)


def test_build_usage_drops_a_week_after_the_cutoff():
    stats = [
        {"player_id": "a", "week": "2", "position": "WR", "fantasy_points_ppr": "5"},
        {"player_id": "a", "week": "3", "position": "WR", "fantasy_points_ppr": "50"},
    ]
    usage = build_usage(stats, [], [], through_week=2)
    assert list(usage["a"].weeks) == [2]


def test_build_usage_ignores_a_snap_row_for_an_unknown_player():
    snaps = [{"pfr_player_id": "ghost", "week": "2", "offense_pct": "0.9"}]
    assert build_usage([], snaps, [], through_week=2) == {}


def test_build_usage_keeps_the_most_recent_team_after_a_trade():
    stats = [
        {"player_id": "a", "week": "1", "position": "WR", "team": "SEA"},
        {"player_id": "a", "week": "2", "position": "WR", "team": "GB"},
    ]
    assert build_usage(stats, [], [], through_week=2)["a"].team == "GB"


# --- roster parsing ------------------------------------------------------------


def test_comments_and_blank_lines_are_skipped():
    lines = roster.parse_roster_file("# a comment\n\n  \nJosh Allen\n")
    assert [line.name for line in lines] == ["Josh Allen"]


def test_a_trailing_comment_is_stripped():
    lines = roster.parse_roster_file("Josh Allen  # my guy\n")
    assert lines[0].name == "Josh Allen"


def test_position_and_team_hints_are_parsed():
    line = roster.parse_roster_file("Puka Nacua WR LA\n")[0]
    assert (line.name, line.position, line.team) == ("Puka Nacua", "WR", "LA")


def test_dst_is_normalized_to_def():
    assert roster.parse_roster_file("Seahawks DST\n")[0].position == "DEF"


def test_a_name_survives_when_no_hint_is_given():
    line = roster.parse_roster_file("Amon-Ra St. Brown\n")[0]
    assert line.name == "Amon-Ra St. Brown"
    assert line.position == "" and line.team == ""


def test_line_numbers_track_the_original_file():
    lines = roster.parse_roster_file("# c\n\nJosh Allen\n\nPuka Nacua\n")
    assert [line.line_number for line in lines] == [3, 5]


def test_a_duplicate_line_is_reported_not_double_counted():
    index = NameIndex(
        [Player(name="Josh Allen", position="QB", team="BUF", gsis_id="1")]
    )
    lines = roster.parse_roster_file("Josh Allen\nJosh Allen\n")
    resolved, failed = roster.resolve_roster(lines, index)
    assert len(resolved) == 1
    assert len(failed) == 1
    assert "duplicate" in failed[0].reason


def test_an_unresolved_line_carries_its_number_and_text():
    index = NameIndex([Player(name="Josh Allen", position="QB", team="BUF")])
    lines = roster.parse_roster_file("Josh Allen\nWho Dis\n")
    _, failed = roster.resolve_roster(lines, index)
    assert failed[0].line.line_number == 2
    assert failed[0].line.raw == "Who Dis"


# --- report rendering ----------------------------------------------------------


def _lineup(count: int):
    players = [
        Player(name=f"Player Number {i}", position="WR", team="SEA", gsis_id=str(i))
        for i in range(count)
    ]
    usage = {
        p.gsis_id: Usage(
            gsis_id=p.gsis_id,
            name=p.name,
            position="WR",
            team="SEA",
            weeks={2: WeekUsage(week=2, points_ppr=float(count - i), snap_pct=0.5)},
        )
        for i, p in enumerate(players)
    }
    projections = {
        p.gsis_id: Projection(
            gsis_id=p.gsis_id,
            name=p.name,
            position="WR",
            team="SEA",
            points=float(count - i),
            games_played=2,
        )
        for i, p in enumerate(players)
    }
    league = League(slots={"WR": min(2, count)})
    return recommend_lineup(
        players, usage, projections, set(), 3, league, through_week=2
    )


def test_render_includes_the_week_and_every_slot():
    text = report.render_lineup(_lineup(4))
    assert "WEEK 3 LINEUP" in text
    assert text.count("WR   ") >= 2


def test_render_states_how_the_number_was_produced():
    """The footer used to disclaim being a projection. It is one now, so it says so."""
    text = report.render_lineup(_lineup(3))
    assert "Projected from usage" in text
    assert "touchdown rates pulled to league average" in text


def test_an_unfilled_slot_is_shouted_about():
    players = [Player(name="Only QB", position="QB", team="SEA", gsis_id="q")]
    lineup = recommend_lineup(
        players, {}, {}, set(), 3, League(slots={"QB": 1, "RB": 1}), through_week=2
    )
    assert "NOBODY ELIGIBLE" in report.render_lineup(lineup)


def test_fit_to_ntfy_keeps_a_small_report_whole():
    lineup = _lineup(5)
    assert report.fit_to_ntfy(lineup) == report.render_lineup(lineup)


def test_fit_to_ntfy_drops_sections_until_it_fits():
    lineup = _lineup(40)
    fitted = report.fit_to_ntfy(lineup, max_bytes=400)
    assert len(fitted.encode("utf-8")) <= 400
    assert "WEEK 3 LINEUP" in fitted, "the lineup itself must survive trimming"


def test_fit_to_ntfy_drops_the_bench_and_keeps_the_close_calls():
    """Pins the trim ORDER, not just that something was dropped.

    The budget here is exactly enough for the report without its bench. An earlier
    version of this test only checked that BENCH had gone, so it passed even when every
    optional section was dropped at once.
    """
    lineup = _lineup(12)
    assert lineup.close_calls, "fixture must produce a close call to be meaningful"

    without_bench = report.render_lineup(lineup, ("close_calls", "warnings"))
    fitted = report.fit_to_ntfy(lineup, max_bytes=len(without_bench.encode("utf-8")))

    assert fitted == without_bench
    assert "BENCH" not in fitted
    assert "CLOSE CALLS" in fitted


def test_fit_to_ntfy_keeps_the_close_calls_until_the_bench_alone_is_not_enough():
    """One byte tighter than the bench-only trim, so close calls must go too."""
    lineup = _lineup(12)
    without_bench = report.render_lineup(lineup, ("close_calls", "warnings"))
    fitted = report.fit_to_ntfy(lineup, max_bytes=len(without_bench.encode("utf-8")) - 1)

    assert "CLOSE CALLS" not in fitted
    assert "BENCH" not in fitted
    assert "WEEK 3 LINEUP" in fitted


def test_fit_to_ntfy_returns_an_oversize_body_rather_than_cutting_a_lineup():
    """Better an attachment the owner can open than half a lineup."""
    lineup = _lineup(60)
    fitted = report.fit_to_ntfy(lineup, max_bytes=50)
    assert len(fitted.encode("utf-8")) > 50, "the mandatory sections were cut"
    assert "WEEK 3 LINEUP" in fitted
    for choice in lineup.choices:
        assert choice.starter.player.name in fitted


def test_a_real_sized_report_fits_a_notification():
    """The whole point of the byte budget: a 15-player roster must fit."""
    assert len(report.fit_to_ntfy(_lineup(15)).encode("utf-8")) <= report.NTFY_MAX_BYTES


# --- ntfy ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_topic_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(notify.TOPIC_ENV, " my-topic ")
    assert notify.resolve_topic() == "my-topic"


def test_an_explicit_topic_wins(monkeypatch):
    monkeypatch.setenv(notify.TOPIC_ENV, "env-topic")
    assert notify.resolve_topic("passed-topic") == "passed-topic"


def test_a_missing_topic_is_an_error(monkeypatch):
    monkeypatch.delenv(notify.TOPIC_ENV, raising=False)
    with pytest.raises(notify.NotifyError, match="no ntfy topic"):
        notify.resolve_topic()


def test_a_topic_with_a_slash_is_rejected():
    with pytest.raises(notify.NotifyError, match="must not contain a slash"):
        notify.resolve_topic("a/b")


def test_send_posts_to_the_topic_url(monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data, headers=headers, timeout=timeout)
        return _FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.send("hello", title="Week 3", topic="t0pic")

    assert seen["url"] == "https://ntfy.sh/t0pic"
    assert seen["data"] == b"hello"
    assert seen["headers"]["Title"] == "Week 3"
    assert seen["timeout"] == notify.TIMEOUT_SECONDS


def test_send_honours_a_custom_server(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda url, **kw: (seen.update(url=url), _FakeResponse())[1],
    )
    notify.send("x", title="t", topic="topic", server="https://push.example.com/")
    assert seen["url"] == "https://push.example.com/topic"


def test_send_refuses_an_oversize_body(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("a request was made despite an oversize body")

    monkeypatch.setattr(notify.requests, "post", explode)
    with pytest.raises(notify.NotifyError, match="over ntfy's"):
        notify.send("x" * (report.NTFY_MAX_BYTES + 1), title="t", topic="topic")


def test_send_refuses_an_empty_body(monkeypatch):
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: _FakeResponse())
    with pytest.raises(notify.NotifyError, match="empty notification"):
        notify.send("", title="t", topic="topic")


def test_send_raises_on_a_non_200(monkeypatch):
    monkeypatch.setattr(
        notify.requests, "post", lambda *a, **k: _FakeResponse(429, "slow down")
    )
    with pytest.raises(notify.NotifyError, match="HTTP 429"):
        notify.send("x", title="t", topic="topic")


def test_send_wraps_a_transport_error(monkeypatch):
    def boom(*a, **k):
        raise notify.requests.ConnectionError("no route")

    monkeypatch.setattr(notify.requests, "post", boom)
    with pytest.raises(notify.NotifyError, match="publish failed"):
        notify.send("x", title="t", topic="topic")


def test_a_non_ascii_title_does_not_break_the_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda url, **kw: (seen.update(kw), _FakeResponse())[1],
    )
    notify.send("body", title="Week 3 \u2014 lineup", topic="topic")
    seen["headers"]["Title"].encode("ascii")


# --- schedule gate -------------------------------------------------------------


def test_parse_schedule_accepts_a_day_and_hour():
    from faab.cli import parse_schedule

    assert parse_schedule("sun:9") == (6, 9)
    assert parse_schedule("TUE:20") == (1, 20)
    assert parse_schedule(" mon:0 ") == (0, 0)


@pytest.mark.parametrize(
    "spec", ["sunday", "sun", "sun:24", "sun:-1", "funday:9", "sun:9:30", "", "9:sun"]
)
def test_parse_schedule_rejects_junk(spec):
    from faab.cli import parse_schedule

    with pytest.raises(ValueError):
        parse_schedule(spec)


def test_the_gate_fires_only_in_the_matching_local_hour():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from faab.cli import is_scheduled_now

    et = ZoneInfo("America/New_York")
    sunday_9am = datetime(2026, 9, 27, 9, 30, tzinfo=et)
    assert is_scheduled_now("sun:9", "America/New_York", sunday_9am)
    assert not is_scheduled_now("sun:10", "America/New_York", sunday_9am)
    assert not is_scheduled_now("mon:9", "America/New_York", sunday_9am)


def test_the_gate_is_evaluated_in_local_time_not_utc():
    """A UTC-hour gate would drift by one hour when daylight saving changes."""
    from datetime import datetime, timezone

    from faab.cli import is_scheduled_now

    # 13:30 UTC is 09:30 EDT in September and 08:30 EST in December.
    september = datetime(2026, 9, 27, 13, 30, tzinfo=timezone.utc)
    december = datetime(2026, 12, 27, 13, 30, tzinfo=timezone.utc)

    assert is_scheduled_now("sun:9", "America/New_York", september)
    assert not is_scheduled_now("sun:9", "America/New_York", december)
    assert is_scheduled_now("sun:8", "America/New_York", december)


def test_an_unknown_timezone_is_reported():
    from faab.cli import is_scheduled_now

    with pytest.raises(ValueError, match="unknown timezone"):
        is_scheduled_now("sun:9", "Mars/Olympus_Mons")
