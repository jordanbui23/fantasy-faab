"""Tests for name normalization and resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import names  # noqa: E402
from faab.names import NameIndex, Player  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ja'Marr Chase", "jamarr chase"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("  Josh   Allen  ", "josh allen"),
        ("JOSH ALLEN", "josh allen"),
        ("Kenneth Walker III", "kenneth walker"),
        ("Odell Beckham Jr.", "odell beckham"),
        ("Michael Pittman Jr", "michael pittman"),
        ("José Álvarez", "jose alvarez"),
        ("D.J. Moore", "dj moore"),
    ],
)
def test_normalize_name(raw, expected):
    assert names.normalize_name(raw) == expected


def test_normalize_keeps_a_two_part_name_that_looks_like_a_suffix():
    """A suffix is only stripped when a real name remains."""
    assert names.normalize_name("Roman V") == "roman v"


@pytest.mark.parametrize("raw", ["", "   ", "123", "!!!", None, 42])
def test_normalize_returns_empty_for_input_without_letters(raw):
    assert names.normalize_name(raw) == ""


def test_empty_name_never_matches():
    index = NameIndex([Player(name="Josh Allen", position="QB", team="BUF")])
    player, reason = index.resolve("123")
    assert player is None
    assert "no letters" in reason


def test_unknown_name_reports_rather_than_guesses():
    index = NameIndex([Player(name="Josh Allen", position="QB", team="BUF")])
    player, reason = index.resolve("Nobody At All", position="QB", team="BUF")
    assert player is None
    assert reason == "no player with that name"


def test_a_hint_cannot_create_a_match():
    """A correct position must not rescue a name that does not exist."""
    index = NameIndex([Player(name="Josh Allen", position="QB", team="BUF")])
    assert index.resolve("Jsoh Alen", position="QB")[0] is None


def test_position_hint_disambiguates():
    index = NameIndex(
        [
            Player(name="Josh Allen", position="QB", team="BUF", status="ACT"),
            Player(name="Josh Allen", position="LB", team="JAX", status="ACT"),
        ]
    )
    player, reason = index.resolve("Josh Allen", position="QB")
    assert reason == ""
    assert player is not None and player.team == "BUF"


def test_team_hint_disambiguates_when_positions_match():
    index = NameIndex(
        [
            Player(name="Mike Williams", position="WR", team="NYJ", status="ACT"),
            Player(name="Mike Williams", position="WR", team="LAC", status="ACT"),
        ]
    )
    player, _ = index.resolve("Mike Williams", team="LAC")
    assert player is not None and player.team == "LAC"


def test_active_status_breaks_a_tie_without_any_hint():
    """The practice-squad namesake must not win over the starter."""
    index = NameIndex(
        [
            Player(name="Josh Allen", position="C", team="TB", status="DEV"),
            Player(name="Josh Allen", position="QB", team="BUF", status="ACT"),
        ]
    )
    player, reason = index.resolve("Josh Allen")
    assert reason == ""
    assert player is not None and player.position == "QB"


def test_two_active_namesakes_stay_ambiguous():
    """Two equally active players must be reported, never picked arbitrarily."""
    index = NameIndex(
        [
            Player(name="Josh Allen", position="QB", team="BUF", status="ACT"),
            Player(name="Josh Allen", position="LB", team="JAX", status="ACT"),
        ]
    )
    player, reason = index.resolve("Josh Allen")
    assert player is None
    assert "ambiguous" in reason
    assert "QB BUF" in reason and "LB JAX" in reason


def test_players_from_nflverse_keeps_rosterable_and_drops_the_rest():
    rows = [
        {"display_name": "A", "position": "RB", "latest_team": "SEA", "status": "ACT", "gsis_id": "1"},
        {"display_name": "B", "position": "WR", "latest_team": "GB", "status": "RES", "gsis_id": "2"},
        {"display_name": "C", "position": "TE", "latest_team": "GB", "status": "CUT", "gsis_id": "3"},
        {"display_name": "D", "position": "QB", "latest_team": "GB", "status": "RET", "gsis_id": "4"},
        {"display_name": "E", "position": "QB", "latest_team": "GB", "status": "ACT", "gsis_id": ""},
        {"display_name": "", "position": "QB", "latest_team": "GB", "status": "ACT", "gsis_id": "6"},
    ]
    built = names.players_from_nflverse(rows)
    assert [p.name for p in built] == ["A", "B"]
    assert all(p.gsis_id for p in built), "a player without a gsis id is useless here"


def test_team_defenses_get_every_spelling_as_an_alias():
    dump = {
        "SEA": {
            "position": "DEF",
            "team": "SEA",
            "first_name": "Seattle",
            "last_name": "Seahawks",
        }
    }
    defenses = names.team_defenses_from_sleeper(dump)
    assert len(defenses) == 1
    index = NameIndex(defenses)
    for spelling in ("Seahawks", "Seattle Seahawks", "Seattle", "SEA"):
        player, reason = index.resolve(spelling)
        assert player is not None, f"{spelling} did not resolve: {reason}"
        assert player.position == "DEF"


def test_team_defense_without_a_nickname_is_skipped():
    dump = {"XX": {"position": "DEF", "team": "XX", "first_name": "Nowhere"}}
    assert names.team_defenses_from_sleeper(dump) == []


def test_injury_status_is_attached_by_name():
    players = [Player(name="Puka Nacua", position="WR", team="LA", gsis_id="1")]
    dump = {
        "x": {
            "full_name": "Puka Nacua",
            "position": "WR",
            "injury_status": "Questionable",
        }
    }
    updated = names.attach_injury_status(players, dump)
    assert updated[0].injury_status == "QUESTIONABLE"
    assert updated[0].gsis_id == "1", "attaching a status must not lose the id"


def test_injury_status_absent_stays_empty_rather_than_healthy():
    players = [Player(name="Puka Nacua", position="WR", team="LA")]
    updated = names.attach_injury_status(players, {"x": {"full_name": "Someone Else"}})
    assert updated[0].injury_status == ""


def test_build_index_combines_players_and_defenses():
    rows = [
        {
            "display_name": "Jaxon Smith-Njigba",
            "position": "WR",
            "latest_team": "SEA",
            "status": "ACT",
            "gsis_id": "1",
        }
    ]
    dump = {
        "SEA": {
            "position": "DEF",
            "team": "SEA",
            "first_name": "Seattle",
            "last_name": "Seahawks",
        },
        "p": {
            "full_name": "Jaxon Smith-Njigba",
            "position": "WR",
            "injury_status": "Doubtful",
        },
    }
    index = names.build_index(rows, dump)
    player, _ = index.resolve("jaxon smith njigba")
    assert player is not None and player.injury_status == "DOUBTFUL"
    assert index.resolve("Seahawks")[0] is not None


# --- silent wrong resolution (review findings) ----------------------------------


def test_a_same_position_namesake_stays_ambiguous_even_when_one_is_active():
    """The reserve player the owner rosters must not resolve to an active stranger."""
    index = NameIndex(
        [
            Player(name="John Smith", position="WR", team="NYJ", status="PUP"),
            Player(name="John Smith", position="WR", team="LAC", status="ACT"),
        ]
    )
    player, reason = index.resolve("John Smith")
    assert player is None
    assert "ambiguous" in reason


def test_a_different_position_namesake_still_resolves_to_the_active_player():
    """Nobody typing Josh Allen means the practice-squad center."""
    index = NameIndex(
        [
            Player(name="Josh Allen", position="C", team="TB", status="DEV"),
            Player(name="Josh Allen", position="QB", team="BUF", status="ACT"),
        ]
    )
    player, reason = index.resolve("Josh Allen")
    assert reason == ""
    assert player is not None and player.position == "QB"


def test_a_namesake_cannot_overwrite_a_more_severe_injury_status():
    """Keying the status by name alone let a namesake turn an OUT into a QUESTIONABLE."""
    players = [
        Player(name="Josh Allen", position="QB", team="BUF", gsis_id="1"),
        Player(name="Josh Allen", position="LB", team="JAX", gsis_id="2"),
    ]
    dump = {
        "a": {"full_name": "Josh Allen", "position": "QB", "injury_status": "Out"},
        "b": {
            "full_name": "Josh Allen",
            "position": "LB",
            "injury_status": "Questionable",
        },
    }
    by_id = {p.gsis_id: p for p in names.attach_injury_status(players, dump)}
    assert by_id["1"].injury_status == "OUT"
    assert by_id["2"].injury_status == "QUESTIONABLE"


def test_the_more_severe_status_wins_when_name_and_position_both_collide():
    players = [Player(name="Alex Doe", position="RB", team="SEA", gsis_id="1")]
    dump = {
        "a": {"full_name": "Alex Doe", "position": "RB", "injury_status": "Questionable"},
        "b": {"full_name": "Alex Doe", "position": "RB", "injury_status": "Out"},
    }
    assert names.attach_injury_status(players, dump)[0].injury_status == "OUT"


def test_severity_order_does_not_depend_on_dump_order():
    players = [Player(name="Alex Doe", position="RB", team="SEA", gsis_id="1")]
    severe_first = {
        "a": {"full_name": "Alex Doe", "position": "RB", "injury_status": "Out"},
        "b": {"full_name": "Alex Doe", "position": "RB", "injury_status": "Doubtful"},
    }
    assert names.attach_injury_status(players, severe_first)[0].injury_status == "OUT"


def test_a_status_for_the_wrong_position_is_not_applied():
    players = [Player(name="Chris Jones", position="WR", team="SEA", gsis_id="1")]
    dump = {"a": {"full_name": "Chris Jones", "position": "DT", "injury_status": "Out"}}
    assert names.attach_injury_status(players, dump)[0].injury_status == ""


def test_a_status_without_a_position_is_applied_when_the_name_is_unique():
    """Three real Sleeper records carry a status and no position."""
    players = [Player(name="Solo Player", position="RB", team="SEA", gsis_id="1")]
    dump = {"a": {"full_name": "Solo Player", "injury_status": "Out"}}
    assert names.attach_injury_status(players, dump)[0].injury_status == "OUT"


def test_a_status_without_a_position_is_ignored_when_the_name_is_shared():
    """It cannot be told apart from the namesake's designation, so it is not guessed."""
    players = [Player(name="Josh Allen", position="QB", team="BUF", gsis_id="1")]
    dump = {
        "a": {"full_name": "Josh Allen", "injury_status": "Out"},
        "b": {"full_name": "Josh Allen", "position": "LB", "injury_status": "IR"},
    }
    assert names.attach_injury_status(players, dump)[0].injury_status == ""


