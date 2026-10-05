"""Tests for the game-day lineup check.

The check exists for one outcome: a starter who will not take the field is reported while
there is still time to move him. Most tests here are about that outcome and about the ways
it could silently not happen: a check skipped, a failure that looks like all clear, an
alert recorded as sent when it was not.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import cli, gameday, notify  # noqa: E402
from faab.collectors import yahoo  # noqa: E402
from faab.collectors.yahoo import YahooPlayer  # noqa: E402
from faab.gameday import Kickoff, State  # noqa: E402

UTC = timezone.utc


def entry(
    name,
    slot,
    team="PHI",
    position="WR",
    status="",
    editable=True,
    eligible=None,
    key=None,
    status_full="",
    note="",
):
    return YahooPlayer(
        player_key=key or f"p.{name.replace(' ', '')}",
        name=name,
        position=position,
        eligible=tuple(eligible or ([position, "W/R/T"] if position in ("WR", "RB", "TE") else [position])),
        team=team,
        status=status,
        status_full=status_full,
        injury_note=note,
        selected_position=slot,
        is_editable=editable,
        bye_week=None,
    )


def game(day, clock, home, away, week=5, season=2026):
    return {
        "season": str(season),
        "week": str(week),
        "gameday": day,
        "gametime": clock,
        "home_team": home,
        "away_team": away,
    }


GAMES = [
    game("2026-10-08", "20:15", "NE", "BUF"),
    game("2026-10-11", "13:00", "PHI", "DAL"),
    game("2026-10-11", "13:00", "MIA", "NYJ"),
    game("2026-10-11", "16:25", "SF", "LA"),
    game("2026-10-12", "20:15", "KC", "DEN"),
    game("2026-10-01", "20:15", "PIT", "CLE", week=4),
    game("2026-10-18", "13:00", "TB", "ATL", week=6),
]


def sunday_early():
    return next(k for k in gameday.kickoffs(GAMES, 2026) if "PHI" in k.teams)


# --- kickoffs -----------------------------------------------------------------------


def test_games_at_the_same_time_form_one_kickoff():
    kicks = gameday.kickoffs(GAMES, 2026)
    early = sunday_early()
    assert early.teams == {"PHI", "DAL", "MIA", "NYJ"}
    assert len([k for k in kicks if k.week == 5]) == 4


def test_eastern_time_is_converted_across_daylight_saving():
    kicks = gameday.kickoffs(
        [game("2026-10-11", "13:00", "A", "B"), game("2026-11-08", "13:00", "C", "D", week=10)],
        2026,
    )
    assert kicks[0].when.astimezone(UTC).hour == 17
    assert kicks[1].when.astimezone(UTC).hour == 18


def test_a_row_without_a_kickoff_time_is_skipped_not_fatal():
    rows = [game("2026-10-11", "", "A", "B"), game("2026-10-11", "13:00", "C", "D")]
    assert [k.teams for k in gameday.kickoffs(rows, 2026)] == [{"C", "D"}]


def test_other_seasons_are_ignored():
    rows = [game("2025-10-11", "13:00", "A", "B", season=2025)]
    assert gameday.kickoffs(rows, 2026) == []


# --- which checks are due ---------------------------------------------------------------


def _at(kick: Kickoff, minutes_before: float) -> datetime:
    return kick.when - timedelta(minutes=minutes_before)


def test_nothing_is_due_before_the_first_check():
    kick = sunday_early()
    assert gameday.due([kick], _at(kick, 76), State()) == []


def test_the_first_check_is_due_75_minutes_before():
    kick = sunday_early()
    assert gameday.due([kick], _at(kick, 75), State()) == [(kick, [kick.key(75)])]


def test_a_done_check_is_not_repeated():
    kick = sunday_early()
    state = State(checks={kick.key(75): "done"})
    assert gameday.due([kick], _at(kick, 40), state) == []


def test_the_last_check_is_due_15_minutes_before():
    kick = sunday_early()
    state = State(checks={kick.key(75): "done"})
    assert gameday.due([kick], _at(kick, 15), state) == [(kick, [kick.key(15)])]


def test_a_missed_first_check_is_made_up_with_the_last():
    """A box that was asleep at T-75 must still check once, not skip the kickoff."""
    kick = sunday_early()
    assert gameday.due([kick], _at(kick, 10), State()) == [(kick, [kick.key(75), kick.key(15)])]


def test_a_failed_check_stays_due():
    kick = sunday_early()
    state = State(checks={kick.key(75): "failed_notified"})
    assert gameday.due([kick], _at(kick, 70), state) == [(kick, [kick.key(75)])]


def test_nothing_is_due_once_the_game_has_started():
    kick = sunday_early()
    assert gameday.due([kick], _at(kick, 0), State()) == []


# --- problems ---------------------------------------------------------------------------


PLAYING = {"PHI", "DAL", "MIA", "NYJ", "SF", "LA", "NE", "BUF", "KC", "DEN"}
KNOWN = PLAYING | {"PIT", "CLE", "TB", "ATL"}


def test_an_inactive_starter_is_reported_with_a_bench_replacement():
    roster = [
        entry("Sam Out", "WR", status="NA", status_full="Inactive: Coach's Decision"),
        entry("Ben Ready", "BN", team="SF"),
    ]
    problems = gameday.find_problems(roster, sunday_early(), PLAYING)
    assert [(p.player.name, p.kind) for p in problems] == [("Sam Out", "out")]
    assert problems[0].replacements == ("Ben Ready",)
    assert problems[0].urgent


@pytest.mark.parametrize("code", ["O", "IR", "IR-R", "PUP-R", "SUSP", "NA", "CEL", "NFI-R"])
def test_every_will_not_play_code_is_urgent(code):
    problems = gameday.find_problems([entry("X Y", "WR", status=code)], sunday_early(), PLAYING)
    assert [p.kind for p in problems] == ["out"]


@pytest.mark.parametrize("code", ["Q", "D", "SOMETHING-NEW"])
def test_an_uncertain_or_unknown_code_is_a_warning_not_silence(code):
    problems = gameday.find_problems([entry("X Y", "WR", status=code)], sunday_early(), PLAYING)
    assert [p.kind for p in problems] == ["warn"]


def test_a_healthy_starter_is_not_reported():
    assert gameday.find_problems([entry("X Y", "WR")], sunday_early(), PLAYING) == []


def test_a_benched_out_player_is_not_reported():
    assert gameday.find_problems([entry("X Y", "BN", status="O")], sunday_early(), PLAYING) == []


def test_a_starter_in_a_later_kickoff_waits_for_his_own_check():
    roster = [entry("Late Out", "WR", team="SF", status="O")]
    assert gameday.find_problems(roster, sunday_early(), PLAYING) == []


def test_a_locked_starter_is_skipped():
    roster = [entry("Locked Out", "WR", status="O", editable=False)]
    assert gameday.find_problems(roster, sunday_early(), PLAYING) == []


def test_a_starter_on_bye_is_reported_once_per_week():
    roster = [entry("Bye Guy", "WR", team="TB", key="p.bye")]
    first = gameday.find_problems(roster, sunday_early(), PLAYING, known_teams=KNOWN)
    assert [p.kind for p in first] == ["bye"]
    again = gameday.find_problems(
        roster, sunday_early(), PLAYING, already_alerted_byes={"p.bye"}, known_teams=KNOWN
    )
    assert again == []


@pytest.mark.parametrize("team", ["XYZ", ""])
def test_a_team_the_schedule_does_not_know_is_a_warning_not_a_bye(team):
    """A misspelt abbreviation must not become a confident 'not playing this week'."""
    roster = [entry("Odd Team", "WR", team=team)]
    problems = gameday.find_problems(roster, sunday_early(), PLAYING, known_teams=KNOWN)
    assert [p.kind for p in problems] == ["warn"]
    assert "not in the schedule" in problems[0].reason


def test_missing_starters_counts_empty_slots():
    roster = [entry("A", "WR"), entry("B", "BN"), entry("C", "IR")]
    assert gameday.missing_starters(roster, 3) == 2
    assert gameday.missing_starters(roster, 1) == 0


def test_an_empty_slot_is_named_in_the_alert():
    text = gameday.render([], sunday_early(), "America/New_York", missing=2)
    assert "EMPTY 2 starting slot(s)" in text


def test_kickoffs_are_read_in_eastern_whatever_the_league_zone():
    kick = gameday.kickoffs([game("2026-10-11", "13:00", "A", "B")], 2026)[0]
    assert kick.when.astimezone(UTC).hour == 17


def test_replacements_exclude_locked_out_bye_and_ineligible_players():
    roster = [
        entry("Sam Out", "WR", status="O"),
        entry("Locked Bench", "BN", editable=False),
        entry("Hurt Bench", "BN", status="O"),
        entry("Bye Bench", "BN", team="TB"),
        entry("Wrong Position", "BN", position="QB"),
        entry("Good Bench", "BN", team="DAL"),
    ]
    problem = gameday.find_problems(roster, sunday_early(), PLAYING)[0]
    assert problem.replacements == ("Good Bench",)


def test_a_flex_starter_can_be_replaced_by_any_flex_eligible_player():
    roster = [
        entry("Flex Out", "W/R/T", position="RB", status="O"),
        entry("Tight End", "BN", position="TE", team="SF"),
    ]
    problem = gameday.find_problems(roster, sunday_early(), PLAYING)[0]
    assert problem.replacements == ("Tight End",)


def test_replacements_are_ordered_by_projection_when_one_is_given():
    roster = [
        entry("Sam Out", "WR", status="O"),
        entry("Low", "BN"),
        entry("High", "BN"),
        entry("Unknown", "BN"),
    ]
    points = {"Low": 3.0, "High": 9.5}
    problem = gameday.find_problems(
        roster, sunday_early(), PLAYING, points=lambda e: points.get(e.name)
    )[0]
    assert problem.replacements == ("High (9.5)", "Low (3.0)")


def test_the_alert_names_the_player_slot_reason_and_replacement():
    roster = [entry("Sam Out", "WR", status="O", status_full="Out", note="Foot"), entry("Ben Ready", "BN")]
    text = gameday.render(gameday.find_problems(roster, sunday_early(), PLAYING), sunday_early(), "America/New_York")
    assert "OUT WR Sam Out PHI: Out, Foot" in text
    assert "start instead: Ben Ready" in text
    assert "1:00pm Sun" in text


def test_an_alert_with_no_replacement_says_so():
    text = gameday.render(
        gameday.find_problems([entry("Sam Out", "WR", status="O")], sunday_early(), PLAYING),
        sunday_early(),
        "America/New_York",
    )
    assert "no healthy bench player" in text


def test_an_oversized_alert_drops_replacements_first():
    roster = [entry(f"Player {i} " + "X" * 60, "WR", status="O") for i in range(30)]
    roster += [entry(f"Bench {i} " + "Y" * 60, "BN") for i in range(5)]
    problems = gameday.find_problems(roster, sunday_early(), PLAYING)
    text = gameday.fit_to_ntfy(problems, sunday_early(), "America/New_York", max_bytes=4096)
    assert len(text.encode("utf-8")) <= 4096
    assert "OUT WR Player 0" in text


# --- state ------------------------------------------------------------------------------


def test_state_round_trips_and_drops_old_checks(tmp_path):
    now = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
    old = Kickoff(when=now - timedelta(days=20), week=2, teams=frozenset())
    new = Kickoff(when=now + timedelta(hours=1), week=5, teams=frozenset())
    state = State(checks={old.key(75): "done", new.key(75): "done"}, bye_alerts=["5:p.1"])
    state.save(tmp_path / "s.json", now)
    loaded = State.load(tmp_path / "s.json")
    assert loaded.checks == {new.key(75): "done"}
    assert loaded.bye_alerts == ["5:p.1"]


def test_a_damaged_state_file_starts_empty_rather_than_stopping_the_check(tmp_path):
    (tmp_path / "s.json").write_text("{not json", encoding="utf-8")
    assert State.load(tmp_path / "s.json").checks == {}


def test_a_second_run_cannot_take_the_lock_while_the_first_holds_it(tmp_path):
    lock = tmp_path / "gameday.lock"
    with gameday.single_run(lock) as first:
        with gameday.single_run(lock) as second:
            assert first is True and second is False
    with gameday.single_run(lock) as again:
        assert again is True


# --- the command, end to end with fakes ---------------------------------------------------


class FakeClient:
    def get(self, path):  # pragma: no cover - the readers are patched below
        raise AssertionError(path)


class Runner:
    """Runs `faab gameday` at a chosen moment, with Yahoo and ntfy faked."""

    def __init__(self, data: Path):
        self.data = data
        self.sent: list[dict[str, Any]] = []
        self.roster: list[YahooPlayer] = []
        self.error: str | None = None
        self.send_error: str | None = None
        self.schedule_error: str | None = None
        (data / "league.toml").write_text("[slots]\nWR = 1\n", encoding="utf-8")

    def games(self) -> list[dict[str, str]]:
        if self.schedule_error:
            raise cli.FetchError(self.schedule_error)
        return GAMES

    def __call__(self, moment: datetime, *extra: str) -> int:
        argv = ["--data", str(self.data), "--league", str(self.data / "league.toml"),
                "gameday", "--now", moment.isoformat(), *extra]
        return cli.main(argv)

    def state(self) -> State:
        return State.load(self.data / "gameday_state.json")


@pytest.fixture
def run(tmp_path, monkeypatch):
    runner = Runner(tmp_path)

    monkeypatch.setattr(cli.nflverse, "load_games", lambda data: runner.games())
    monkeypatch.setattr(cli, "_yahoo_client", lambda args: FakeClient())
    monkeypatch.setattr(cli.yahoo_state, "resolve_league_key", lambda client, league: "470.l.1")

    def teams(client, key):
        if runner.error:
            raise yahoo.YahooError(runner.error)
        return [yahoo.YahooTeam("470.l.1.t.2", "Mine", 100, 1, True)]

    monkeypatch.setattr(cli.yahoo, "league_teams", teams)
    monkeypatch.setattr(cli.yahoo, "team_roster", lambda client, key, week: runner.roster)
    monkeypatch.setattr(cli, "_points_for", lambda args, league, games: (lambda e: None))

    def fake_send(body, title, priority="default", tags="football", **kwargs):
        if runner.send_error:
            raise notify.NotifyError(runner.send_error)
        runner.sent.append({"body": body, "title": title, "priority": priority})

    monkeypatch.setattr(cli.notify, "send", fake_send)
    return runner


def test_an_out_starter_is_pushed_as_urgent_and_the_check_recorded(run):
    run.roster = [entry("Sam Out", "WR", status="O"), entry("Ben Ready", "BN")]
    kick = sunday_early()
    assert run(_at(kick, 70), "--send") == 0
    assert len(run.sent) == 1 and run.sent[0]["priority"] == "urgent"
    assert "Sam Out" in run.sent[0]["body"]
    assert run.state().checks == {kick.key(75): "done"}


def test_the_same_check_does_not_alert_twice(run):
    run.roster = [entry("Sam Out", "WR", status="O")]
    kick = sunday_early()
    run(_at(kick, 70), "--send")
    run(_at(kick, 65), "--send")
    assert len(run.sent) == 1


def test_a_problem_still_present_at_the_last_check_alerts_again(run):
    run.roster = [entry("Sam Out", "WR", status="O")]
    kick = sunday_early()
    run(_at(kick, 70), "--send")
    run(_at(kick, 14), "--send")
    assert len(run.sent) == 2


def test_an_all_clear_check_sends_nothing_and_is_recorded(run):
    run.roster = [entry("Fine", "WR")]
    kick = sunday_early()
    assert run(_at(kick, 70), "--send") == 0
    assert run.sent == []
    assert run.state().checks == {kick.key(75): "done"}


def test_a_yahoo_failure_is_pushed_once_and_the_check_stays_due(run):
    """A failure must never look like a quiet all clear."""
    run.error = "Yahoo returned 503"
    kick = sunday_early()
    assert run(_at(kick, 70), "--send") == 1
    assert run(_at(kick, 65), "--send") == 1
    assert [s["title"] for s in run.sent] == ["Lineup check failed"]
    assert run.state().checks == {kick.key(75): "failed_notified"}

    run.error = None
    run.roster = [entry("Sam Out", "WR", status="O")]
    assert run(_at(kick, 60), "--send") == 0
    assert run.sent[-1]["title"].startswith("Lineup alert")
    assert run.state().checks == {kick.key(75): "done"}


def test_a_delivery_failure_leaves_the_check_due_for_the_next_run(run):
    run.roster = [entry("Sam Out", "WR", status="O")]
    run.send_error = "ntfy down"
    kick = sunday_early()
    assert run(_at(kick, 70), "--send") == 2
    assert kick.key(75) not in run.state().checks

    run.send_error = None
    assert run(_at(kick, 65), "--send") == 0
    assert len(run.sent) == 1


def test_without_send_nothing_is_recorded(run):
    run.roster = [entry("Sam Out", "WR", status="O")]
    kick = sunday_early()
    run(_at(kick, 70))
    assert run.sent == []
    assert run.state().checks == {}


def test_force_checks_the_next_kickoff_now_and_records_nothing(run):
    run.roster = [entry("Sam Out", "WR", status="O")]
    kick = sunday_early()
    assert run(_at(kick, 600), "--force", "--send") == 0
    assert len(run.sent) == 1
    assert run.state().checks == {}


def test_no_yahoo_call_is_made_when_nothing_is_due(run, monkeypatch):
    monkeypatch.setattr(cli, "_yahoo_client", lambda args: pytest.fail("Yahoo was called"))
    assert run(_at(sunday_early(), 600), "--send") == 0


def test_a_ranking_failure_still_sends_the_alert(run, monkeypatch):
    def broken(args, league, games):
        raise OSError("stats cache unreadable")

    monkeypatch.setattr(cli, "_points_for", broken)
    run.roster = [entry("Sam Out", "WR", status="O"), entry("Ben Ready", "BN")]
    assert run(_at(sunday_early(), 70), "--send") == 0
    assert "Ben Ready" in run.sent[0]["body"]


def test_a_bye_starter_is_alerted_once_across_kickoffs(run):
    run.roster = [entry("Bye Guy", "WR", team="TB", key="p.bye")]
    kicks = [k for k in gameday.kickoffs(GAMES, 2026) if k.week == 5]
    run(_at(kicks[0], 70), "--send")
    run(_at(kicks[1], 70), "--send")
    assert len(run.sent) == 1


def test_the_cli_parser_accepts_gameday():
    args = cli.build_parser().parse_args(["gameday", "--send", "--force"])
    assert isinstance(args, argparse.Namespace) and args.send and args.force


def test_an_empty_starting_slot_is_urgent_at_every_check_until_filled(run):
    """A slot emptied after an earlier alert must not be hidden by that alert."""
    run.roster = [entry("Only Bench", "BN")]
    kick = sunday_early()
    assert run(_at(kick, 70), "--send") == 0
    assert run.sent[0]["priority"] == "urgent" and "EMPTY 1" in run.sent[0]["body"]
    run(_at(kick, 14), "--send")
    assert len(run.sent) == 2

    later = next(k for k in gameday.kickoffs(GAMES, 2026) if "SF" in k.teams)
    run.roster = [entry("Filled", "WR", team="SF")]
    run(_at(later, 70), "--send")
    assert len(run.sent) == 2


def test_a_schedule_without_this_season_is_a_pushed_failure(run, monkeypatch):
    monkeypatch.setattr(run, "games", lambda: [game("2025-10-12", "13:00", "PHI", "DAL", season=2025)])
    assert run(_at(sunday_early(), 70), "--send") == 1
    assert [s["title"] for s in run.sent] == ["Lineup check failed"]
    assert "no 2026 games" in run.sent[0]["body"]


def test_an_out_starter_on_an_unknown_team_is_still_urgent():
    problems = gameday.find_problems(
        [entry("Odd Out", "WR", team="XYZ", status="O")], sunday_early(), PLAYING, known_teams=KNOWN
    )
    assert [p.kind for p in problems] == ["out"]
    assert "not in the schedule" in problems[0].reason


def test_a_schedule_outage_falls_back_to_the_cached_copy(run, monkeypatch):
    run.schedule_error = "github down"
    monkeypatch.setattr(cli.nflverse, "load_cached_games", lambda data, max_age: GAMES)
    run.roster = [entry("Sam Out", "WR", status="O")]
    assert run(_at(sunday_early(), 70), "--send") == 0
    assert run.sent[0]["title"].startswith("Lineup alert")


def test_no_schedule_at_all_is_pushed_once_a_day(run, monkeypatch):
    run.schedule_error = "github down"

    def no_cache(data, max_age):
        raise OSError("no cached schedule")

    monkeypatch.setattr(cli.nflverse, "load_cached_games", no_cache)
    moment = _at(sunday_early(), 70)
    assert run(moment, "--send") == 1
    assert run(moment + timedelta(minutes=5), "--send") == 1
    assert [s["title"] for s in run.sent] == ["Lineup check failed"]
    assert run(moment + timedelta(days=1), "--send") == 1
    assert len(run.sent) == 2


def test_state_is_saved_after_each_kickoff_so_a_later_crash_cannot_repeat_an_alert(run, monkeypatch):
    kicks = [k for k in gameday.kickoffs(GAMES, 2026) if k.week == 5][1:3]
    monkeypatch.setattr(
        cli.gameday_mod, "due", lambda kickoffs, now, state: [(k, [k.key(75)]) for k in kicks]
    )
    run.roster = [entry("Sam Out", "WR", status="O")]
    original = cli._gameday_check
    calls = []

    def crash_on_second(args, league, client, kickoff, *rest):
        calls.append(kickoff)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return original(args, league, client, kickoff, *rest)

    monkeypatch.setattr(cli, "_gameday_check", crash_on_second)
    moment = kicks[0].when - timedelta(minutes=70)
    with pytest.raises(RuntimeError):
        run(moment, "--send")
    assert run.state().checks.get(kicks[0].key(75)) == "done"


def test_a_cached_schedule_older_than_three_days_is_not_trusted(run, tmp_path):
    import os

    run.schedule_error = "github down"
    cache = tmp_path / "nflverse" / "games.csv"
    cache.parent.mkdir(parents=True)
    header = "season,week,gameday,gametime,home_team,away_team"
    rows = [f"{g['season']},{g['week']},{g['gameday']},{g['gametime']},{g['home_team']},{g['away_team']}" for g in GAMES]
    cache.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    run.roster = [entry("Sam Out", "WR", status="O")]
    moment = _at(sunday_early(), 70)

    assert run(moment, "--send") == 0
    assert run.sent[-1]["title"].startswith("Lineup alert")

    old = cache.stat().st_mtime - 4 * 24 * 3600
    os.utime(cache, (old, old))
    assert run(moment + timedelta(minutes=50), "--send") == 1
    assert run.sent[-1]["title"] == "Lineup check failed"


def test_an_unknown_team_warning_does_not_hide_a_later_out(run):
    kick = sunday_early()
    run.roster = [entry("Odd Team", "WR", team="XYZ", key="p.odd")]
    run(_at(kick, 70), "--send")
    run.roster = [entry("Odd Team", "WR", team="XYZ", key="p.odd", status="O")]
    run(_at(kick, 14), "--send")
    assert len(run.sent) == 2
    assert run.sent[1]["priority"] == "urgent"
