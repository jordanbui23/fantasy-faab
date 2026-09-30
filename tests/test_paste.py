"""Tests for reading pasted Yahoo text, and for the availability filter it feeds.

The parser is deliberately not keyed to Yahoo's column layout, so these tests assert that
varied shapes all resolve rather than asserting one exact format.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.model import waiver  # noqa: E402
from faab.names import NameIndex, Player, build_index  # noqa: E402
from faab.paste import extract_budgets, extract_players  # noqa: E402


def _player(name, position="RB", team="SEA", gsis=""):
    return Player(
        gsis_id=gsis or name.lower().replace(" ", "-"),
        name=name,
        position=position,
        team=team,
        status="ACT",
    )


@pytest.fixture
def index() -> NameIndex:
    """A small index built the same way the real one is."""
    rows = [
        {"gsis_id": "1", "display_name": "Ollie Gordon II", "position": "RB",
         "team_abbr": "MIA", "status": "ACT"},
        {"gsis_id": "2", "display_name": "Amon-Ra St. Brown", "position": "WR",
         "team_abbr": "DET", "status": "ACT"},
        {"gsis_id": "3", "display_name": "Mike Washington Jr.", "position": "RB",
         "team_abbr": "LV", "status": "ACT"},
        {"gsis_id": "4", "display_name": "Mike Washington Jr.", "position": "DB",
         "team_abbr": "TB", "status": "ACT"},
        {"gsis_id": "5", "display_name": "Ted Hurst III", "position": "WR",
         "team_abbr": "TB", "status": "ACT"},
    ]
    sleeper = {
        "PIT": {"player_id": "PIT", "position": "DEF", "last_name": "Steelers",
                "team": "PIT", "active": True},
        "SF": {"player_id": "SF", "position": "DEF", "last_name": "49ers",
               "team": "SF", "active": True},
    }
    return build_index(rows, sleeper)


# --- names come out of varied shapes -------------------------------------------


def test_a_plain_name_resolves(index):
    players, problems = extract_players("Ollie Gordon II", index)
    assert [p.name for p in players] == ["Ollie Gordon II"]
    assert problems == []


def test_yahoo_wide_columns_resolve(index):
    text = "Ollie Gordon II RB - MIA  Q  84%  9.0  12"
    players, problems = extract_players(text, index)
    assert [p.name for p in players] == ["Ollie Gordon II"]
    assert problems == []


def test_a_hyphenated_name_with_a_period_resolves(index):
    players, _ = extract_players("Amon-Ra St. Brown WR DET 16.2", index)
    assert [p.name for p in players] == ["Amon-Ra St. Brown"]


def test_a_suffix_is_kept_and_the_team_is_not_eaten(index):
    """"Ted Hurst III TB" must keep III as part of the name and take TB as the team."""
    players, problems = extract_players("Ted Hurst III TB 5.74", index)
    assert [p.name for p in players] == ["Ted Hurst III"]
    assert problems == []


# --- hints settle an ambiguity that a name alone cannot ------------------------


def test_a_position_hint_resolves_a_same_name_pair(index):
    players, problems = extract_players("Mike Washington Jr. RB LV 4.34", index)
    assert [(p.name, p.position) for p in players] == [("Mike Washington Jr.", "RB")]
    assert problems == []


def test_without_a_hint_the_same_name_stays_ambiguous(index):
    players, problems = extract_players("Mike Washington Jr.", index)
    assert players == []
    assert len(problems) == 1
    assert "ambiguous" in problems[0].reason


def test_an_ambiguity_outranks_a_not_found_in_the_report(index):
    """The actionable reason must survive, not the last one tried."""
    _, problems = extract_players("Mike Washington Jr.", index)
    assert "ambiguous" in problems[0].reason


# --- team defenses, which are one word and carry no gsis id -------------------


def test_a_one_word_defense_resolves(index):
    players, problems = extract_players("Steelers DEF PIT 8.17", index)
    assert [p.name for p in players] == ["Steelers"]
    assert problems == []


def test_a_defense_whose_name_starts_with_a_digit_resolves(index):
    """49ers is a real team name and a leading digit must not reject it."""
    players, problems = extract_players("49ers DEF SF 5.67", index)
    assert [p.name for p in players] == ["49ers"]
    assert problems == []


def test_two_defenses_both_survive_dedup(index):
    """A defense has no gsis id, so keying dedup on it alone would collapse them."""
    players, _ = extract_players("Steelers DEF PIT 8.17\n49ers DEF SF 5.67", index)
    assert sorted(p.name for p in players) == ["49ers", "Steelers"]


# --- chrome and noise ----------------------------------------------------------


def test_a_widget_line_is_skipped_rather_than_reported(index):
    """Yahoo renders a widget label into the name's own line."""
    text = "Ollie Gordon II\nOllie Gordon IIVideo ForecastPlayer Note\nMIA - RB"
    players, problems = extract_players(text, index)
    assert [p.name for p in players] == ["Ollie Gordon II"]
    assert problems == [], f"chrome was reported as a failure: {problems}"


