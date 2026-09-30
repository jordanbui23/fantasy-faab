"""Read raw pasted text from Yahoo's web pages.

The Yahoo API is gated behind an approval that has not arrived, so the league facts it
would supply are pasted in by hand instead. The roster paste was converted by hand once.
That does not survive a weekly cadence, so this module reads Yahoo's own output directly.

It does not parse Yahoo's layout, deliberately. Column order, injury flags and the
surrounding chrome all change without notice, and a parser keyed to them breaks silently
the week Yahoo ships a redesign. Instead every line is scanned for the longest run of
leading words that resolves to a real player in the nflverse index. A line that resolves
is a player, a line that does not is reported rather than dropped.

That inverts the usual failure mode. A layout change costs nothing, while a genuinely
unknown name is surfaced and can be fixed by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from faab.names import NameIndex, Player
from faab.roster import POSITIONS

# Longest name this will try, in words. "Amon-Ra St. Brown" and "Ollie Gordon II" are
# three; four leaves room for a suffix Yahoo appends to the name itself.
MAX_NAME_WORDS = 4

# One word is a real name here, because every team defense has one. A single word is safe
# to attempt only because `NameIndex.resolve` refuses an ambiguous match, so a bare surname
# is rejected rather than guessed at.
MIN_NAME_WORDS = 1

# Words this many tokens deep are scanned for a trailing position or team hint, which sit
# after the name and must not be mistaken for part of it.
MAX_LINE_WORDS = 6

# Yahoo renders a widget label into the same line as the name, so a line carrying one of
# these is chrome and never a player. Skipping them keeps a raw paste from reporting a
# failure for every row it already matched.
WIDGET_MARKERS = (
    "player note",
    "video forecast",
    "video playlist",
    "new player notes",
    "no new player",
    "forecast",
)

# Tokens that appear beside a name and are never part of one. Injury flags, roster status,
# and the free-agent marker. Positions and teams are handled separately because a two or
# three letter uppercase token is ambiguous with an initial.
NOISE_TOKENS = frozenset(
    {
        "Q",
        "O",
        "D",
        "P",
        "IR",
        "IL",
        "SUS",
        "NA",
        "PUP",
        "COV",
        "DNR",
        "FA",
        "W",
        "GTD",
        "OUT",
        "DTD",
    }
)

_MONEY = re.compile(r"^\$?(\d{1,4})$")
_NUMERIC = re.compile(r"^[-+$]?\d+(?:[.,]\d+)?%?$")
# Requires a letter somewhere but permits a leading digit, because "49ers" is a real team
# name. A purely numeric token is rejected by _NUMERIC before this is ever consulted.
_WORD = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9'’.\-]*$")


@dataclass(frozen=True)
class PasteLine:
    """One line of pasted text, and what was made of it."""

    raw: str
    line_number: int
    name: str = ""
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return not self.reason


@dataclass(frozen=True)
class TeamBudget:
    """One team's remaining FAAB, as pasted from the standings page."""

    team: str
    budget: int


def _candidate_words(line: str) -> list[str]:
    """The leading words of a line that could belong to a name or its hints."""
    cleaned = line.replace("|", " ").replace("\t", " ")
    # A trailing "- TEAM" or "(TEAM)" is Yahoo chrome, never part of a name.
    cleaned = re.sub(r"[-(),]", " ", cleaned)
    words: list[str] = []
    for token in cleaned.split():
        if _NUMERIC.match(token):
            break
        if token.upper() in NOISE_TOKENS:
            break
        if not _WORD.match(token):
            break
        words.append(token)
        if len(words) >= MAX_LINE_WORDS:
            break
    return words


def _split_hints(words: list[str]) -> tuple[list[str], str, str]:
    """Separate a trailing position and team from the name they follow.

    Consumes at most one of each, from the end, so "Ted Hurst III TB" keeps the suffix and
    takes only TB as the team. Without this an ambiguous name stays ambiguous even when the
    line beside it says which position and team are meant.
    """
    remaining = list(words)
    position = ""
    team = ""
    while len(remaining) > 1:
        candidate = remaining[-1].upper()
        if not position and candidate in POSITIONS:
            position = "DEF" if candidate == "DST" else candidate
            remaining.pop()
            continue
        if not team and 2 <= len(candidate) <= 3 and candidate.isalpha():
            team = candidate
            remaining.pop()
            continue
        break
    return remaining, position, team


