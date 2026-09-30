"""Roster loading.

The roster is typed into a plain text file rather than fetched, because Yahoo gated its
API behind an approval that has not arrived. This is the stopgap, and it is replaced by
the Yahoo collector without changing anything downstream: both produce a list of
resolved `Player` records.

The file lives at `data/roster.txt` and is gitignored, because a roster is league data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faab.names import NameIndex, Player

POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF", "DST"})


@dataclass(frozen=True)
class RosterLine:
    """One line of the roster file, before resolution."""

    raw: str
    line_number: int
    name: str
    position: str = ""
    team: str = ""


@dataclass(frozen=True)
class Unresolved:
    line: RosterLine
    reason: str


def parse_roster_file(text: str) -> list[RosterLine]:
    """Parse the roster file into lines.

    One player per line. A `#` comment and a blank line are skipped. Trailing tokens
    that look like a position or a team are treated as disambiguation hints, so both
    `Ja'Marr Chase` and `Ja'Marr Chase WR CIN` work. A hint is optional and only
        narrows an ambiguous name.
    """
    lines: list[RosterLine] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        tokens = stripped.replace(",", " ").split()
        position = ""
        team = ""
        # Consume hints from the end so a name containing a short word survives.
        while len(tokens) > 1:
            candidate = tokens[-1].upper()
            if not position and candidate in POSITIONS:
                position = "DEF" if candidate == "DST" else candidate
                tokens.pop()
                continue
            if not team and 2 <= len(candidate) <= 3 and candidate.isalpha():
                team = candidate
                tokens.pop()
                continue
            break
        name = " ".join(tokens).strip()
        if not name:
            continue
        lines.append(
            RosterLine(
                raw=stripped, line_number=number, name=name, position=position, team=team
            )
        )
    return lines


def resolve_roster(
    lines: list[RosterLine], index: NameIndex
) -> tuple[list[Player], list[Unresolved]]:
    """Resolve parsed lines to players.

    Returns the resolved players and every line that could not be resolved, with the
    reason. Nothing is guessed: an unmatched or ambiguous line comes back so the owner
    can fix it, because a silently wrong player means a wrong lineup.
    """
    resolved: list[Player] = []
    failed: list[Unresolved] = []
    seen: set[str] = set()

    for line in lines:
        player, reason = index.resolve(line.name, line.position, line.team)
        if player is None:
            failed.append(Unresolved(line=line, reason=reason))
            continue
        marker = player.gsis_id or f"{player.position}:{player.key}"
        if marker in seen:
            failed.append(Unresolved(line=line, reason="duplicate of an earlier line"))
            continue
        seen.add(marker)
        resolved.append(player)
    return resolved, failed


def load_roster(path: Path, index: NameIndex) -> tuple[list[Player], list[Unresolved]]:
    """Read and resolve the roster file. Raises FileNotFoundError when absent."""
    return resolve_roster(parse_roster_file(path.read_text(encoding="utf-8")), index)


ROSTER_TEMPLATE = """\
# Your Yahoo roster, one player per line.
#
# Position and team are optional. They are only used to disambiguate two players who
# share a name, so plain names are fine:
#
#   Bijan Robinson
#   Puka Nacua WR LA
#   Seahawks DEF
#
# A line that cannot be matched is reported rather than guessed, so a report will tell
# you which line to fix. This file is gitignored.
"""
