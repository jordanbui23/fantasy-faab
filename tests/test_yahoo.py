"""Tests for the Yahoo collector, against synthetic responses in Yahoo's JSON shape.

No real league data is used. The builders below reproduce the structure Yahoo returns,
which is the part a parser gets wrong: collections keyed "0", "1" plus "count", and one
resource's fields spread across a list of single-key objects.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.collectors import yahoo, yahoo_auth  # noqa: E402
from faab.collectors.yahoo import YahooError  # noqa: E402


def collection(name, entries) -> dict[str, Any]:
    body: dict[str, Any] = {str(i): {name: entry} for i, entry in enumerate(entries)}
    body["count"] = len(entries)
    return body


def team_parts(key, name, balance=50, priority=3, own=False):
    meta: list[Any] = [
        {"team_key": key},
        {"team_id": key.rsplit(".", 1)[-1]},
        {"name": name},
        [],
        {"waiver_priority": priority},
        {"faab_balance": str(balance)},
    ]
    if own:
        meta.append({"is_owned_by_current_login": 1})
    return [meta]


def player_parts(
    key,
    name,
    position="WR",
    team="Phi",
    slot="WR",
    status=None,
    editable=None,
    eligible=None,
    bye=9,
    note=None,
):
    meta: list[Any] = [
        {"player_key": key},
        {"name": {"full": name, "first": name.split()[0], "last": name.split()[-1]}},
        {"editorial_team_abbr": team},
        {"bye_weeks": {"week": str(bye)}},
        {"display_position": position},
        {"primary_position": position},
        {"eligible_positions": [{"position": p} for p in (eligible or [position])]},
    ]
    if status:
        meta.append({"status": status})
        meta.append({"status_full": {"O": "Out", "Q": "Questionable", "NA": "Inactive"}.get(status, status)})
    if note:
        meta.append({"injury_note": note})
    extras: list[Any] = [{"selected_position": [{"coverage_type": "week", "week": "5"}, {"position": slot}]}]
    if editable is not None:
        extras.append({"is_editable": 1 if editable else 0})
    return [meta, *extras]


def roster_team(team, players):
    return [*team, {"roster": {"coverage_type": "week", "week": "5", "0": {"players": collection("player", players)}}}]


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload)[:100] if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, headers))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StubClient:
    """Answers `get` from a path prefix table, for the parsing tests."""

    def __init__(self, table):
        self.table = table

    def get(self, path):
        for prefix, content in self.table.items():
            if prefix in path:
                return content
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token.json"
    yahoo_auth.save_token(path, yahoo_auth.Token("access-1", "refresh-1", 9_999_999_999.0))
    return path


CREDS = yahoo_auth.Credentials("id", "secret", "https://localhost:8000")


# --- shape helpers --------------------------------------------------------------


def test_items_follows_count_order():
    body = collection("x", ["a", "b", "c"])
    assert list(yahoo.items(body, "x")) == ["a", "b", "c"]


def test_items_without_count_still_reads_numeric_keys_in_order():
    body = {"1": {"x": "b"}, "0": {"x": "a"}, "10": {"x": "k"}}
    assert list(yahoo.items(body, "x")) == ["a", "b", "k"]


def test_fields_merges_nested_lists():
    assert yahoo.fields([{"a": 1}, [{"b": 2}, [{"c": 3}]], "junk"]) == {"a": 1, "b": 2, "c": 3}


@pytest.mark.parametrize("yahoo_abbr,expected", [("Phi", "PHI"), ("LAR", "LA"), ("Jax", "JAX"), ("WSH", "WAS"), ("SF", "SF")])
def test_team_abbreviations_match_nflverse(yahoo_abbr, expected):
    assert yahoo.nfl_team(yahoo_abbr) == expected


# --- leagues ----------------------------------------------------------------------


def _leagues_content(*keys):
    leagues = collection("league", [[{"league_key": k, "name": f"League {i}", "num_teams": 10}] for i, k in enumerate(keys)])
    games = collection("game", [[{"game_key": "470"}, {"leagues": leagues}]])
    return {"users": collection("user", [[{"guid": "x"}, {"games": games}]])}


def test_my_leagues_reads_every_league():
    client = StubClient({"/users": _leagues_content("470.l.111", "470.l.222")})
    assert [lg.league_key for lg in yahoo.my_leagues(client)] == ["470.l.111", "470.l.222"]


def test_the_configured_league_is_picked_by_id():
    leagues = yahoo.my_leagues(StubClient({"/users": _leagues_content("470.l.111", "470.l.222")}))
    assert yahoo.pick_league(leagues, 222).league_key == "470.l.222"


def test_a_single_league_needs_no_configuration():
    leagues = yahoo.my_leagues(StubClient({"/users": _leagues_content("470.l.111")}))
    assert yahoo.pick_league(leagues, 0).league_key == "470.l.111"


def test_several_leagues_without_an_id_refuse_to_guess():
    leagues = yahoo.my_leagues(StubClient({"/users": _leagues_content("470.l.111", "470.l.222")}))
    with pytest.raises(YahooError, match="yahoo_league_id"):
        yahoo.pick_league(leagues, 0)


def test_an_unknown_league_id_is_refused():
    leagues = yahoo.my_leagues(StubClient({"/users": _leagues_content("470.l.111")}))
    with pytest.raises(YahooError, match="999"):
        yahoo.pick_league(leagues, 999)


def test_game_key_is_read():
    client = StubClient({"/game/nfl": {"game": [{"game_key": "470", "season": "2026"}]}})
    assert yahoo.game_key(client) == "470"


# --- teams ----------------------------------------------------------------------


def _teams_content():
    teams = collection(
        "team",
        [
            team_parts("470.l.1.t.1", "Alpha", balance=97, priority=1),
            team_parts("470.l.1.t.2", "Mine", balance=100, priority=6, own=True),
            team_parts("470.l.1.t.3", "Gamma", balance=37, priority=2),
        ],
    )
    return {"league": [{"league_key": "470.l.1"}, {"teams": teams}]}


def test_teams_carry_balance_priority_and_ownership():
    teams = yahoo.league_teams(StubClient({"/teams": _teams_content()}), "470.l.1")
    assert [(t.faab_balance, t.waiver_priority, t.is_own) for t in teams] == [
        (97, 1, False),
        (100, 6, True),
        (37, 2, False),
    ]
    assert yahoo.own_team(teams).name == "Mine"


def test_no_owned_team_is_an_error_not_a_guess():
    teams = [t for t in yahoo.league_teams(StubClient({"/teams": _teams_content()}), "L") if not t.is_own]
    with pytest.raises(YahooError, match="found 0"):
        yahoo.own_team(teams)


def test_an_empty_teams_response_raises():
    content = {"league": [{"league_key": "L"}, {"teams": {"count": 0}}]}
    with pytest.raises(YahooError):
        yahoo.league_teams(StubClient({"/teams": content}), "L")


# --- rosters ----------------------------------------------------------------------


def _rosters_content():
    mine = roster_team(
        team_parts("470.l.1.t.2", "Mine", own=True),
        [
            player_parts("p.1", "Alan Starter", slot="WR", editable=True, eligible=["WR", "W/R/T"]),
            player_parts("p.2", "Ben Bench", slot="BN", status="Q", editable=False, note="Ankle"),
            player_parts("p.3", "Rams", position="DEF", team="LAR", slot="DEF", editable=True),
        ],
    )
    rival = roster_team(team_parts("470.l.1.t.1", "Alpha"), [player_parts("p.9", "Cal Rival", slot="RB", position="RB")])
    return {"league": [{"league_key": "470.l.1"}, {"teams": collection("team", [mine, rival])}]}


def test_rosters_are_keyed_by_team_and_carry_slots():
    rosters = yahoo.league_rosters(StubClient({"/teams/roster": _rosters_content()}), "470.l.1", 5)
    assert set(rosters) == {"470.l.1.t.2", "470.l.1.t.1"}
    mine = rosters["470.l.1.t.2"]
    assert [p.selected_position for p in mine] == ["WR", "BN", "DEF"]
    assert [p.is_starting for p in mine] == [True, False, True]


def test_is_editable_is_read_from_the_trailing_objects():
    """Yahoo puts it after selected_position, not with the player's own fields."""
    mine = yahoo.league_rosters(StubClient({"/teams/roster": _rosters_content()}), "L", 5)["470.l.1.t.2"]
    assert [p.is_editable for p in mine] == [True, False, True]


