"""Player name normalization and lookup.

A roster is typed by hand, so a name arrives with inconsistent punctuation, a missing
generational suffix, or a nickname. Every statistical join in this project keys on
`gsis_id`, so a typed name has to reach an id before anything else can happen.

**nflverse's player directory is the id authority, not Sleeper.** Measured 2026-09-23,
Sleeper's dump carries a `gsis_id` on only 566 of its 2,743 records that have both a
position and a team, so joining through Sleeper drops most of the league. nflverse's
`players.csv` carries a `gsis_id` on all 13,956 of its `ACT` rows, and every one of the
1,306 player ids in the 2026 weekly stats is present in it. Sleeper is still needed for
two things it alone has: team defenses, which are not players and so are absent from
nflverse, and live injury status.

The normalization is lossy in one direction only. It removes what carries no identity
(case, accents, punctuation, suffixes) and keeps everything that does. It never
guesses: an unmatched or ambiguous name is reported to the caller, because starting the
wrong player is worse than being told which line of a text file to fix.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
_NON_NAME = re.compile(r"[^a-z ]+")
_SPACES = re.compile(r"\s+")

# nflverse status values a rostered player can plausibly hold. ACT is preferred when a
# name is otherwise ambiguous, which is what separates Josh Allen the Buffalo
# quarterback from Josh Allen the practice-squad center.
ACTIVE_STATUS = "ACT"
ROSTERABLE_STATUSES = frozenset({"ACT", "RES", "PUP", "SUS", "RSN", "RSR", "DEV"})

# Injury designations ordered by how much they stop a player playing. Used only to break
# a tie between two Sleeper records that share a name AND a position, where the safer
# reading is the more severe one: a wrongly benched player is visible on the bench with
# his reason printed, while a wrongly started player who never takes the field is a
# guaranteed zero the owner cannot undo.
INJURY_SEVERITY = ("OUT", "IR", "PUP", "SUS", "COV", "NA", "DNR", "DOUBTFUL", "QUESTIONABLE")


def _injury_rank(status: str) -> int:
    try:
        return INJURY_SEVERITY.index(status)
    except ValueError:
        return len(INJURY_SEVERITY)



def normalize_name(name: str) -> str:
    """Reduce a display name to a comparable key.

    Strips accents, case, punctuation and a trailing generational suffix. Returns an
    empty string for input carrying no letters, which callers must treat as a non-match
    rather than as a wildcard.
    """
    if not isinstance(name, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower().replace("-", " ").replace(".", "")
    cleaned = _NON_NAME.sub("", lowered)
    parts = [p for p in _SPACES.split(cleaned) if p]
    while len(parts) > 2 and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


@dataclass(frozen=True)
class Player:
    """One roster-eligible entity: an NFL player, or a team defense."""

    name: str
    position: str
    team: str
    gsis_id: str = ""
    status: str = ""
    injury_status: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    @property
    def keys(self) -> tuple[str, ...]:
        """Every name this entity can be found under, normalized and deduplicated."""
        found: list[str] = []
        for candidate in (self.name, *self.aliases):
            key = normalize_name(candidate)
            if key and key not in found:
                found.append(key)
        return tuple(found)

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE_STATUS


def players_from_nflverse(rows: list[dict[str, str]]) -> list[Player]:
    """Build the player list from nflverse's directory, which owns `gsis_id`.

    Retired and cut players are excluded. Keeping them would add decades of name
    collisions to an index that only ever looks up a current roster.
    """
    players: list[Player] = []
    for row in rows:
        status = (row.get("status") or "").upper()
        gsis_id = row.get("gsis_id") or ""
        name = row.get("display_name") or ""
        if status not in ROSTERABLE_STATUSES or not gsis_id or not name:
            continue
        players.append(
            Player(
                name=name,
                position=(row.get("position") or "").upper(),
                team=(row.get("latest_team") or "").upper(),
                gsis_id=gsis_id,
                status=status,
            )
        )
    return players


def team_defenses_from_sleeper(dump: Mapping[str, Any]) -> list[Player]:
    """Build team-defense entries, which exist only in Sleeper.

    A defense is indexed under its nickname, its city, its full name and its team
    abbreviation, because a roster line may spell it any of those ways. It carries no
    `gsis_id`, since a defense has no player stats to join to.
    """
    defenses: list[Player] = []
    for raw in dump.values():
        if not isinstance(raw, dict) or raw.get("position") != "DEF":
            continue
        team = str(raw.get("team") or "").upper()
        city = str(raw.get("first_name") or "")
        nickname = str(raw.get("last_name") or "")
        if not team or not nickname:
            continue
        full = f"{city} {nickname}".strip()
        defenses.append(
            Player(
                name=nickname,
                position="DEF",
                team=team,
                status=ACTIVE_STATUS,
                aliases=tuple(a for a in (full, city, team) if a),
            )
        )
    return defenses


def attach_injury_status(players: list[Player], dump: Mapping[str, Any]) -> list[Player]:
    """Copy Sleeper's injury status onto matching players, by name AND position.

    Position is part of the key because a name alone is not unique. Two players called
    Josh Allen exist in the same directory, and keying on the name alone let the second
    record seen overwrite the first. That could replace an `OUT` with a `QUESTIONABLE`
    and put a player who will not take the field into a starting slot, which is the one
    outcome the lineup builder exists to prevent.

    When two records share a name and a position, the more severe status wins. That
    choice is deliberate and is explained at `INJURY_SEVERITY`.

    A name Sleeper does not carry keeps an empty status. That means no KNOWN problem
    rather than healthy, and the distinction is why the report says to check before
    kickoff.
    """
    by_key: dict[tuple[str, str], str] = {}
    without_position: dict[str, str] = {}

    # Count EVERY record per name, not only those carrying a status. Counting only
    # status-bearing records made a name look unique when a healthy namesake existed, so
    # a positionless designation could be attached to the wrong player. Two positionless
    # records for one name also collapsed into a single entry when positions were what
    # got counted.
    records_per_name: dict[str, int] = {}
    for raw in dump.values():
        if not isinstance(raw, dict):
            continue
        key = normalize_name(str(raw.get("full_name") or ""))
        if key:
            records_per_name[key] = records_per_name.get(key, 0) + 1

    for raw in dump.values():
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("injury_status") or "").strip().upper()
        key = normalize_name(str(raw.get("full_name") or ""))
        position = str(raw.get("position") or "").upper()
        if not key or not status:
            continue

        if position:
            existing = by_key.get((key, position))
            if existing is None or _injury_rank(status) < _injury_rank(existing):
                by_key[(key, position)] = status
        else:
            existing = without_position.get(key)
            if existing is None or _injury_rank(status) < _injury_rank(existing):
                without_position[key] = status

    updated: list[Player] = []
    for player in players:
        status = by_key.get((player.key, player.position), "")
        if not status and records_per_name.get(player.key, 0) == 1:
            # Three real records carry a status with no position. Applying one by name
            # alone is safe only when that name appears once in the whole dump, so it
            # cannot belong to somebody else.
            status = without_position.get(player.key, "")
        updated.append(
            Player(
                name=player.name,
                position=player.position,
                team=player.team,
                gsis_id=player.gsis_id,
                status=player.status,
                injury_status=status or player.injury_status,
                aliases=player.aliases,
            )
        )
    return updated


class NameIndex:
    """Maps a normalized name to every player who can be found under it."""

    def __init__(self, players: list[Player]):
        self._by_key: dict[str, list[Player]] = {}
        for player in players:
            for key in player.keys:
                self._by_key.setdefault(key, []).append(player)

    def __len__(self) -> int:
        return len(self._by_key)

    def candidates(self, name: str) -> list[Player]:
        return list(self._by_key.get(normalize_name(name), []))

    def resolve(
        self, name: str, position: str = "", team: str = ""
    ) -> tuple[Player | None, str]:
        """Resolve one name to a single player.

        Narrows an ambiguous name by position, then by team. Anything still ambiguous is
        reported rather than guessed, with one narrow exception described below.

        The exception: a single active player wins when every other candidate plays a
        DIFFERENT position. Josh Allen is both the active Buffalo quarterback and a
        practice-squad center, and nobody typing that name means the center. Two
        candidates at the SAME position stay ambiguous even when one is active, because
        nothing here distinguishes them and picking the active one would silently start
        a stranger in place of the reserve player the owner actually rosters.
        """
        key = normalize_name(name)
        if not key:
            return None, "no letters in the name"

        matches = self._by_key.get(key, [])
        if not matches:
            return None, "no player with that name"

        if len(matches) > 1 and position:
            narrowed = [p for p in matches if p.position == position.upper()]
            if narrowed:
                matches = narrowed
        if len(matches) > 1 and team:
            narrowed = [p for p in matches if p.team == team.upper()]
            if narrowed:
                matches = narrowed
        if len(matches) > 1:
            active = [p for p in matches if p.is_active]
            if len(active) == 1:
                others = [p for p in matches if p is not active[0]]
                # Every position must be KNOWN and different. An empty position is
                # unknown, not proof of a different one, and treating it as different
                # let an active player win over a candidate who might share his spot.
                distinguishable = bool(active[0].position) and all(
                    p.position and p.position != active[0].position for p in others
                )
                if distinguishable:
                    matches = active

        if len(matches) > 1:
            where = ", ".join(
                " ".join(filter(None, (p.position, p.team, p.status))) for p in matches
            )
            return None, f"ambiguous, matches {len(matches)}: {where}"
        return matches[0], ""


def build_index(
    nflverse_players: list[dict[str, str]], sleeper_dump: Mapping[str, Any]
) -> NameIndex:
    """The index the whole pipeline resolves against.

    nflverse supplies players and their ids, Sleeper supplies team defenses and injury
    status.
    """
    players = attach_injury_status(
        players_from_nflverse(nflverse_players), sleeper_dump
    )
    return NameIndex(players + team_defenses_from_sleeper(sleeper_dump))
