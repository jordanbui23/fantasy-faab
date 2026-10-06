"""League state from Yahoo, in the shapes the models already take.

The models were written against a hand-typed roster and pasted pages. This module turns
the API's view of the same league into those shapes, so the models do not change and the
pasted files stay a working fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from faab.collectors import yahoo
from faab.collectors.yahoo import Getter, YahooPlayer, YahooTeam
from faab.league import League
from faab.names import INJURY_SEVERITY, NameIndex, Player, normalize_name

# Yahoo's status codes in the vocabulary `model.lineup` blocks and warns on. A code missing
# from this table is passed through as it is, and the lineup shows it as a warning.
YAHOO_STATUS = {
    "O": "OUT",
    "IR": "IR",
    "IR-R": "IR",
    "IR-NR": "IR",
    "IR-LT": "IR",
    "PUP-R": "PUP",
    "PUP-P": "PUP",
    "NFI-R": "OUT",
    "NFI-A": "OUT",
    "SUSP": "SUS",
    "NA": "NA",
    "CEL": "OUT",
    "COVID-19": "COV",
    "D": "DOUBTFUL",
    "Q": "QUESTIONABLE",
}

# Mapped statuses that mean the player will not take the field.
WILL_NOT_PLAY = frozenset({"OUT", "IR", "PUP", "SUS", "COV", "NA"})


_REVERSE_ALIASES: dict[str, list[str]] = {}
for _yahoo_abbr, _nflverse_abbr in yahoo.TEAM_ALIASES.items():
    _REVERSE_ALIASES.setdefault(_nflverse_abbr, []).append(_yahoo_abbr)


def lineup_status(code: str) -> str:
    code = (code or "").strip().upper()
    return YAHOO_STATUS.get(code, code)


def _severity(status: str) -> int:
    try:
        return INJURY_SEVERITY.index(status)
    except ValueError:
        return len(INJURY_SEVERITY) if not status else len(INJURY_SEVERITY) - 1


def more_severe(first: str, second: str) -> str:
    """The worse of two designations. A wrongly benched player is visible; a wrongly
    started one is a zero nobody can undo."""
    return first if _severity(first) <= _severity(second) else second


def marker(player: Player) -> str:
    return player.marker


def resolve_player(entry: YahooPlayer, index: NameIndex) -> tuple[Player | None, str]:
    """The pipeline's player for a Yahoo entry, carrying the worse of both statuses."""
    if entry.position == "DEF":
        # Yahoo names a defense by nickname. Sleeper, which supplies defenses, spells a
        # few abbreviations differently from nflverse, so every spelling is tried.
        spellings = [entry.name, entry.team, *_REVERSE_ALIASES.get(entry.team, [])]
        found, reason = None, "no defense under any spelling"
        for spelling in spellings:
            found, reason = index.resolve(spelling, "DEF")
            if found is not None and found.position == "DEF":
                break
            found = None
    else:
        found, reason = index.resolve(entry.name, entry.position, entry.team)
    if found is None:
        return None, reason
    status = more_severe(lineup_status(entry.status), (found.injury_status or "").upper())
    return replace(found, injury_status=status), ""


@dataclass
class LeagueState:
    league_key: str
    week: int
    own: YahooTeam
    teams: list[YahooTeam]
    own_entries: list[YahooPlayer]
    roster: list[Player]
    unresolved: list[tuple[YahooPlayer, str]]
    rostered_ids: set[str] = field(default_factory=set)
    rostered_names: set[str] = field(default_factory=set)
    entry_for: dict[str, YahooPlayer] = field(default_factory=dict)
    keepers: set[str] = field(default_factory=set)
    reserve: set[str] = field(default_factory=set)

    @property
    def unmatched_active(self) -> int:
        """Own players outside IR slots whose names did not resolve. They still hold a spot."""
        return sum(1 for entry, _ in self.unresolved if entry.selected_position != "IR")

    @property
    def active_roster(self) -> list[Player]:
        """The roster less players in IR slots, which take no bench space."""
        return [player for player in self.roster if marker(player) not in self.reserve]

    @property
    def own_budget(self) -> int:
        return self.own.faab_balance if self.own.faab_balance is not None else 0

    @property
    def rival_budgets(self) -> list[int]:
        return [
            team.faab_balance
            for team in self.teams
            if not team.is_own and team.faab_balance is not None
        ]

    def is_rostered(self, player: Player) -> bool:
        """Whether anyone in the league holds this player.

        An unresolved name on a rival roster still counts by name, so a player who failed
        to match is never offered as a claim.
        """
        if player.gsis_id and player.gsis_id in self.rostered_ids:
            return True
        return any(key in self.rostered_names for key in player.keys)


def resolve_league_key(client: Getter, league: League) -> str:
    return yahoo.pick_league(yahoo.my_leagues(client), league.yahoo_league_id).league_key


def load_state(client: Getter, league: League, index: NameIndex, week: int) -> LeagueState:
    league_key = resolve_league_key(client, league)
    teams = yahoo.league_teams(client, league_key)
    own = yahoo.own_team(teams)
    rosters = yahoo.league_rosters(client, league_key, week)
    missing = [team.team_key for team in teams if not rosters.get(team.team_key)]
    if missing:
        # A rival with no roster here would make every player he holds look claimable.
        raise yahoo.YahooError(f"{league_key}: no roster for {len(missing)} team(s): {', '.join(missing)}")
    own_entries = rosters.get(own.team_key, [])
    if not own_entries:
        raise yahoo.YahooError(f"{own.team_key}: own roster came back empty")

    state = LeagueState(
        league_key=league_key,
        week=week,
        own=own,
        teams=teams,
        own_entries=own_entries,
        roster=[],
        unresolved=[],
    )
    for team_key, entries in rosters.items():
        for entry in entries:
            player, reason = resolve_player(entry, index)
            if player is None:
                state.rostered_names.add(normalize_name(entry.name))
                if entry.position == "DEF":
                    state.rostered_names.add(normalize_name(entry.team))
                if team_key == own.team_key:
                    state.unresolved.append((entry, reason))
                continue
            if player.gsis_id:
                state.rostered_ids.add(player.gsis_id)
            else:
                state.rostered_names.update(player.keys)
            if team_key == own.team_key:
                state.roster.append(player)
                state.entry_for[marker(player)] = entry
                if entry.is_keeper:
                    state.keepers.add(marker(player))
                if entry.selected_position == "IR":
                    state.reserve.add(marker(player))
    return state


def lineup_changes(state: LeagueState, starters: list[Player]) -> tuple[list[str], list[str]]:
    """Who to move into and out of Yahoo's starting lineup to match a recommendation."""
    wanted = {marker(player) for player in starters}
    start = [
        entry.name
        for key, entry in state.entry_for.items()
        if key in wanted and not entry.is_starting
    ]
    sit = [
        entry.name
        for key, entry in state.entry_for.items()
        if key not in wanted and entry.is_starting
    ]
    return sorted(start), sorted(sit)