def test_a_rival_player_without_is_editable_is_unknown_not_locked():
    rival = yahoo.league_rosters(StubClient({"/teams/roster": _rosters_content()}), "L", 5)["470.l.1.t.1"]
    assert rival[0].is_editable is None


def test_status_note_eligibility_team_and_bye_are_read():
    mine = yahoo.league_rosters(StubClient({"/teams/roster": _rosters_content()}), "L", 5)["470.l.1.t.2"]
    assert mine[1].status == "Q" and mine[1].injury_note == "Ankle"
    assert mine[0].eligible == ("WR", "W/R/T")
    assert mine[0].team == "PHI" and mine[0].bye_week == 9
    assert mine[2].team == "LA"


def test_team_roster_reads_one_team():
    content = {"team": roster_team(team_parts("T", "Mine"), [player_parts("p.1", "Alan Starter")])}
    players = yahoo.team_roster(StubClient({"/team/T/roster": content}), "T", 5)
    assert [p.name for p in players] == ["Alan Starter"]


def test_a_roster_for_another_week_is_refused():
    """Last week's lineup has the right number of starters and the wrong players."""
    content = {"team": roster_team(team_parts("T", "Mine"), [player_parts("p.1", "Alan Starter")])}
    with pytest.raises(YahooError, match="week 6"):
        yahoo.team_roster(StubClient({"/team/T/roster": content}), "T", 6)


