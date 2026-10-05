"""Game-day lineup check: a starter who will not play, caught while he can still be moved.

NFL teams name their inactive players 90 minutes before each kickoff, and Yahoo locks a
player when his own game starts. So the useful window is between those two moments, and
the check runs twice inside it for every kickoff time: once soon after the inactive lists
should be out, and once more just before kickoff for anything that changed late.

What this cannot catch is a player who is active, takes the field, and scores nothing.
Nothing before kickoff distinguishes him from any other active starter.

Cron wakes this every few minutes and almost every run does nothing but read the cached
schedule. A check is due when its moment has passed, its game has not started, and it has
not already succeeded, so a run that cron skipped is made up by the next one.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator, Set
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from faab.cache import write_json_atomic
from faab.collectors.yahoo import YahooPlayer
from faab.yahoo_state import WILL_NOT_PLAY, lineup_status

# Minutes before kickoff. The first lands 15 minutes after the inactive lists are due.
CHECK_OFFSETS = (75, 15)

# nflverse writes every kickoff in Eastern wall-clock time, whatever the league's own zone.
SCHEDULE_ZONE = "America/New_York"

# A check record older than this is dropped, so the state file stays small.
STATE_RETENTION = timedelta(days=10)

MAX_REPLACEMENTS = 2


@dataclass(frozen=True)
class Kickoff:
    when: datetime
    week: int
    teams: frozenset[str]

    def key(self, offset: int) -> str:
        return f"{self.when.astimezone(timezone.utc).isoformat()}@{offset}"


@dataclass(frozen=True)
class Problem:
    player: YahooPlayer
    kind: str
    reason: str
    replacements: tuple[str, ...] = ()

    @property
    def urgent(self) -> bool:
        return self.kind in ("out", "bye")


@dataclass
class State:
    checks: dict[str, str] = field(default_factory=dict)
    bye_alerts: list[str] = field(default_factory=list)
    notices: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            # A damaged file must not stop the check. Starting empty can only repeat an
            # alert, never skip one.
            return cls()
        if not isinstance(raw, dict):
            return cls()
        checks, byes, notices = raw.get("checks"), raw.get("bye_alerts"), raw.get("notices")
        return cls(
            checks={str(k): str(v) for k, v in (checks if isinstance(checks, dict) else {}).items()},
            bye_alerts=[str(b) for b in (byes if isinstance(byes, list) else [])],
            notices={str(k): str(v) for k, v in (notices if isinstance(notices, dict) else {}).items()},
        )

    def save(self, path: Path, now: datetime) -> None:
        cutoff = now - STATE_RETENTION
        kept = {}
        for key, value in self.checks.items():
            try:
                moment = datetime.fromisoformat(key.split("@", 1)[0])
            except ValueError:
                continue
            if moment >= cutoff:
                kept[key] = value
        self.checks = kept
        self.bye_alerts = self.bye_alerts[-200:]
        self.notices = {
            key: value for key, value in self.notices.items() if _dated(key) >= cutoff.date()
        }
        write_json_atomic(
            path, {"checks": self.checks, "bye_alerts": self.bye_alerts, "notices": self.notices}
        )


def _dated(key: str) -> date:
    """The date a notice key ends with, or the earliest date when it carries none."""
    try:
        return date.fromisoformat(key[-10:])
    except ValueError:
        return date.min


@contextmanager
def single_run(lock_path: Path) -> Iterator[bool]:
    """Yield True when this process holds the lock, False when another run does."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def kickoffs(games: list[dict[str, str]], season: int) -> list[Kickoff]:
    """Every scheduled kickoff of the season, grouped by start time."""
    tz = ZoneInfo(SCHEDULE_ZONE)
    grouped: dict[tuple[datetime, int], set[str]] = {}
    for row in games:
        if str(row.get("season")) != str(season):
            continue
        day, clock = row.get("gameday") or "", row.get("gametime") or ""
        try:
            week = int(row.get("week") or 0)
            local = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            continue
        teams = grouped.setdefault((local, week), set())
        teams.update(t for t in (row.get("home_team"), row.get("away_team")) if t)
    return sorted(
        (Kickoff(when=when, week=week, teams=frozenset(teams)) for (when, week), teams in grouped.items()),
        key=lambda k: k.when,
    )


def due(kickoff_list: list[Kickoff], now: datetime, state: State) -> list[tuple[Kickoff, list[str]]]:
    """Kickoffs with at least one check due, and the keys of those checks."""
    found: list[tuple[Kickoff, list[str]]] = []
    for kickoff in kickoff_list:
        if now >= kickoff.when:
            continue
        keys = [
            kickoff.key(offset)
            for offset in CHECK_OFFSETS
            if now >= kickoff.when - timedelta(minutes=offset)
            and state.checks.get(kickoff.key(offset)) != "done"
        ]
        if keys:
            found.append((kickoff, keys))
    return found


def playing_teams(kickoff_list: list[Kickoff], week: int) -> set[str]:
    return {team for k in kickoff_list if k.week == week for team in k.teams}