def test_the_covid_designation_is_treated_as_a_reason_not_to_start():
    from faab.model.lineup import OUT_STATUSES

    assert "COV" in OUT_STATUSES


# --- round 2 review: uniqueness must count every record, not only status-bearing ones


def test_a_positionless_status_is_ignored_when_a_healthy_namesake_exists():
    """Counting only status-bearing records made this name look unique.

    The healthy namesake carries no status, so an earlier version did not count him, and
    the positionless OUT was attached to whichever player was asked for.
    """
    players = [Player(name="John Smith", position="RB", team="SEA", gsis_id="1")]
    dump = {
        "a": {"full_name": "John Smith", "injury_status": "Out"},
        "b": {"full_name": "John Smith", "position": "WR"},
    }
    assert names.attach_injury_status(players, dump)[0].injury_status == ""


def test_two_positionless_namesakes_are_both_ignored():
    """Two records collapsed into one entry when positions were what got counted."""
    players = [Player(name="John Smith", position="RB", team="SEA", gsis_id="1")]
    dump = {
        "a": {"full_name": "John Smith", "injury_status": "Out"},
        "b": {"full_name": "John Smith", "injury_status": "Questionable"},
    }
    assert names.attach_injury_status(players, dump)[0].injury_status == ""


def test_a_positionless_status_still_applies_for_a_genuinely_unique_name():
    players = [Player(name="Solo Player", position="RB", team="SEA", gsis_id="1")]
    dump = {
        "a": {"full_name": "Solo Player", "injury_status": "Out"},
        "b": {"full_name": "Someone Else", "position": "WR", "injury_status": "IR"},
    }
    assert names.attach_injury_status(players, dump)[0].injury_status == "OUT"


