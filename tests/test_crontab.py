"""Tests for crontab block editing.

This module edits state outside the repo, so a mistake destroys cron jobs this project
did not create. Most of these tests are about what must SURVIVE an edit, not about what
the edit produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab import crontab  # noqa: E402
from faab.crontab import CrontabError  # noqa: E402

RUNNER = "/home/me/projects/fantasy-faab/bin/faab-cron"
LOG_DIR = "/home/me/projects/fantasy-faab/data/logs"

OTHER_JOBS = "\n".join(
    [
        "*/2 * * * * /home/me/bin/watchdog.sh >> /tmp/w.log 2>&1",
        "0 */2 * * * /home/me/bin/backup.sh",
        "*/1 * * * * /home/me/.local/bin/sync-photos.sh",
    ]
)


def _install(current, topic="my-topic"):
    return crontab.install(current, topic, RUNNER, LOG_DIR)


# --- what must survive an edit --------------------------------------------------


def test_unrelated_jobs_survive_an_install():
    result = _install(OTHER_JOBS + "\n")
    for line in OTHER_JOBS.splitlines():
        assert line in result


def test_unrelated_jobs_survive_a_remove():
    installed = _install(OTHER_JOBS + "\n")
    result = crontab.remove(installed)
    for line in OTHER_JOBS.splitlines():
        assert line in result
    assert not crontab.contains_block(result)


def test_a_job_merely_mentioning_the_repo_path_is_not_deleted():
    """The old substring filter deleted any line naming this repo."""
    bystander = f"0 3 * * * tar -czf /backup/x.tgz {Path(RUNNER).parents[1]}"
    current = f"{bystander}\n"
    result = crontab.remove(_install(current))
    assert bystander in result


def test_a_job_mentioning_faab_cron_is_not_deleted():
    bystander = "0 4 * * * /home/me/bin/my-own-faab-cron-backup.sh"
    result = crontab.remove(_install(f"{bystander}\n"))
    assert bystander in result


def test_an_owners_own_topic_variable_is_not_deleted():
    """The old filter deleted this line even when it belonged to something else."""
    bystander = "FAAB_NTFY_TOPIC=something-the-owner-set-for-another-job"
    result = crontab.remove(_install(f"{bystander}\n{OTHER_JOBS}\n"))
    assert bystander in result


def test_removing_from_a_crontab_without_our_block_changes_nothing():
    current = OTHER_JOBS + "\n"
    assert crontab.remove(current).splitlines() == OTHER_JOBS.splitlines()


def test_remove_on_an_empty_crontab_yields_empty():
    assert crontab.remove("") == ""


# --- idempotence ---------------------------------------------------------------


def test_installing_twice_leaves_one_block():
    once = _install(OTHER_JOBS + "\n")
    twice = _install(once)
    assert twice.count(crontab.BEGIN_MARKER) == 1
    assert twice.count(crontab.END_MARKER) == 1
    assert twice == once


def test_installing_a_new_topic_replaces_the_old_one():
    once = _install(OTHER_JOBS + "\n", topic="first-topic")
    twice = _install(once, topic="second-topic")
    assert "FAAB_NTFY_TOPIC=second-topic" in twice
    assert "first-topic" not in twice


def test_install_then_remove_restores_the_original_lines():
    current = OTHER_JOBS + "\n"
    assert crontab.remove(_install(current)).splitlines() == current.splitlines()


# --- the credential must not leak to later jobs --------------------------------


def test_the_topic_is_cleared_after_our_entries():
    """A cron variable stays in force, so a job appended later would inherit it."""
    lines = crontab.build_block("my-topic", RUNNER, LOG_DIR)
    set_at = lines.index("FAAB_NTFY_TOPIC=my-topic")
    cleared_at = lines.index("FAAB_NTFY_TOPIC=")
    last_entry = max(i for i, line in enumerate(lines) if line.startswith("17 "))
    assert set_at < last_entry < cleared_at


def test_the_variable_precedes_both_entries():
    lines = crontab.build_block("my-topic", RUNNER, LOG_DIR)
    set_at = lines.index("FAAB_NTFY_TOPIC=my-topic")
    for index, line in enumerate(lines):
        if line.startswith("17 "):
            assert set_at < index


def test_both_schedules_are_installed():
    block = "\n".join(crontab.build_block("t", RUNNER, LOG_DIR))
    assert "--scheduled lineup" in block
    assert "--scheduled waivers" in block
    assert "--scheduled gameday" in block


def test_the_gameday_entry_runs_every_five_minutes():
    entries = [
        line for line in crontab.build_block("t", RUNNER, LOG_DIR) if "--scheduled gameday" in line
    ]
    assert len(entries) == 1
    assert entries[0].split()[:5] == ["*/5", "*", "*", "*", "*"]


# --- refuse rather than guess --------------------------------------------------


def test_a_begin_marker_without_an_end_is_refused():
    damaged = f"{OTHER_JOBS}\n{crontab.BEGIN_MARKER}\nFAAB_NTFY_TOPIC=x\n"
    with pytest.raises(CrontabError, match="unbalanced"):
        crontab.remove(damaged)


def test_an_end_marker_without_a_begin_is_refused():
    damaged = f"{OTHER_JOBS}\n{crontab.END_MARKER}\n"
    with pytest.raises(CrontabError, match="unbalanced"):
        crontab.remove(damaged)


def test_two_blocks_are_refused():
    doubled = _install(_install(OTHER_JOBS + "\n")) + "\n".join(
        crontab.build_block("other", RUNNER, LOG_DIR)
    )
    with pytest.raises(CrontabError, match="more than one"):
        crontab.remove(doubled)


def test_a_reversed_marker_pair_is_refused():
    reversed_pair = f"{crontab.END_MARKER}\nFAAB_NTFY_TOPIC=x\n{crontab.BEGIN_MARKER}\n"
    with pytest.raises(CrontabError, match="precedes"):
        crontab.remove(reversed_pair)


def test_a_refusal_never_returns_a_partial_crontab():
    """The caller pipes the result back, so a refusal must raise, never return text."""
    damaged = f"{OTHER_JOBS}\n{crontab.BEGIN_MARKER}\n"
    try:
        crontab.remove(damaged)
    except CrontabError:
        pass
    else:
        pytest.fail("a damaged crontab was edited instead of refused")


# --- topic validation ----------------------------------------------------------


@pytest.mark.parametrize("topic", ["abc", "a-b_c9", "Topic_1", "x", "9"])
def test_a_valid_topic_is_accepted(topic):
    assert crontab.validate_topic(topic) == topic


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "has space",
        "has/slash",
        "has\nnewline",
        "semi;colon",
        "$(id)",
        "back`tick`",
        "quote'd",
        'double"d',
        "tab\there",
        "hash#mark",
        "equals=sign",
        "per%cent",
    ],
)
def test_an_unsafe_topic_is_refused(topic):
    with pytest.raises(CrontabError):
        crontab.validate_topic(topic)


def test_a_topic_at_the_limit_is_accepted():
    assert crontab.validate_topic("a" * crontab.MAX_TOPIC_LENGTH)


def test_a_topic_over_the_limit_is_refused():
    with pytest.raises(CrontabError, match="over the"):
        crontab.validate_topic("a" * (crontab.MAX_TOPIC_LENGTH + 1))


def test_a_non_string_topic_is_refused():
    with pytest.raises(CrontabError):
        crontab.validate_topic(None)  # type: ignore[arg-type]


def test_building_a_block_validates_the_topic():
    """Validation must not be skippable by calling the builder directly."""
    with pytest.raises(CrontabError):
        crontab.build_block("bad topic", RUNNER, LOG_DIR)


def test_installing_validates_the_topic_before_touching_the_crontab():
    with pytest.raises(CrontabError):
        crontab.install(OTHER_JOBS + "\n", "bad topic", RUNNER, LOG_DIR)


# --- shape ---------------------------------------------------------------------


def test_the_result_ends_with_one_newline():
    result = _install(OTHER_JOBS + "\n")
    assert result.endswith("\n")
    assert not result.endswith("\n\n")


def test_blank_lines_are_not_accumulated():
    current = f"{OTHER_JOBS}\n\n\n"
    assert "\n\n" not in _install(current)


def test_a_crontab_with_no_trailing_newline_is_handled():
    result = _install(OTHER_JOBS)
    for line in OTHER_JOBS.splitlines():
        assert line in result


# --- legacy lines are reported, never deleted ----------------------------------


def test_legacy_lines_are_reported():
    legacy = f"{crontab.LEGACY_MARKER}\n17 * * * * {RUNNER} --scheduled lineup\n"
    found = crontab.find_legacy_lines(legacy, RUNNER)
    assert len(found) == 2


def test_legacy_lines_are_not_deleted_by_an_edit():
    """An unmarked line may be one the owner wrote, so it survives and is reported."""
    legacy_entry = f"17 * * * * {RUNNER} --scheduled lineup"
    result = _install(f"{legacy_entry}\n")
    assert legacy_entry in result


def test_no_legacy_lines_in_a_clean_crontab():
    assert crontab.find_legacy_lines(OTHER_JOBS + "\n", RUNNER) == []


# --- round 6 review -------------------------------------------------------------


@pytest.mark.parametrize("topic", ["abc\n", "abc\r", "abc\n\n", "\nabc", "abc\nmore"])
def test_a_topic_with_a_newline_is_refused(topic):
    """`match` with a `$` anchor accepted a trailing newline, which corrupts a crontab."""
    with pytest.raises(CrontabError):
        crontab.validate_topic(topic)


def test_no_block_line_ever_contains_a_newline():
    """Each entry must be exactly one crontab line."""
    for line in crontab.build_block("my-topic", RUNNER, LOG_DIR):
        assert "\n" not in line and "\r" not in line


def test_every_entry_has_five_cron_fields_then_the_runner():
    """Substring checks passed even with malformed schedule fields."""
    for line in crontab.build_block("t", RUNNER, LOG_DIR):
        if not line.startswith("17 "):
            continue
        fields = line.split()
        assert fields[:5] == ["17", "*", "*", "*", "*"], f"bad schedule in {line!r}"
        assert fields[5] == RUNNER, f"bad runner in {line!r}"
        assert fields[6] == "--scheduled"
        assert fields[7] in ("lineup", "waivers")
        assert line.endswith(f">> {LOG_DIR}/cron.log 2>&1")


def test_the_block_has_exactly_two_entries():
    entries = [
        line for line in crontab.build_block("t", RUNNER, LOG_DIR)
        if line.startswith("17 ")
    ]
    assert len(entries) == 2
    assert len({line.split()[7] for line in entries}) == 2


def test_the_block_is_exactly_six_lines():
    """Markers, the variable, two entries, and the clearing line."""
    assert len(crontab.build_block("t", RUNNER, LOG_DIR)) == 7


def test_install_output_parses_back_to_the_same_lines():
    """The written text must re-read as the lines it was built from."""
    result = crontab.install(OTHER_JOBS + "\n", "my-topic", RUNNER, LOG_DIR)
    assert result.splitlines()[-7:] == crontab.build_block("my-topic", RUNNER, LOG_DIR)