def season_teams(kickoff_list: list[Kickoff]) -> set[str]:
    return {team for k in kickoff_list for team in k.teams}


def missing_starters(roster: list[YahooPlayer], required: int) -> int:
    """Starting slots with nobody in them. Yahoo lists players, not empty slots."""
    return max(0, required - sum(1 for entry in roster if entry.is_starting))


def _can_fill(candidate: YahooPlayer, slot: str) -> bool:
    return slot in candidate.eligible or slot == candidate.position


def _replacements(
    problem: YahooPlayer,
    roster: list[YahooPlayer],
    playing: set[str],
    points: Callable[[YahooPlayer], float | None],
) -> tuple[str, ...]:
    options = [
        entry
        for entry in roster
        if entry.selected_position == "BN"
        and entry.is_editable is not False
        and lineup_status(entry.status) not in WILL_NOT_PLAY
        and entry.team in playing
        and _can_fill(entry, problem.selected_position)
    ]
    scored = [(points(entry), entry) for entry in options]
    scored.sort(key=lambda pair: (pair[0] is None, -(pair[0] or 0.0)))
    return tuple(
        f"{entry.name} ({value:.1f})" if value is not None else entry.name
        for value, entry in scored[:MAX_REPLACEMENTS]
    )


def find_problems(
    roster: list[YahooPlayer],
    kickoff: Kickoff,
    playing: set[str],
    points: Callable[[YahooPlayer], float | None] = lambda entry: None,
    already_alerted_byes: Set[str] = frozenset(),
    known_teams: Set[str] | None = None,
) -> list[Problem]:
    """Starters in this kickoff who will not or may not play, plus starters on bye.

    A locked starter is skipped, since nothing can be done about him. A team the schedule
    does not know is a warning rather than a bye: it means a spelling this module has not
    seen, and calling that a bye would be a confident wrong answer.
    """
    known = known_teams if known_teams is not None else playing
    problems: list[Problem] = []
    for entry in roster:
        if not entry.is_starting or entry.is_editable is False:
            continue
        status = lineup_status(entry.status)
        if entry.team not in known:
            unknown = f"team {entry.team or 'blank'} is not in the schedule, check by hand"
            if status in WILL_NOT_PLAY:
                kind, reason = "out", f"{entry.status_full or status}; {unknown}"
            else:
                kind, reason = "warn", unknown
        elif entry.team not in playing:
            if entry.player_key in already_alerted_byes:
                continue
            kind, reason = "bye", f"{entry.team} not playing this week"
        elif entry.team not in kickoff.teams:
            continue
        elif status in WILL_NOT_PLAY:
            kind, reason = "out", entry.status_full or status
        elif status:
            kind, reason = "warn", entry.status_full or status
        else:
            continue
        if entry.injury_note and kind != "bye":
            reason = f"{reason}, {entry.injury_note}"
        problems.append(
            Problem(
                player=entry,
                kind=kind,
                reason=reason,
                replacements=_replacements(entry, roster, playing, points),
            )
        )
    problems.sort(key=lambda p: (not p.urgent, p.player.selected_position, p.player.name))
    return problems


def _clock(kickoff: Kickoff, zone: str) -> str:
    local = kickoff.when.astimezone(ZoneInfo(zone))
    return f"{local:%-I:%M%p %a}".replace("AM", "am").replace("PM", "pm")


def render(problems: list[Problem], kickoff: Kickoff, zone: str, missing: int = 0) -> str:
    lines = [f"Before the {_clock(kickoff, zone)} kickoff", ""]
    if missing:
        lines.append(f"EMPTY {missing} starting slot(s) have nobody in them")
    for problem in problems:
        entry = problem.player
        label = {"out": "OUT", "bye": "BYE", "warn": "CHECK"}[problem.kind]
        lines.append(f"{label} {entry.selected_position} {entry.name} {entry.team}: {problem.reason}")
        if problem.replacements:
            lines.append(f"  start instead: {', '.join(problem.replacements)}")
        elif problem.kind != "warn":
            lines.append("  no healthy bench player can fill that slot")
    lines.extend(["", "Yahoo locks each player at his own kickoff."])
    return "\n".join(lines)


def render_failure(error: str, kickoff: Kickoff | None, zone: str) -> str:
    when = f"before the {_clock(kickoff, zone)} kickoff " if kickoff else ""
    return (
        f"The lineup check {when}failed, so nothing was verified. Check your Yahoo lineup "
        f"by hand.\n\n{error[:300]}"
    )


def fit_to_ntfy(
    problems: list[Problem], kickoff: Kickoff, zone: str, missing: int = 0, max_bytes: int = 4096
) -> str:
    """The alert, dropping replacement suggestions before it would exceed one notification."""
    text = render(problems, kickoff, zone, missing)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    bare = [Problem(p.player, p.kind, p.reason) for p in problems]
    text = render(bare, kickoff, zone, missing)
    return text if len(text.encode("utf-8")) <= max_bytes else text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
