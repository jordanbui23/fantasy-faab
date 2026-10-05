"""Yahoo Fantasy Sports collector: league state the pasted files used to stand in for.

Read-only. Yahoo's API has no write path for claims or lineups, so nothing here can change
the league. `docs/RESEARCH.md` section 7g records which requests return which fields.

Yahoo's JSON is not shaped like its XML. A collection is an object keyed "0", "1" and so on
plus a "count", and one resource's fields are spread across a list of single-key objects,
sometimes nested a list deeper. Every reader here walks that shape by name and never by a
fixed index, so a reordered field does not silently read the wrong value.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

from faab.collectors import yahoo_auth

API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
TIMEOUT = 30

# Yahoo's team abbreviations, upper-cased, where they differ from nflverse's.
TEAM_ALIASES = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}

BENCH_SLOTS = frozenset({"BN", "IR"})


class YahooError(RuntimeError):
    """A request failed or returned something this module cannot read."""


class Getter(Protocol):
    """Anything that answers a Fantasy API path with its `fantasy_content`."""

    def get(self, path: str) -> dict: ...


@dataclass(frozen=True)
class YahooLeague:
    league_key: str
    name: str
    num_teams: int


@dataclass(frozen=True)
class YahooTeam:
    team_key: str
    name: str
    faab_balance: int | None
    waiver_priority: int | None
    is_own: bool


@dataclass(frozen=True)
class YahooPlayer:
    player_key: str
    name: str
    position: str
    eligible: tuple[str, ...]
    team: str
    status: str
    status_full: str
    injury_note: str
    selected_position: str
    is_editable: bool | None
    bye_week: int | None

    @property
    def is_starting(self) -> bool:
        return bool(self.selected_position) and self.selected_position not in BENCH_SLOTS


@dataclass(frozen=True)
class Bid:
    team_key: str
    player_name: str
    amount: int
    timestamp: int


def nfl_team(abbr: str) -> str:
    """Yahoo's team abbreviation in nflverse's spelling, which the schedule keys on."""
    upper = (abbr or "").strip().upper()
    return TEAM_ALIASES.get(upper, upper)


def items(collection: Any, name: str) -> Iterator[Any]:
    """Each `name` entry of a Yahoo collection, in order."""
    if not isinstance(collection, dict):
        return
    count = collection.get("count")
    keys = (
        [str(i) for i in range(count)]
        if isinstance(count, int)
        else sorted((k for k in collection if k.isdigit()), key=int)
    )
    for key in keys:
        entry = collection.get(key)
        if isinstance(entry, dict) and name in entry:
            yield entry[name]


def fields(parts: Any) -> dict[str, Any]:
    """Merge a resource's list of single-key objects into one dict."""
    merged: dict[str, Any] = {}
    if isinstance(parts, dict):
        return dict(parts)
    if not isinstance(parts, list):
        return merged
    for part in parts:
        if isinstance(part, dict):
            merged.update(part)
        elif isinstance(part, list):
            merged.update(fields(part))
    return merged


