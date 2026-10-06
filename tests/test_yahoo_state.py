"""Tests for turning Yahoo's league view into the shapes the models take."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import cli, yahoo_state  # noqa: E402
from faab.collectors import yahoo  # noqa: E402
from faab.collectors.yahoo import YahooPlayer, YahooTeam  # noqa: E402
from faab.league import League  # noqa: E402
from faab.model.project import Projection  # noqa: E402
from faab.names import NameIndex, Player  # noqa: E402


def player(name, position="WR", team="PHI", gsis="", injury=""):
    return Player(name=name, position=position, team=team, gsis_id=gsis, status="ACT", injury_status=injury)


def defense(nickname, team, city):
    return Player(name=nickname, position="DEF", team=team, status="ACT", aliases=(f"{city} {nickname}", city, team))


INDEX = NameIndex(
    [
        player("Alan Starter", gsis="00-1"),
        player("Ben Bench", gsis="00-2", injury="OUT"),
        player("Cal Rival", position="RB", team="DAL", gsis="00-3"),
        player("Dee Free", position="TE", team="MIA", gsis="00-4"),
        player("Eve Free", position="QB", team="MIA", gsis="00-5"),
        player("Same Name", team="NYJ", gsis="00-6"),
        player("Same Name", team="NYG", gsis="00-7"),
        defense("Rams", "LAR", "Los Angeles"),
        defense("Chargers", "LAC", "Los Angeles"),
    ]
)


def entry(name, slot="WR", position="WR", team="PHI", status="", key=None, keeper=False):
    return YahooPlayer(
        is_keeper=keeper,
        player_key=key or f"p.{name}",
        name=name,
        position=position,
        eligible=(position,),
        team=team,
        status=status,
        status_full="",
        injury_note="",
        selected_position=slot,
        is_editable=True,
        bye_week=None,
    )


# --- status ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [("O", "OUT"), ("IR-R", "IR"), ("PUP-R", "PUP"), ("NA", "NA"), ("Q", "QUESTIONABLE"), ("", ""), ("NEW", "NEW")],
)
def test_yahoo_codes_map_to_the_lineup_vocabulary(code, expected):
    assert yahoo_state.lineup_status(code) == expected


@pytest.mark.parametrize(
    "first,second,worse",
    [("OUT", "", "OUT"), ("", "OUT", "OUT"), ("QUESTIONABLE", "OUT", "OUT"), ("", "", ""), ("", "QUESTIONABLE", "QUESTIONABLE")],
)
def test_the_more_severe_designation_wins(first, second, worse):
    assert yahoo_state.more_severe(first, second) == worse


def test_a_sleeper_out_survives_a_yahoo_blank():
    """Yahoo can clear a status before kickoff while another source still has him out."""
    resolved, _ = yahoo_state.resolve_player(entry("Ben Bench"), INDEX)
    assert resolved is not None and resolved.injury_status == "OUT"


def test_a_yahoo_out_survives_a_sleeper_blank():
    resolved, _ = yahoo_state.resolve_player(entry("Alan Starter", status="O"), INDEX)
    assert resolved is not None and resolved.injury_status == "OUT"


# --- resolution ---------------------------------------------------------------------------


def test_a_defense_resolves_by_nickname_even_when_abbreviations_differ():
    """Yahoo's LAR becomes nflverse's LA, and Sleeper keeps LAR."""
    resolved, _ = yahoo_state.resolve_player(entry("Rams", slot="DEF", position="DEF", team="LA"), INDEX)
    assert resolved is not None and resolved.team == "LAR"


def test_a_defense_never_resolves_to_a_player():
    resolved, reason = yahoo_state.resolve_player(entry("Nobody", slot="DEF", position="DEF", team="ZZZ"), INDEX)
    assert resolved is None and reason


def test_a_namesake_is_separated_by_team():
    resolved, _ = yahoo_state.resolve_player(entry("Same Name", team="NYG"), INDEX)
    assert resolved is not None and resolved.gsis_id == "00-7"


# --- league state --------------------------------------------------------------------------


class StubClient:
    def get(self, path):  # pragma: no cover - readers are patched
        raise AssertionError(path)


@pytest.fixture
def state(monkeypatch):
    mine = [
        entry("Alan Starter", slot="WR", keeper=True),
        entry("Ben Bench", slot="IR"),
        entry("Rams", slot="DEF", position="DEF", team="LA"),
        entry("Ghost Player", slot="BN"),
    ]
    rival = [entry("Cal Rival", slot="RB", position="RB", team="DAL"), entry("Mystery Man", slot="BN")]
    monkeypatch.setattr(yahoo, "my_leagues", lambda c: [yahoo.YahooLeague("470.l.1", "L", 3)])
    monkeypatch.setattr(
        yahoo,
        "league_teams",
        lambda c, k: [YahooTeam("T1", "Mine", 88, 4, True), YahooTeam("T2", "Them", 40, 1, False), YahooTeam("T3", "Other", None, 2, False)],
    )
    monkeypatch.setattr(yahoo, "league_rosters", lambda c, k, w: {"T1": mine, "T2": rival, "T3": [entry("Other Guy", slot="QB", position="QB")]})
    return yahoo_state.load_state(StubClient(), League(), INDEX, 5)


def test_a_rival_without_a_roster_is_an_error_not_an_open_pool(monkeypatch):
    """His players would otherwise all look claimable."""
    monkeypatch.setattr(yahoo, "my_leagues", lambda c: [yahoo.YahooLeague("470.l.1", "L", 2)])
    monkeypatch.setattr(
        yahoo, "league_teams",
        lambda c, k: [YahooTeam("T1", "Mine", 88, 4, True), YahooTeam("T2", "Them", 40, 1, False)],
    )
    monkeypatch.setattr(yahoo, "league_rosters", lambda c, k, w: {"T1": [entry("Alan Starter")]})
    with pytest.raises(yahoo.YahooError, match="no roster for 1 team"):
        yahoo_state.load_state(StubClient(), League(), INDEX, 5)


def test_a_rival_with_an_empty_roster_is_an_error_too(monkeypatch):
    monkeypatch.setattr(yahoo, "my_leagues", lambda c: [yahoo.YahooLeague("470.l.1", "L", 2)])
    monkeypatch.setattr(
        yahoo, "league_teams",
        lambda c, k: [YahooTeam("T1", "Mine", 88, 4, True), YahooTeam("T2", "Them", 40, 1, False)],
    )
    monkeypatch.setattr(yahoo, "league_rosters", lambda c, k, w: {"T1": [entry("Alan Starter")], "T2": []})
    with pytest.raises(yahoo.YahooError, match="no roster for 1 team"):
        yahoo_state.load_state(StubClient(), League(), INDEX, 5)


def test_own_roster_and_budgets_come_from_yahoo(state):
    assert {p.name for p in state.roster} == {"Alan Starter", "Ben Bench", "Rams"}
    assert state.own_budget == 88
    assert state.rival_budgets == [40]


def test_an_unresolved_own_player_is_reported(state):
    assert [e.name for e, _ in state.unresolved] == ["Ghost Player"]


def test_rostered_players_league_wide_are_not_claimable(state):
    assert state.is_rostered(player("Cal Rival", position="RB", team="DAL", gsis="00-3"))
    assert not state.is_rostered(player("Dee Free", position="TE", team="MIA", gsis="00-4"))


def test_an_unresolved_rival_player_still_blocks_his_name(state):
    """A rival's player who failed to match must not be offered as a claim."""
    assert state.is_rostered(player("Mystery Man", gsis="00-99"))


