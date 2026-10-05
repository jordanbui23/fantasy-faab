"""Tests for league configuration and slot rules."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.league import ConfigError, League, load_league  # noqa: E402


def test_defaults_apply_when_the_file_is_absent(tmp_path):
    league = load_league(tmp_path / "nope.toml")
    assert league.teams == 12
    assert league.slots["RB"] == 2
    assert league.starters == 9


def test_a_valid_file_is_read(tmp_path):
    path = tmp_path / "league.toml"
    path.write_text(
        'teams = 10\nfaab_budget = 250\nseason = 2026\n[slots]\nQB = 1\nRB = 3\n',
        encoding="utf-8",
    )
    league = load_league(path)
    assert league.teams == 10
    assert league.faab_budget == 250
    assert league.slots == {"QB": 1, "RB": 3}
    assert league.starters == 4


def test_slot_names_are_upper_cased(tmp_path):
    path = tmp_path / "league.toml"
    path.write_text("[slots]\nqb = 1\nflex = 1\n", encoding="utf-8")
    assert set(load_league(path).slots) == {"QB", "FLEX"}


def test_a_zero_count_slot_is_dropped(tmp_path):
    path = tmp_path / "league.toml"
    path.write_text("[slots]\nQB = 1\nK = 0\n", encoding="utf-8")
    assert load_league(path).slots == {"QB": 1}


def test_broken_toml_is_reported(tmp_path):
    path = tmp_path / "league.toml"
    path.write_text("teams = = 3", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_league(path)


@pytest.mark.parametrize(
    "body",
    [
        "[slots]\nQB = -1\n",
        "[slots]\nQB = true\n",
        '[slots]\nQB = "one"\n',
    ],
)
def test_a_bad_slot_count_is_rejected(tmp_path, body):
    path = tmp_path / "league.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="non-negative integer"):
        load_league(path)


def test_every_slot_zero_is_rejected(tmp_path):
    path = tmp_path / "league.toml"
    path.write_text("[slots]\nQB = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="every slot count is zero"):
        load_league(path)


def test_an_empty_slots_table_is_rejected(tmp_path):
    path = tmp_path / "league.toml"
    path.write_text("teams = 12\n[slots]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty table"):
        load_league(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("teams", 1), ("teams", True), ("faab_budget", -5), ("season", 1990)],
)
def test_a_bad_scalar_is_rejected(tmp_path, field, value):
    path = tmp_path / "league.toml"
    literal = "true" if value is True else str(value)
    path.write_text(f"{field} = {literal}\n[slots]\nQB = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=field):
        load_league(path)


# --- slot rules ----------------------------------------------------------------


def test_a_single_position_slot_accepts_only_that_position():
    league = League(slots={"RB": 1})
    assert league.accepts("RB", "RB")
    assert not league.accepts("RB", "WR")


def test_flex_accepts_the_skill_positions_and_not_a_quarterback():
    league = League(slots={"FLEX": 1})
    for position in ("RB", "WR", "TE"):
        assert league.accepts("FLEX", position)
    assert not league.accepts("FLEX", "QB")
    assert not league.accepts("FLEX", "K")


def test_superflex_accepts_a_quarterback():
    assert League(slots={"SUPERFLEX": 1}).accepts("SUPERFLEX", "QB")


def test_slot_order_puts_single_position_slots_first():
    league = League(slots={"FLEX": 1, "RB": 2, "QB": 1})
    order = league.slot_order()
    assert order.index("FLEX") == len(order) - 1
    assert order.count("RB") == 2


def test_slot_order_expands_every_count():
    league = League(slots={"WR": 3, "FLEX": 2})
    order = league.slot_order()
    assert order.count("WR") == 3
    assert order.count("FLEX") == 2
    assert len(order) == 5 == league.starters


# --- the local override, which keeps a team name out of the committed file -----


def test_the_local_override_supplies_a_value_the_committed_file_lacks(tmp_path):
    (tmp_path / "league.toml").write_text("teams = 10\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text('own_team = "Mine"\n', encoding="utf-8")
    league = load_league(tmp_path / "league.toml")
    assert league.own_team == "Mine"
    assert league.teams == 10


def test_the_local_override_wins_over_the_committed_file(tmp_path):
    (tmp_path / "league.toml").write_text('own_team = "Placeholder"\n', encoding="utf-8")
    (tmp_path / "league.local.toml").write_text('own_team = "Mine"\n', encoding="utf-8")
    assert load_league(tmp_path / "league.toml").own_team == "Mine"


def test_without_an_override_the_committed_file_stands(tmp_path):
    (tmp_path / "league.toml").write_text('own_team = "Committed"\n', encoding="utf-8")
    assert load_league(tmp_path / "league.toml").own_team == "Committed"


def test_overriding_one_scoring_weight_keeps_the_others(tmp_path):
    """A table merges key by key, so one override does not reset its siblings."""
    (tmp_path / "league.toml").write_text(
        "[scoring]\nreception = 0.5\npass_td = 6.0\n", encoding="utf-8"
    )
    (tmp_path / "league.local.toml").write_text(
        "[scoring]\nreception = 1.0\n", encoding="utf-8"
    )
    scoring = load_league(tmp_path / "league.toml").scoring
    assert scoring.reception == 1.0
    assert scoring.pass_td == 6.0


def test_the_override_applies_even_without_a_committed_file(tmp_path):
    (tmp_path / "league.local.toml").write_text('own_team = "Mine"\n', encoding="utf-8")
    assert load_league(tmp_path / "league.toml").own_team == "Mine"


def test_a_broken_override_is_reported_by_its_own_name(tmp_path):
    (tmp_path / "league.toml").write_text("teams = 10\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text("own_team = \n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    assert "league.local.toml" in str(caught.value)


def test_a_bad_slot_in_the_override_names_the_override(tmp_path):
    (tmp_path / "league.toml").write_text("[slots]\nQB = 1\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text("[slots]\nRB = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    assert "league.local.toml" in str(caught.value)


def test_a_bad_value_in_the_committed_file_names_the_committed_file(tmp_path):
    (tmp_path / "league.toml").write_text("teams = 1\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text('own_team = "Mine"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    assert "league.local.toml" not in str(caught.value)
    assert "league.toml" in str(caught.value)


def test_a_bad_scoring_weight_in_the_override_names_the_override(tmp_path):
    (tmp_path / "league.toml").write_text("[scoring]\nreception = 1.0\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text('[scoring]\npass_td = "six"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    assert "league.local.toml" in str(caught.value)


def test_a_bad_scoring_weight_in_the_committed_file_names_the_committed_file(tmp_path):
    (tmp_path / "league.toml").write_text('[scoring]\npass_td = "six"\n', encoding="utf-8")
    (tmp_path / "league.local.toml").write_text('own_team = "Mine"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    assert "league.local.toml" not in str(caught.value)
    assert "league.toml" in str(caught.value)


def test_an_unknown_scoring_key_in_the_override_names_the_override(tmp_path):
    (tmp_path / "league.local.toml").write_text("[scoring]\nbonus = 3\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    assert "league.local.toml" in str(caught.value)
    assert "bonus" in str(caught.value)


def test_unknown_scoring_keys_in_both_files_are_each_named_with_their_file(tmp_path):
    (tmp_path / "league.toml").write_text("[scoring]\nalpha = 1\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text("[scoring]\nbeta = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_league(tmp_path / "league.toml")
    message = str(caught.value)
    committed, local = sorted(message.split("; "), key=lambda part: "league.local.toml" in part)
    assert "alpha" in committed and "beta" not in committed
    assert "league.local.toml" not in committed
    assert "league.local.toml" in local and "beta" in local and "alpha" not in local


# --- Yahoo league id and claim positions ------------------------------------------------


def test_the_yahoo_league_id_comes_from_the_local_override(tmp_path):
    (tmp_path / "league.toml").write_text("teams = 10\n", encoding="utf-8")
    (tmp_path / "league.local.toml").write_text("yahoo_league_id = 12345\n", encoding="utf-8")
    assert load_league(tmp_path / "league.toml").yahoo_league_id == 12345


@pytest.mark.parametrize("value", ['"12345"', "-1", "true"])
def test_a_bad_yahoo_league_id_is_refused(tmp_path, value):
    (tmp_path / "league.toml").write_text(f"yahoo_league_id = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="yahoo_league_id"):
        load_league(tmp_path / "league.toml")


def test_claim_positions_default_to_every_scoring_position(tmp_path):
    assert load_league(tmp_path / "absent.toml").claim_positions == ("QB", "RB", "WR", "TE", "K")


def test_claim_positions_can_be_narrowed_locally(tmp_path):
    (tmp_path / "league.toml").write_text('claim_positions = ["QB", "RB"]\n', encoding="utf-8")
    (tmp_path / "league.local.toml").write_text('claim_positions = ["rb", "WR"]\n', encoding="utf-8")
    assert load_league(tmp_path / "league.toml").claim_positions == ("RB", "WR")


@pytest.mark.parametrize("value", ['[]', '"RB"', '["RB", "XX"]', "[1]"])
def test_bad_claim_positions_are_refused_naming_the_file(tmp_path, value):
    (tmp_path / "league.local.toml").write_text(f"claim_positions = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="league.local.toml.*claim_positions"):
        load_league(tmp_path / "league.toml")