def section(parts: Any, name: str) -> Any:
    """The first object in a resource list that carries `name`."""
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and name in part:
                return part[name]
    if isinstance(parts, dict):
        return parts.get(name)
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Client:
    """An authorized GET against the Fantasy API.

    A 401 means the access token aged out early, so the token is refreshed once and the
    request retried. A 5xx or a connection error is retried once after a pause, because the
    game-day check runs minutes before kickoff and one transient error should not cost a
    whole slot. Anything else raises, with Yahoo's own message.
    """

    def __init__(
        self,
        credentials: yahoo_auth.Credentials,
        token_path: Path,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.credentials = credentials
        self.token_path = token_path
        self.session = session or requests
        self.sleep = sleep

    def _token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            return yahoo_auth.access_token(self.credentials, self.token_path)
        current = yahoo_auth.load_token(self.token_path)
        if current is None:
            raise yahoo_auth.AuthError(f"no token at {self.token_path}")
        renewed = yahoo_auth.refresh(self.credentials, current)
        yahoo_auth.save_token(self.token_path, renewed)
        return renewed.access_token

    def get(self, path: str) -> dict:
        url = API_BASE + path
        token = self._token()
        refreshed = False
        retried = False
        while True:
            try:
                response = self.session.get(
                    url,
                    params={"format": "json"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                if retried:
                    raise YahooError(f"could not reach Yahoo: {exc}") from exc
                retried = True
                self.sleep(2)
                continue
            if response.status_code == 401 and not refreshed:
                refreshed = True
                token = self._token(force_refresh=True)
                continue
            if response.status_code >= 500 and not retried:
                retried = True
                self.sleep(2)
                continue
            if response.status_code != 200:
                raise YahooError(
                    f"{path}: Yahoo returned {response.status_code}: {response.text[:200]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise YahooError(f"{path}: response was not JSON") from exc
            content = payload.get("fantasy_content") if isinstance(payload, dict) else None
            if not isinstance(content, dict):
                raise YahooError(f"{path}: response has no fantasy_content")
            return content


def game_key(client: Getter, code: str = "nfl") -> str:
    """The current season's game key, which changes every year."""
    game = fields(client.get(f"/game/{code}").get("game"))
    key = str(game.get("game_key") or "")
    if not key:
        raise YahooError(f"/game/{code}: no game_key")
    return key


def my_leagues(client: Getter, code: str = "nfl") -> list[YahooLeague]:
    content = client.get(f"/users;use_login=1/games;game_keys={code}/leagues")
    leagues: list[YahooLeague] = []
    for user in items(content.get("users"), "user"):
        for game in items(section(user, "games"), "game"):
            for league in items(section(game, "leagues"), "league"):
                meta = fields(league)
                leagues.append(
                    YahooLeague(
                        league_key=str(meta.get("league_key") or ""),
                        name=str(meta.get("name") or ""),
                        num_teams=_int_or_none(meta.get("num_teams")) or 0,
                    )
                )
    return [league for league in leagues if league.league_key]


def pick_league(leagues: list[YahooLeague], league_id: int | None) -> YahooLeague:
    """The configured league, or the only one. Never a guess between several."""
    if league_id:
        for league in leagues:
            if league.league_key.rsplit(".", 1)[-1] == str(league_id):
                return league
        raise YahooError(f"league {league_id} is not among this account's leagues")
    if len(leagues) == 1:
        return leagues[0]
    if not leagues:
        raise YahooError("this Yahoo account has no league this season")
    choices = ", ".join(f"{lg.league_key.rsplit('.', 1)[-1]} ({lg.name})" for lg in leagues)
    raise YahooError(
        f"this account has {len(leagues)} leagues, so set yahoo_league_id in "
        f"league.local.toml to one of: {choices}"
    )


def _team(parts: Any) -> YahooTeam:
    meta = fields(parts[0] if isinstance(parts, list) and parts else parts)
    return YahooTeam(
        team_key=str(meta.get("team_key") or ""),
        name=str(meta.get("name") or ""),
        faab_balance=_int_or_none(meta.get("faab_balance")),
        waiver_priority=_int_or_none(meta.get("waiver_priority")),
        is_own=_int_or_none(meta.get("is_owned_by_current_login")) == 1,
    )


def league_teams(client: Getter, league_key: str) -> list[YahooTeam]:
    content = client.get(f"/league/{league_key}/teams")
    teams = [_team(team) for team in items(section(content.get("league"), "teams"), "team")]
    if not teams:
        raise YahooError(f"{league_key}: no teams in the response")
    return teams


def own_team(teams: list[YahooTeam]) -> YahooTeam:
    owned = [team for team in teams if team.is_own]
    if len(owned) != 1:
        raise YahooError(f"expected one team owned by this login, found {len(owned)}")
    return owned[0]


def _player(parts: Any) -> YahooPlayer:
    meta = fields(parts[0] if isinstance(parts, list) and parts else parts)
    extras = fields(parts[1:]) if isinstance(parts, list) else {}
    name = meta.get("name")
    full = str(name.get("full") or "") if isinstance(name, dict) else str(name or "")
    eligible = tuple(
        str(entry.get("position"))
        for entry in meta.get("eligible_positions") or []
        if isinstance(entry, dict) and entry.get("position")
    )
    selected = fields(extras.get("selected_position"))
    bye = meta.get("bye_weeks")
    editable = extras.get("is_editable", meta.get("is_editable"))
    return YahooPlayer(
        player_key=str(meta.get("player_key") or ""),
        name=full,
        position=str(meta.get("primary_position") or meta.get("display_position") or ""),
        eligible=eligible,
        team=nfl_team(str(meta.get("editorial_team_abbr") or "")),
        status=str(meta.get("status") or "").strip().upper(),
        status_full=str(meta.get("status_full") or ""),
        injury_note=str(meta.get("injury_note") or ""),
        selected_position=str(selected.get("position") or ""),
        is_editable=None if editable is None else _int_or_none(editable) == 1,
        bye_week=_int_or_none(bye.get("week")) if isinstance(bye, dict) else None,
    )


def _roster_players(team_parts: Any) -> list[YahooPlayer]:
    roster = section(team_parts, "roster")
    players: list[YahooPlayer] = []
    for block in items(roster, "players") if isinstance(roster, dict) else []:
        players.extend(_player(player) for player in items(block, "player"))
    return [player for player in players if player.player_key]


def team_roster(client: Getter, team_key: str, week: int) -> list[YahooPlayer]:
    content = client.get(f"/team/{team_key}/roster;week={week}")
    roster = section(content.get("team"), "roster")
    returned = _int_or_none(roster.get("week")) if isinstance(roster, dict) else None
    if returned != week:
        raise YahooError(f"{team_key}: asked for week {week}'s roster, got week {returned}")
    players = _roster_players(content.get("team"))
    if not players:
        raise YahooError(f"{team_key}: roster for week {week} came back empty")
    return players


def league_rosters(client: Getter, league_key: str, week: int) -> dict[str, list[YahooPlayer]]:
    """Every team's roster for one week, by team key."""
    content = client.get(f"/league/{league_key}/teams/roster;week={week}")
    rosters: dict[str, list[YahooPlayer]] = {}
    for team in items(section(content.get("league"), "teams"), "team"):
        key = _team(team).team_key
        if key:
            rosters[key] = _roster_players(team)
    if not rosters:
        raise YahooError(f"{league_key}: no rosters in the response")
    return rosters


def completed_bids(client: Getter, league_key: str) -> list[Bid]:
    """Winning FAAB bids on completed claims. Losing bids are not in the API."""
    content = client.get(f"/league/{league_key}/transactions")
    bids: list[Bid] = []
    for transaction in items(section(content.get("league"), "transactions"), "transaction"):
        meta = fields(transaction[0] if isinstance(transaction, list) else transaction)
        amount = _int_or_none(meta.get("faab_bid"))
        if amount is None or meta.get("status") != "successful":
            continue
        for player in items(section(transaction, "players"), "player"):
            pmeta = fields(player[0] if isinstance(player, list) else player)
            data = section(player, "transaction_data")
            moves = data if isinstance(data, list) else [data]
            for move in moves:
                if isinstance(move, dict) and move.get("type") == "add":
                    name = pmeta.get("name")
                    bids.append(
                        Bid(
                            team_key=str(move.get("destination_team_key") or ""),
                            player_name=str(name.get("full") if isinstance(name, dict) else name or ""),
                            amount=amount,
                            timestamp=_int_or_none(meta.get("timestamp")) or 0,
                        )
                    )
    return bids
