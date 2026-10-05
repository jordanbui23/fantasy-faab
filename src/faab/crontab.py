"""Crontab block editing.

This module owns every byte this project writes into the crontab. It lives here rather
than in `bin/faab-cron` because it edits state outside the repo, and a mistake destroys
cron jobs this project did not create. Shell made that logic untestable, so five review
rounds verified it by hand instead. These are pure string functions with a test suite.

Two rules shape the whole module.

Match an exact marked block, never a substring. An earlier version filtered the crontab
with `grep -v fantasy-faab`, which deleted any unrelated job whose command merely
mentioned this repo's path, and `grep -v '^FAAB_NTFY_TOPIC='`, which deleted the same
variable if the owner set it for something else.

Refuse rather than guess. A crontab carrying a begin marker with no end marker is
damaged, and deleting to the end of the file would be a guess about how much belongs to
us. Unbalanced markers raise instead.
"""

from __future__ import annotations

import os
import re
import sys

BEGIN_MARKER = "# >>> fantasy-faab scheduled reports >>>"
END_MARKER = "# <<< fantasy-faab scheduled reports <<<"

TOPIC_VARIABLE = "FAAB_NTFY_TOPIC"

# Conservative cap. ntfy's own documented maximum was not verified from this box, so this
# is a deliberate limit rather than a quoted one. A topic this long is a typo anyway.
MAX_TOPIC_LENGTH = 64

# fullmatch, not match: `$` also matches just before a trailing newline, so
# `match` accepted "abc\n" and that newline would have corrupted the crontab.
_TOPIC_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

# The unmarked form an earlier version installed. Removed only on an EXACT line match,
# which is safe in a way the old substring filter was not.
LEGACY_MARKER = "# fantasy-faab scheduled reports"


class CrontabError(RuntimeError):
    """Raised when the crontab cannot be edited safely."""


def validate_topic(topic: str) -> str:
    """Return the topic, or raise when it cannot be written as a cron variable.

    A cron variable runs to the end of its line with no quoting and no expansion, so a
    value holding a space, a newline or a shell metacharacter would corrupt the file or
    silently become part of the value.
    """
    if not isinstance(topic, str) or not topic:
        raise CrontabError(f"{TOPIC_VARIABLE} is empty")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise CrontabError(
            f"{TOPIC_VARIABLE} is {len(topic)} characters, over the "
            f"{MAX_TOPIC_LENGTH} character limit"
        )
    if not _TOPIC_PATTERN.fullmatch(topic):
        raise CrontabError(
            f"{TOPIC_VARIABLE} may use only letters, digits, underscore and hyphen, "
            f"got {topic!r}"
        )
    return topic


def build_block(topic: str, runner: str, log_dir: str) -> list[str]:
    """The lines this project owns, between its markers.

    The topic is cleared on the line after the entries. A cron variable stays in force
    for every entry that follows it, so without that reset a job the owner appends later
    would inherit the credential.
    """
    validate_topic(topic)
    return [
        BEGIN_MARKER,
        f"{TOPIC_VARIABLE}={topic}",
        f"17 * * * * {runner} --scheduled lineup >> {log_dir}/cron.log 2>&1",
        f"17 * * * * {runner} --scheduled waivers >> {log_dir}/cron.log 2>&1",
        f"*/5 * * * * {runner} --scheduled gameday >> {log_dir}/gameday.log 2>&1",
        f"{TOPIC_VARIABLE}=",
        END_MARKER,
    ]


def strip_block(current: str) -> list[str]:
    """Every line except this project's block.

    Raises when the markers are unbalanced, because the safe amount to delete is then
    unknowable.
    """
    lines = current.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN_MARKER]
    ends = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]

    if len(begins) > 1 or len(ends) > 1:
        raise CrontabError(
            "the crontab holds more than one fantasy-faab block; remove the extra by "
            "hand with `crontab -e`"
        )
    if len(begins) != len(ends):
        raise CrontabError(
            "the crontab holds an unbalanced fantasy-faab marker; fix it by hand with "
            "`crontab -e` rather than letting this script guess what to delete"
        )

    if not begins:
        return list(lines)

    start, end = begins[0], ends[0]
    if end < start:
        raise CrontabError(
            "the fantasy-faab end marker precedes its begin marker; fix it by hand"
        )
    return lines[:start] + lines[end + 1 :]


def find_legacy_lines(current: str, runner: str) -> list[str]:
    """Lines an earlier unmarked version may have written, for the owner to review.

    These are reported, never deleted. An unmarked line cannot be told apart from a job
    the owner wrote by hand that happens to call the same script, and deleting somebody
    else's cron entry is worse than leaving a duplicate the owner can see.
    """
    found = []
    for line in strip_block(current):
        stripped = line.strip()
        if stripped == LEGACY_MARKER:
            found.append(stripped)
        elif runner and stripped.startswith("17 * * * *") and runner in stripped:
            found.append(stripped)
    return found


def install(current: str, topic: str, runner: str, log_dir: str) -> str:
    """The crontab with this project's block replaced, and every other line preserved."""
    kept = [line for line in strip_block(current) if line.strip()]
    return "\n".join(kept + build_block(topic, runner, log_dir)) + "\n"


def remove(current: str) -> str:
    """The crontab without this project's block."""
    kept = [line for line in strip_block(current) if line.strip()]
    return ("\n".join(kept) + "\n") if kept else ""


def contains_block(current: str) -> bool:
    return any(line.strip() == BEGIN_MARKER for line in current.splitlines())


def _main(argv: list[str]) -> int:
    """Read a crontab on stdin, write the edited one on stdout.

    The shell owns talking to the `crontab` command and nothing else. Errors go to
    stderr with a non-zero exit, so the caller never pipes a damaged crontab back.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="faab.crontab")
    parser.add_argument("action", choices=("install", "remove", "show", "legacy"))
    parser.add_argument("--runner", default="")
    parser.add_argument("--log-dir", default="")
    args = parser.parse_args(argv)

    # The topic comes from the environment, never from argv. A command line is readable
    # by any local process, and the topic is the credential. Taking it from the
    # environment also stops a leading-hyphen topic being parsed as an option.
    topic = os.environ.get(TOPIC_VARIABLE, "")

    current = sys.stdin.read() if args.action in ("install", "remove", "legacy") else ""

    try:
        if args.action == "install":
            sys.stdout.write(
                install(current, topic, args.runner, args.log_dir)
            )
        elif args.action == "remove":
            sys.stdout.write(remove(current))
        elif args.action == "show":
            validate_topic(topic)
            sys.stdout.write(
                "\n".join(build_block(topic, args.runner, args.log_dir)) + "\n"
            )
        else:
            for line in find_legacy_lines(current, args.runner):
                sys.stdout.write(line + "\n")
    except CrontabError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