def test_keepers_and_ir_slots_are_tracked(state):
    starter = next(p for p in state.roster if p.name == "Alan Starter")
    bench = next(p for p in state.roster if p.name == "Ben Bench")
    assert state.keepers == {starter.marker}
    assert state.reserve == {bench.marker}
    assert bench not in state.active_roster and starter in state.active_roster


def test_lineup_changes_name_who_to_start_and_sit(state):
    bench = next(p for p in state.roster if p.name == "Ben Bench")
    rams = next(p for p in state.roster if p.name == "Rams")
    start, sit = yahoo_state.lineup_changes(state, [bench, rams])
    assert start == ["Ben Bench"]
    assert sit == ["Alan Starter"]


def test_a_matching_lineup_needs_no_change(state):
    starters = [p for p in state.roster if p.name in ("Alan Starter", "Rams")]
    assert yahoo_state.lineup_changes(state, starters) == ([], [])


def test_several_leagues_without_an_id_is_an_error(monkeypatch):
    monkeypatch.setattr(yahoo, "my_leagues", lambda c: [yahoo.YahooLeague("470.l.1", "A", 10), yahoo.YahooLeague("470.l.2", "B", 8)])
    with pytest.raises(yahoo.YahooError, match="yahoo_league_id"):
        yahoo_state.resolve_league_key(StubClient(), League())


# --- the waiver pool -------------------------------------------------------------------------


def _projection(name, position, team, gsis, points):
    return Projection(
        gsis_id=gsis, name=name, position=position, team=team, points=points, games_played=3,
        **{k: 0.0 for k in Projection.__dataclass_fields__ if k not in ("gsis_id", "name", "position", "team", "points", "games_played")},
    )