def test_a_positioned_status_is_unaffected_by_a_namesake():
    """The exact name and position match wins without consulting uniqueness at all."""
    players = [Player(name="John Smith", position="RB", team="SEA", gsis_id="1")]
    dump = {
        "a": {"full_name": "John Smith", "position": "RB", "injury_status": "Out"},
        "b": {"full_name": "John Smith", "position": "WR", "injury_status": "IR"},
    }
    assert names.attach_injury_status(players, dump)[0].injury_status == "OUT"


# --- round 2 review: an unknown position is not proof of a different position ---


def test_an_unknown_position_keeps_the_name_ambiguous():
    """Empty is unknown, so it cannot prove the other candidate plays elsewhere."""
    index = NameIndex(
        [
            Player(name="Chris Doe", position="", team="NYJ", status="RES"),
            Player(name="Chris Doe", position="WR", team="LAC", status="ACT"),
        ]
    )
    player, reason = index.resolve("Chris Doe")
    assert player is None
    assert "ambiguous" in reason


def test_an_active_candidate_without_a_position_does_not_win():
    index = NameIndex(
        [
            Player(name="Chris Doe", position="WR", team="NYJ", status="RES"),
            Player(name="Chris Doe", position="", team="LAC", status="ACT"),
        ]
    )
    assert index.resolve("Chris Doe")[0] is None


def test_a_known_and_different_position_still_resolves():
    index = NameIndex(
        [
            Player(name="Chris Doe", position="C", team="NYJ", status="DEV"),
            Player(name="Chris Doe", position="WR", team="LAC", status="ACT"),
        ]
    )
    player, reason = index.resolve("Chris Doe")
    assert reason == ""
    assert player is not None and player.position == "WR"