def extract_players(
    text: str, index: NameIndex
) -> tuple[list[Player], list[PasteLine]]:
    """Pull every player out of pasted text.

    Tries the longest leading run of words first, so "Ollie Gordon II" is preferred over
    "Ollie Gordon" when both resolve. A line yielding nothing is returned for review.
    """
    found: list[Player] = []
    seen: set[str] = set()
    problems: list[PasteLine] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue

        lowered = stripped.lower()
        if any(marker in lowered for marker in WIDGET_MARKERS):
            continue

        words = _candidate_words(stripped)
        name_words, position, team = _split_hints(words)
        if len(name_words) < MIN_NAME_WORDS:
            continue
        # A lone short uppercase token is a team code left over from a line like "MIA - RB",
        # never a name. Every real one-word name here is a team nickname, which is longer.
        if len(name_words) == 1 and len(name_words[0]) <= 3 and name_words[0].isupper():
            continue

        player: Player | None = None
        attempted = ""
        failures: list[tuple[str, str]] = []
        for length in range(min(len(name_words), MAX_NAME_WORDS), MIN_NAME_WORDS - 1, -1):
            attempted = " ".join(name_words[:length])
            candidate, reason = index.resolve(attempted, position, team)
            if candidate is not None:
                player = candidate
                break
            failures.append((attempted, reason))

        if player is None:
            # Only a plausible full name is worth reporting. A single word that resolves to
            # nothing is almost always a column header, and reporting those would bury the
            # one genuinely unknown player in a page of chrome. One-word names are still
            # ATTEMPTED, because every team defense is one.
            if len(name_words) < 2:
                continue
            # An ambiguous name is actionable, so it outranks "no player with that name"
            # even when a shorter attempt produced the latter.
            ambiguous = next((f for f in failures if "ambiguous" in f[1].lower()), None)
            attempted, reason = ambiguous or failures[0]
            problems.append(
                PasteLine(
                    raw=stripped, line_number=number, name=attempted, reason=reason
                )
            )
            continue

        # A team defense carries no gsis_id, so keying dedup on it alone collapses all
        # 32 of them into one. Fall back to the fields that do distinguish them.
        key = player.gsis_id or f"{player.name}|{player.position}|{player.team}"
        if key in seen:
            continue
        seen.add(key)
        found.append(player)

    return found, problems


_PROJECTION_LINE = re.compile(
    r"^(?P<name>.+?)\s+(?P<position>QB|RB|WR|TE|K|DEF|DST)\s+"
    r"(?P<team>[A-Z0-9]{2,3})\s+(?P<points>\d+(?:\.\d+)?)\s*$"
)


def extract_projections(text: str, index: NameIndex) -> dict[str, float]:
    """Yahoo's own projected points per player, keyed by `gsis_id`.

    Read from the same paste as the availability list, where each line ends in the number
    Yahoo shows in its Fan Pts column. Only a line carrying an explicit position, team and
    number is used, so a name pasted without them contributes availability but no market
    number and is silently skipped.

    These matter because they are an INDEPENDENT estimate. Measured over 116 available
    players, this project's own projection sits about two points above Yahoo's for a receiver
    and two below for a quarterback, and the players it is most wrong about are the ones its
    ranking promotes. A second opinion is the cheapest available defence against that.
    """
    market: dict[str, float] = {}
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        match = _PROJECTION_LINE.match(stripped)
        if match is None:
            continue
        position = match.group("position")
        player, _ = index.resolve(
            match.group("name"),
            "DEF" if position == "DST" else position,
            match.group("team"),
        )
        if player is None or not player.gsis_id:
            continue
        market[player.gsis_id] = float(match.group("points"))
    return market


def extract_budgets(text: str) -> list[TeamBudget]:
    """Pull team names and remaining FAAB out of a pasted standings block.

    A team name is whatever precedes the money on its line, because a fantasy team name
    can be any string at all and cannot be validated against anything.
    """
    budgets: list[TeamBudget] = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        tokens = stripped.replace("\t", " ").split()
        if len(tokens) < 2:
            continue
        match = _MONEY.match(tokens[-1])
        if match is None:
            continue
        amount = int(match.group(1))
        if amount > 1000:
            continue
        team = " ".join(tokens[:-1]).strip(" .:-")
        if not team:
            continue
        budgets.append(TeamBudget(team=team, budget=amount))
    return budgets