def test_the_pool_is_unrostered_players_at_claimable_positions_best_first(state):
    projections = {
        "00-3": _projection("Cal Rival", "RB", "DAL", "00-3", 20.0),
        "00-4": _projection("Dee Free", "TE", "MIA", "00-4", 8.0),
        "00-5": _projection("Eve Free", "QB", "MIA", "00-5", 18.0),
        "00-1": _projection("Alan Starter", "WR", "PHI", "00-1", 15.0),
    }
    league = League(claim_positions=("RB", "WR", "TE", "K"))
    pool = cli._yahoo_pool(projections, INDEX, state, league)
    assert [p.name for p in pool] == ["Dee Free"]
    assert [p.name for p in cli._yahoo_pool(projections, INDEX, state, League())] == ["Eve Free", "Dee Free"]


# --- waivers wiring --------------------------------------------------------------------------


@pytest.fixture
def waivers_env(tmp_path, monkeypatch):
    """`faab waivers` with every data source faked, so only the wiring is under test."""
    monkeypatch.setattr(cli, "_build_index", lambda data: INDEX)
    monkeypatch.setattr(cli.nflverse, "load_games", lambda data: [])
    monkeypatch.setattr(cli.nflverse, "completed_week", lambda games, season: 4)
    monkeypatch.setattr(cli.nflverse, "upcoming_week", lambda games, season: 5)
    monkeypatch.setattr(cli.nflverse, "load_weekly_player_stats", lambda season, data: [])
    monkeypatch.setattr(cli.nflverse, "load_snap_counts", lambda season, data: [])
    monkeypatch.setattr(cli.nflverse, "load_players", lambda data: [])
    monkeypatch.setattr(cli.nflverse, "teams_on_bye", lambda games, season, week: set())
    monkeypatch.setattr(cli, "build_usage", lambda *a, **k: {})
    monkeypatch.setattr(cli, "build_projections", lambda *a, **k: {})
    monkeypatch.setattr(cli, "fetch_trending_players", lambda *a, **k: [])
    return ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"), "waivers"]


def test_an_empty_yahoo_pool_never_falls_back_to_an_unfiltered_sheet(waivers_env, monkeypatch, capsys, state):
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (state, ""))
    monkeypatch.setattr(
        cli.waiver_model, "find_candidates", lambda *a, **k: pytest.fail("rival-blind sheet used")
    )
    assert cli.main(waivers_env) == 0
    assert "No unrostered player" in capsys.readouterr().out


def test_the_fallback_warning_leads_the_report(waivers_env, monkeypatch, capsys, tmp_path):
    note = "Yahoo unavailable, so pasted files were used: 503"
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, note))
    (tmp_path / "roster.txt").write_text("Alan Starter\n", encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "waivers"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith(note)


def test_the_fallback_warning_leads_the_swap_sheet(waivers_env, monkeypatch, capsys, tmp_path):
    note = "Yahoo unavailable, so pasted files were used: 503"
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, note))
    monkeypatch.setattr(cli, "_available_players", lambda data, index: [player("Dee Free", position="TE", gsis="00-4")])
    monkeypatch.setattr(cli, "_price_swaps", lambda *a, **k: [])
    (tmp_path / "roster.txt").write_text("Alan Starter\n", encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "waivers"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith(note)


def test_the_fallback_warning_leads_the_lineup(waivers_env, monkeypatch, capsys, tmp_path):
    note = "Yahoo unavailable, so pasted files were used: 503"
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, note))
    (tmp_path / "roster.txt").write_text("Alan Starter\n", encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "lineup"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith(note)


def test_a_yahoo_lineup_report_leads_with_the_moves_to_make(waivers_env, monkeypatch, capsys, tmp_path, state):
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (state, ""))
    (tmp_path / "league.toml").write_text("[slots]\nWR = 1\nDEF = 1\n", encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"), "lineup"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("Your Yahoo lineup already matches.")


def test_an_open_bench_spot_makes_a_claim_free(state):
    """IR-slotted players take no bench space, so they must not make the roster look full.

    Two starters and one bench spot hold three players. The active roster has two, so a
    claim costs nobody, though the whole roster including IR would look full.
    """
    league = League(slots={"WR": 1, "DEF": 1}, bench=1)
    pool = [player("Same Name", team="NYJ", gsis="00-6")]
    projections = {
        "00-6": _projection("Same Name", "WR", "NYJ", "00-6", 12.0),
        "00-1": _projection("Alan Starter", "WR", "PHI", "00-1", 10.0),
    }
    assert len(state.active_roster) == league.starters
    swaps = cli._price_swaps(pool, state.active_roster, {}, projections, set(), 5, 4, league, {})
    assert swaps[0].drop is None and swaps[0].gain > 0
    # The fixture also carries an unmatched bench player, who would fill the spot.
    assert state.unmatched_active == 1


def test_an_unmatched_active_player_still_holds_a_roster_spot(state):
    """The fixture's own roster has one unresolved bench player, Ghost Player."""
    assert state.unmatched_active == 1
    league = League(slots={"WR": 1, "DEF": 1}, bench=1)
    pool = [player("Same Name", team="NYJ", gsis="00-6")]
    projections = {
        "00-6": _projection("Same Name", "WR", "NYJ", "00-6", 12.0),
        "00-1": _projection("Alan Starter", "WR", "PHI", "00-1", 10.0),
    }
    swaps = cli._price_swaps(
        pool, state.active_roster, {}, projections, set(), 5, 4, league, {},
        unmatched=state.unmatched_active,
    )
    assert swaps[0].drop is not None


def test_a_configured_keeper_is_protected_without_yahoo():
    roster = [player("Alan Starter", gsis="00-1"), player("Same Name", team="NYJ", gsis="00-6")]
    league = League(keepers=("alan starter",))
    assert cli._configured_keepers(roster, league) == frozenset({"00-1"})
    assert cli._configured_keepers(roster, League()) == frozenset()


def test_the_paste_fallback_protects_configured_keepers(waivers_env, monkeypatch, tmp_path):
    """A Yahoo outage must not reopen the keeper as a drop."""
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, "down"))
    monkeypatch.setattr(cli, "_available_players", lambda data, index: [player("Dee Free", position="TE", gsis="00-4")])
    seen = []

    def record(pool, roster, usage, projections, byes, week, through, league, market, protected, unmatched):
        seen.append(protected)
        return []

    monkeypatch.setattr(cli, "_price_swaps", record)
    (tmp_path / "roster.txt").write_text("Alan Starter\nSame Name WR NYJ\n", encoding="utf-8")
    (tmp_path / "league.toml").write_text('keepers = ["Alan Starter"]\n', encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "waivers"]
    assert cli.main(argv) == 0
    assert seen == [frozenset({"00-1"})]