def test_an_empty_team_roster_raises_rather_than_reporting_all_clear():
    content = {"team": roster_team(team_parts("T", "Mine"), [])}
    with pytest.raises(YahooError, match="empty"):
        yahoo.team_roster(StubClient({"/team/T/roster": content}), "T", 5)


# --- transactions -----------------------------------------------------------------


def _transaction(meta, moves):
    players = collection(
        "player",
        [[[{"player_key": f"p.{i}"}, {"name": {"full": name}}], {"transaction_data": data}] for i, (name, data) in enumerate(moves)],
    )
    return [meta, {"players": players}]


def test_completed_bids_read_adds_in_either_shape():
    add_list = [{"type": "add", "destination_team_key": "T1"}]
    drop_dict = {"type": "drop", "source_team_key": "T1"}
    add_dict = {"type": "add", "destination_team_key": "T2"}
    transactions = collection(
        "transaction",
        [
            _transaction({"type": "add/drop", "status": "successful", "faab_bid": "12", "timestamp": "100"}, [("Dee One", add_list), ("Old Guy", drop_dict)]),
            _transaction({"type": "add", "status": "successful", "faab_bid": "0", "timestamp": "200"}, [("Eve Two", add_dict)]),
            _transaction({"type": "drop", "status": "successful", "timestamp": "300"}, [("Fay Three", drop_dict)]),
            _transaction({"type": "add", "status": "failed", "faab_bid": "40", "timestamp": "400"}, [("Gus Four", add_dict)]),
        ],
    )
    content = {"league": [{"league_key": "L"}, {"transactions": transactions}]}
    bids = yahoo.completed_bids(StubClient({"/transactions": content}), "L")
    assert [(b.team_key, b.player_name, b.amount) for b in bids] == [("T1", "Dee One", 12), ("T2", "Eve Two", 0)]


# --- the client ---------------------------------------------------------------------


def _ok(content):
    return FakeResponse(200, {"fantasy_content": content})


def test_the_client_sends_the_bearer_token_and_asks_for_json(token_file):
    session = FakeSession([_ok({"game": []})])
    yahoo.Client(CREDS, token_file, session=session, sleep=lambda s: None).get("/game/nfl")
    url, headers = session.calls[0]
    assert url == yahoo.API_BASE + "/game/nfl"
    assert headers["Authorization"] == "Bearer access-1"


def test_a_401_refreshes_once_and_retries(token_file, monkeypatch):
    refreshed = []

    def fake_refresh(credentials, token):
        refreshed.append(token.refresh_token)
        return yahoo_auth.Token("access-2", "refresh-2", 9_999_999_999.0)

    monkeypatch.setattr(yahoo_auth, "refresh", fake_refresh)
    session = FakeSession([FakeResponse(401, text="expired"), _ok({"ok": 1})])
    content = yahoo.Client(CREDS, token_file, session=session, sleep=lambda s: None).get("/x")
    assert content == {"ok": 1}
    assert refreshed == ["refresh-1"]
    assert session.calls[1][1]["Authorization"] == "Bearer access-2"
    saved = yahoo_auth.load_token(token_file)
    assert saved is not None and saved.refresh_token == "refresh-2"


def test_a_second_401_is_an_error_not_a_loop(token_file, monkeypatch):
    monkeypatch.setattr(yahoo_auth, "refresh", lambda c, t: yahoo_auth.Token("a", "r", 9e9))
    session = FakeSession([FakeResponse(401, text="no"), FakeResponse(401, text="still no")])
    with pytest.raises(YahooError, match="401"):
        yahoo.Client(CREDS, token_file, session=session, sleep=lambda s: None).get("/x")


def test_a_server_error_is_retried_once(token_file):
    pauses = []
    session = FakeSession([FakeResponse(503, text="busy"), _ok({"ok": 1})])
    assert yahoo.Client(CREDS, token_file, session=session, sleep=pauses.append).get("/x") == {"ok": 1}
    assert pauses == [2]


def test_a_connection_error_is_retried_once_then_raised(token_file):
    error = requests.ConnectionError("down")
    session = FakeSession([error, error])
    with pytest.raises(YahooError, match="could not reach"):
        yahoo.Client(CREDS, token_file, session=session, sleep=lambda s: None).get("/x")
    assert len(session.calls) == 2


def test_a_403_raises_with_yahoos_message(token_file):
    session = FakeSession([FakeResponse(403, text="This application is not authorized")])
    with pytest.raises(YahooError, match="403.*not authorized"):
        yahoo.Client(CREDS, token_file, session=session, sleep=lambda s: None).get("/x")


@pytest.mark.parametrize("response", [FakeResponse(200, None, text="<html>"), FakeResponse(200, {"error": "x"})])
def test_an_unreadable_body_raises(token_file, response):
    session = FakeSession([response])
    with pytest.raises(YahooError):
        yahoo.Client(CREDS, token_file, session=session, sleep=lambda s: None).get("/x")