def test_a_comment_and_a_blank_line_are_skipped(index):
    players, problems = extract_players("# a note\n\nOllie Gordon II\n", index)
    assert len(players) == 1
    assert problems == []


def test_a_duplicate_is_kept_once(index):
    players, _ = extract_players("Ollie Gordon II\nOllie Gordon II RB MIA", index)
    assert len(players) == 1


def test_a_header_word_is_not_reported_as_a_missing_player(index):
    players, problems = extract_players("Offense\nRankings\nTrends", index)
    assert players == []
    assert problems == [], f"headers were reported: {problems}"


# --- budgets -------------------------------------------------------------------


def test_budgets_parse_with_and_without_a_dollar_sign():
    entries = extract_budgets("Team One $93\nTeam Two 52")
    assert [(e.team, e.budget) for e in entries] == [("Team One", 93), ("Team Two", 52)]


def test_a_multi_word_team_name_survives():
    entries = extract_budgets("The Longest Team Name In The League $98")
    assert entries[0].team == "The Longest Team Name In The League"


def test_a_zero_budget_is_kept_not_dropped():
    """A rival with nothing left is a real fact and changes contention."""
    entries = extract_budgets("Broke Team $0")
    assert entries[0].budget == 0


def test_a_line_without_money_is_skipped():
    assert extract_budgets("Standings\nRank Team W-L-T") == []


def test_an_implausible_amount_is_rejected():
    assert extract_budgets("Total Points 1200") == []


# --- rival contention ----------------------------------------------------------


def test_no_rival_budgets_returns_none_not_zero():
    """Zero pressure from missing data would understate every bid."""
    assert waiver.rival_contention([], 100) is None


def test_every_rival_able_to_match_is_full_contention():
    assert waiver.rival_contention([100, 98, 95], 100) == pytest.approx(1.0)


def test_no_rival_able_to_match_is_zero_contention():
    assert waiver.rival_contention([1, 2, 0], 100) == pytest.approx(0.0)


def test_contention_is_the_share_able_to_match():
    assert waiver.rival_contention([100, 100, 0, 0], 100) == pytest.approx(0.5)


def test_a_known_rich_league_bids_more_than_a_poor_one():
    rich = waiver.suggested_bid(0.9, 500, 100, rival_budgets=[100] * 9)
    poor = waiver.suggested_bid(0.9, 500, 100, rival_budgets=[0] * 9)
    assert rich > poor


def test_absent_rival_budgets_fall_back_to_the_add_count():
    """With no budgets the add count still moves the bid, so the signal is not dropped."""
    chased = waiver.suggested_bid(0.9, waiver.SATURATION_ADDS, 100, rival_budgets=None)
    ignored = waiver.suggested_bid(0.9, 1, 100, rival_budgets=None)
    assert chased > ignored


def test_known_poor_rivals_beat_the_add_count_fallback():
    """Budgets are preferred: nine broke rivals cannot outbid, whatever the public does."""
    with_budgets = waiver.suggested_bid(
        0.9, waiver.SATURATION_ADDS, 100, rival_budgets=[0] * 9
    )
    fallback = waiver.suggested_bid(0.9, waiver.SATURATION_ADDS, 100, rival_budgets=None)
    assert with_budgets < fallback