def test_a_keeper_name_that_matches_nobody_is_named_in_the_banner(waivers_env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, ""))
    monkeypatch.setattr(cli, "_available_players", lambda data, index: [player("Dee Free", position="TE", gsis="00-4")])
    monkeypatch.setattr(cli, "_price_swaps", lambda *a, **k: [])
    (tmp_path / "roster.txt").write_text("Alan Starter\n", encoding="utf-8")
    (tmp_path / "league.toml").write_text('keepers = ["Alan Startr"]\n', encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "waivers"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith("Keeper Alan Startr is not on the roster, so no drop")


def test_a_pasted_roster_with_no_keepers_set_says_so(waivers_env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, ""))
    monkeypatch.setattr(cli, "_available_players", lambda data, index: [player("Dee Free", position="TE", gsis="00-4")])
    monkeypatch.setattr(cli, "_price_swaps", lambda *a, **k: [])
    (tmp_path / "roster.txt").write_text("Alan Starter\n", encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "waivers"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith("No keepers set in league.local.toml, so no drop")


def test_matched_keepers_add_no_warning():
    roster = [player("Alan Starter", gsis="00-1")]
    assert cli._keeper_warnings(roster, League(keepers=("Alan Starter",)), from_yahoo=False) == []
    assert cli._keeper_warnings(roster, League(), from_yahoo=True) == []


@pytest.mark.parametrize("keepers", ["", 'keepers = ["Alan Startr"]\n'])
def test_unconfirmed_keeper_protection_names_no_drop(waivers_env, monkeypatch, tmp_path, keepers):
    """A keeper that cannot be identified must not be offered, so nobody is."""
    monkeypatch.setattr(cli, "_league_state", lambda args, league, index, week: (None, "down"))
    monkeypatch.setattr(cli, "_available_players", lambda data, index: [player("Dee Free", position="TE", gsis="00-4")])
    seen = []

    def record(pool, roster, usage, projections, byes, week, through, league, market, protected, unmatched):
        seen.append((protected, {p.marker for p in roster}))
        return []

    monkeypatch.setattr(cli, "_price_swaps", record)
    (tmp_path / "roster.txt").write_text("Alan Starter\nSame Name WR NYJ\n", encoding="utf-8")
    (tmp_path / "league.toml").write_text(keepers, encoding="utf-8")
    argv = ["--data", str(tmp_path), "--league", str(tmp_path / "league.toml"),
            "--roster", str(tmp_path / "roster.txt"), "waivers"]
    assert cli.main(argv) == 0
    protected, roster = seen[0]
    assert roster and protected == frozenset(roster)
