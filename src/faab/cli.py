"""Command line entry points, driven by cron.

    python -m faab lineup            print the Sunday start/sit report
    python -m faab lineup --send     print it and push it to ntfy
    python -m faab waivers           print the waiver claim sheet
    python -m faab gameday           check starters before each kickoff
    python -m faab roster            show how each roster player resolved

Rosters, budgets and the free-agent pool come from Yahoo when a token exists, and from the
pasted files in data/ otherwise. `--source` forces one or the other.

Exit codes: 0 success, 1 a usage or data problem the owner must fix, 2 a delivery
failure. cron distinguishes them, so a silent failure cannot look like a quiet week.
"""

from __future__ import annotations

import argparse
import os
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from faab import gameday as gameday_mod
from faab import notify, paste, report, yahoo_state
from faab.cache import FetchError
from faab.collectors import nflverse
from faab.collectors import yahoo
from faab.collectors import yahoo_auth
from faab.collectors.sleeper import fetch_players
from faab.league import ConfigError, League, load_league
from faab.model.lineup import recommend_lineup
from faab.model.project import build_projections
from faab.model.usage import build_usage
from faab.model import need
from faab.model import waiver as waiver_model
from faab.collectors.sleeper import fetch_trending_players
from faab.names import NameIndex, Player, build_index
from faab.roster import ROSTER_TEMPLATE, load_roster

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
ROSTER_PATH = DATA_DIR / "roster.txt"
LEAGUE_PATH = REPO_ROOT / "league.toml"

# Players priced as possible claims when the pool comes from Yahoo, best projected first.
# About the size of the pasted available-players page it replaces.
MAX_PRICED_CANDIDATES = 150

# A pasted projections page older than this is last week's, and is ignored.
MARKET_MAX_AGE_SECONDS = 4 * 24 * 60 * 60

# A cached schedule older than this is not trusted for kickoff times, because the league
# can move a game during the season.
SCHEDULE_CACHE_MAX_AGE_SECONDS = 3 * 24 * 60 * 60


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_schedule(spec: str) -> tuple[int, int]:
    """Parse a `Day:Hour` gate such as `sun:9` into a weekday index and an hour.

    Raises ValueError on anything unparseable, so a mistyped cron entry fails loudly
    the first time rather than silently never firing.
    """
    parts = spec.strip().lower().split(":")
    if len(parts) != 2:
        raise ValueError(f"schedule must be Day:Hour, got {spec!r}")
    day, hour_raw = parts
    if day not in WEEKDAYS:
        raise ValueError(f"unknown day {day!r}, expected one of {', '.join(WEEKDAYS)}")
    if not hour_raw.isdigit() or not 0 <= int(hour_raw) <= 23:
        raise ValueError(f"hour must be 0..23, got {hour_raw!r}")
    return WEEKDAYS.index(day), int(hour_raw)


def is_scheduled_now(spec: str, timezone: str, now: datetime | None = None) -> bool:
    """Whether the local wall clock matches the gate.

    The gate lives here rather than in the cron expression because the box runs UTC
    while the schedule is in Eastern time, and a fixed UTC hour drifts by one when
    daylight saving changes. An hourly cron plus this check is correct year round.
    """
    weekday, hour = parse_schedule(spec)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {timezone!r}: {exc}") from exc
    local = (now or datetime.now(zone)).astimezone(zone)
    return local.weekday() == weekday and local.hour == hour


def _build_index(data_dir: Path) -> NameIndex:
    return build_index(
        nflverse.load_players(data_dir),
        fetch_players(data_dir / "sleeper_players.json"),
    )


def _ensure_roster(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ROSTER_TEMPLATE, encoding="utf-8")
    raise SystemExit(
        f"No roster yet. A template was written to {path}\n"
        "Add your players, one per line, then run this again."
    )


def _yahoo_client(args: argparse.Namespace) -> yahoo.Client | None:
    """A client when credentials and a token both exist, otherwise None."""
    token_path = args.data / "yahoo_token.json"
    if not token_path.exists():
        return None
    env = {**yahoo_auth.read_env_file(args.data.parent / ".env"), **os.environ}
    try:
        credentials = yahoo_auth.Credentials.from_env(env)
    except yahoo_auth.AuthError:
        return None
    return yahoo.Client(credentials, token_path)


def _league_state(
    args: argparse.Namespace, league: League, index: NameIndex, week: int
) -> tuple[yahoo_state.LeagueState | None, str]:
    """Yahoo's view of the league, or None and the reason the pasted files are used.

    With `--source auto` a Yahoo failure falls back to the pasted files and says so in the
    report, because a week-old paste is worse than live data but better than no report.
    With `--source yahoo` the failure is the result.
    """
    source = getattr(args, "source", "auto")
    if source == "paste":
        return None, ""
    client = _yahoo_client(args)
    if client is None:
        if source == "yahoo":
            raise ConfigError("no Yahoo token or credentials. Run `python -m faab auth` first.")
        return None, ""
    try:
        return yahoo_state.load_state(client, league, index, week), ""
    except (yahoo.YahooError, yahoo_auth.AuthError) as exc:
        if source == "yahoo":
            raise
        return None, f"Yahoo unavailable, so pasted files were used: {exc}"


def _fallback_banner(note: str) -> str:
    """The warning that leads a report built from pasted files, so it can never be trimmed."""
    return f"{note}\n\n" if note else ""


def _append_notes(body: str, notes: list[str]) -> str:
    for note in notes:
        candidate = f"{body}\n{note}"
        if len(candidate.encode("utf-8")) <= report.NTFY_MAX_BYTES:
            body = candidate
    return body


def cmd_roster(args: argparse.Namespace) -> int:
    index = _build_index(args.data)
    league = load_league(args.league)
    games = nflverse.load_games(args.data)
    week = nflverse.upcoming_week(games, league.season) or 1
    state, note = _league_state(args, league, index, week)
    if state is not None:
        print(f"Yahoo roster, week {week}: {len(state.roster)} resolved, "
              f"{len(state.unresolved)} unresolved\n")
        for player in state.roster:
            entry = state.entry_for[yahoo_state.marker(player)]
            flag = player.injury_status or "-"
            print(f"  ok   {entry.selected_position:<5} {player.name:<22} "
                  f"{player.position:<3} {player.team:<4} {flag}")
        for entry, reason in state.unresolved:
            print(f"  FAIL {entry.selected_position:<5} {entry.name} -- {reason}")
        return 1 if state.unresolved else 0
    if note:
        print(note)
    _ensure_roster(args.roster)
    resolved, failed = load_roster(args.roster, index)

    print(f"{len(resolved)} resolved, {len(failed)} unresolved\n")
    for player in resolved:
        ids = "gsis" if player.gsis_id else "NO GSIS ID"
        print(f"  ok   {player.name:<22} {player.position:<3} {player.team:<4} {ids}")
    for failure in failed:
        print(
            f"  FAIL line {failure.line.line_number}: "
            f"{failure.line.raw!r} -- {failure.reason}"
        )
    return 1 if failed else 0


def cmd_lineup(args: argparse.Namespace) -> int:
    league = load_league(args.league)
    index = _build_index(args.data)

    games = nflverse.load_games(args.data)
    week = args.week or nflverse.upcoming_week(games, league.season)
    if not week:
        print(f"No upcoming week found for {league.season}. Season may be over.")
        return 1

    state, fallback_note = _league_state(args, league, index, week)
    if state is not None:
        roster, unmatched = state.roster, len(state.unresolved)
    else:
        _ensure_roster(args.roster)
        roster, failed = load_roster(args.roster, index)
        unmatched = len(failed)

    if not roster:
        print("Roster resolved to nobody. Run `python -m faab roster` to see why.")
        return 1
    through = args.through_week or nflverse.completed_week(games, league.season)

    stats = nflverse.load_weekly_player_stats(league.season, args.data)
    usage = build_usage(
        stats,
        nflverse.load_snap_counts(league.season, args.data),
        nflverse.load_players(args.data),
        through_week=through,
    )
    projections = build_projections(stats, through_week=through, scoring=league.scoring)
    byes = nflverse.teams_on_bye(games, league.season, week)
    lineup = recommend_lineup(
        roster, usage, projections, byes, week, league, through_week=through
    )

    prefix = _fallback_banner(fallback_note)
    if state is not None:
        start, sit = yahoo_state.lineup_changes(state, [c.starter.player for c in lineup.choices])
        if start or sit:
            moves = [f"  start {name}" for name in start] + [f"  sit {name}" for name in sit]
            prefix += "\n".join(["CHANGE IN YAHOO", *moves, "", ""])
        else:
            prefix += "Your Yahoo lineup already matches.\n\n"
    budget = report.NTFY_MAX_BYTES - len(prefix.encode("utf-8"))
    body = prefix + report.fit_to_ntfy(lineup, max_bytes=budget)
    if unmatched:
        body = _append_notes(body, [f"{unmatched} roster player(s) unmatched, run `faab roster`"])

    print(body)

    if args.snapshot:
        _write_snapshot(args.data, week, through, lineup, body)

    if args.send:
        try:
            notify.send(body, title=f"Week {week} lineup")
        except notify.NotifyError as exc:
            print(f"\nDelivery failed: {exc}", file=sys.stderr)
            return 2
        print("\nSent to ntfy.")
    return 0


def _write_snapshot(
    data_dir: Path, week: int, through: int, lineup: object, body: str
) -> None:
    """Keep what the report said, so it can be scored against what happened."""
    from dataclasses import asdict, is_dataclass

    target = data_dir / "snapshots" / f"lineup_week{week:02d}.json"
    payload = {
        "week": week,
        "stats_through_week": through,
        "rendered": body,
        "lineup": asdict(lineup) if is_dataclass(lineup) and not isinstance(lineup, type) else None,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load_available(data_dir: Path, index: NameIndex) -> tuple[set[str] | None, list]:
    """The free-agent pool from `data/available.txt`, or None when it is absent.

    None and an empty set mean different things. None means nothing was pasted, so the
    other rosters are invisible. An empty set means the paste contained no claimable
    player, and nothing should be recommended.
    """
    path = data_dir / "available.txt"
    if not path.exists():
        return None, []
    players, problems = paste.extract_players(path.read_text(encoding="utf-8"), index)
    return {p.gsis_id for p in players if p.gsis_id}, problems


def _load_budgets(data_dir: Path, league: League) -> tuple[list[int], int]:
    """Rival FAAB budgets and this team's own, from `data/budgets.txt`.

    The owner's team is identified by `own_team`, normally set in the gitignored
    `league.local.toml` because it identifies its owner. When the budgets file is absent,
    or the owner's team is not in it, the configured budget stands and rivals are unknown.
    """
    path = data_dir / "budgets.txt"
    if not path.exists():
        return [], league.faab_budget
    entries = paste.extract_budgets(path.read_text(encoding="utf-8"))
    own_name = (league.own_team or "").strip().lower()
    own = league.faab_budget
    rivals: list[int] = []
    for entry in entries:
        if own_name and entry.team.strip().lower() == own_name:
            own = entry.budget
        else:
            rivals.append(entry.budget)
    return rivals, own


def _load_market(data_dir: Path, index: NameIndex) -> dict[str, float]:
    """Yahoo's projections from the availability paste, empty when none was pasted."""
    path = data_dir / "available.txt"
    if not path.exists():
        return {}
    return paste.extract_projections(path.read_text(encoding="utf-8"), index)


def _available_players(data_dir: Path, index: NameIndex) -> list[Player]:
    path = data_dir / "available.txt"
    if not path.exists():
        return []
    players, _ = paste.extract_players(path.read_text(encoding="utf-8"), index)
    return [p for p in players if p.gsis_id]


def _market_covers(roster: list[Player], market: dict[str, float]) -> bool:
    """Whether Yahoo numbers exist for the owner's own roster as well as the pool.

    When they do not, a drop is valued on this project's projection while an add is valued on
    the lower of two, which tilts every comparison toward making the claim.
    """
    held = [p.gsis_id for p in roster if p.gsis_id]
    return bool(held) and all(gsis in market for gsis in held)


def _price_swaps(
    pool: list[Player],
    roster: list[Player],
    usage: dict,
    projections: dict,
    byes: set[str],
    week: int,
    through: int,
    league: League,
    market: dict[str, float],
) -> list:
    """Every claim in the pool, priced as a swap and ranked by net gain."""
    context = need.Context(
        roster=roster,
        usage=usage,
        projections=projections,
        byes=byes,
        week=week,
        league=league,
        through_week=through,
        roster_size=max(len(roster), league.starters),
        market=market,
    )
    base = need.projected_total(need.baseline_lineup(context))
    swaps = [need.best_swap(context, player, base) for player in pool]
    swaps.sort(key=lambda s: (-s.gain, -s.optimistic_gain))
    return need.best_per_position(swaps)


def _maybe_send(args: argparse.Namespace, body: str, week: int) -> int:
    if not args.send:
        return 0
    try:
        notify.send(body, title=f"Week {week} waivers", priority="high")
    except notify.NotifyError as exc:
        print(f"\nDelivery failed: {exc}", file=sys.stderr)
        return 2
    print("\nSent to ntfy.")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    """Authorize once, or refresh and report on an existing token.

    Interactive by design and never called from cron. `access_token` refreshes silently on
    the scheduled path, so this is only run when there is no token at all.
    """
    env = {**yahoo_auth.read_env_file(args.data.parent / ".env"), **os.environ}
    try:
        credentials = yahoo_auth.Credentials.from_env(env)
    except yahoo_auth.AuthError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    token_path = args.data / "yahoo_token.json"
    if args.code:
        try:
            token = yahoo_auth.exchange_code(
                credentials, yahoo_auth.code_from_redirect(args.code)
            )
        except yahoo_auth.AuthError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        yahoo_auth.save_token(token_path, token)
        print(f"Authorized. Token saved to {token_path} with owner-only permissions.")
        return 0

    try:
        existing = yahoo_auth.load_token(token_path)
    except yahoo_auth.AuthError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    if existing is not None and not existing.expired:
        remaining = int(existing.expires_at - time.time())
        print(f"Token valid for another {remaining // 60} minutes.")
        return 0
    if existing is not None:
        token = yahoo_auth.refresh(credentials, existing)
        yahoo_auth.save_token(token_path, token)
        print("Token refreshed.")
        return 0

    print("Open this once, approve, then re-run with --code and the redirected URL:")
    print()
    print(yahoo_auth.authorization_url(credentials))
    print()
    print("The browser will fail to reach localhost. That is expected: the code is in")
    print("the address bar.")
    return 0


def _market_is_fresh(data_dir: Path) -> bool:
    path = data_dir / "available.txt"
    return path.exists() and time.time() - path.stat().st_mtime < MARKET_MAX_AGE_SECONDS


def _yahoo_pool(
    projections: dict, index: NameIndex, state: yahoo_state.LeagueState, league: League
) -> list[Player]:
    """Unrostered players worth pricing, best projected first.

    Everyone not on a roster in the league is claimable through FAAB, free agent or on
    waivers alike, so the pool is the complement of the rosters rather than a page.
    """
    pool: list[Player] = []
    ranked = sorted(projections.values(), key=lambda p: -p.points)
    for projection in ranked:
        if projection.position not in league.claim_positions:
            continue
        player, _ = index.resolve(projection.name, projection.position, projection.team)
        if player is None or player.gsis_id != projection.gsis_id:
            continue
        if state.is_rostered(player):
            continue
        pool.append(player)
        if len(pool) >= MAX_PRICED_CANDIDATES:
            break
    return pool


def cmd_waivers(args: argparse.Namespace) -> int:
    league = load_league(args.league)
    index = _build_index(args.data)

    games = nflverse.load_games(args.data)
    through = args.through_week or nflverse.completed_week(games, league.season)
    if not through:
        print(f"No completed week yet for {league.season}.")
        return 1
    week = args.week or nflverse.upcoming_week(games, league.season) or through + 1

    state, fallback_note = _league_state(args, league, index, week)
    if state is not None:
        roster, failed = state.roster, state.unresolved
    else:
        _ensure_roster(args.roster)
        roster, failed = load_roster(args.roster, index)

    stats = nflverse.load_weekly_player_stats(league.season, args.data)
    usage = build_usage(
        stats,
        nflverse.load_snap_counts(league.season, args.data),
        nflverse.load_players(args.data),
        through_week=through,
    )
    projections = build_projections(stats, through_week=through, scoring=league.scoring)
    byes = nflverse.teams_on_bye(games, league.season, week)

    trending = {
        row["name"]: row["count"]
        for row in fetch_trending_players(
            args.data / "sleeper_players.json", kind="add", lookback_hours=72, limit=150
        )
        if row.get("name") and isinstance(row.get("count"), int)
    }

    if state is not None:
        available, available_problems = None, []
        rival_budgets, own_budget = state.rival_budgets, state.own_budget
        market = _load_market(args.data, index) if _market_is_fresh(args.data) else {}
        pool = _yahoo_pool(projections, index, state, league)
    else:
        available, available_problems = _load_available(args.data, index)
        rival_budgets, own_budget = _load_budgets(args.data, league)
        market = _load_market(args.data, index)
        pool = [
            p for p in _available_players(args.data, index)
            if p.position in league.claim_positions
        ]

    banner = _fallback_banner(fallback_note)
    budget_bytes = report.NTFY_MAX_BYTES - len(banner.encode("utf-8"))
    if state is not None and not pool:
        # The usage-based sheet below excludes only this team's roster. With Yahoo's view
        # in hand that would be a step backwards, offering players rivals already hold.
        body = f"WEEK {week} WAIVERS\n\nNo unrostered player at {', '.join(league.claim_positions)} has a projection."
        print(body)
        return _maybe_send(args, body, week)

    if pool:
        swaps = _price_swaps(
            pool, roster, usage, projections, byes, week, through, league, market
        )
        gains = [s.gain for s in swaps]
        bids = {
            s.add.name: waiver_model.bid_for_swap(
                s.gain,
                [g for g in gains if g is not s.gain],
                own_budget,
                rival_budgets=rival_budgets,
            )
            for s in swaps
        }
        rivals_able = sum(1 for b in rival_budgets if b >= own_budget * 0.12)
        body = report.fit_swaps_to_ntfy(
            swaps,
            week,
            own_budget,
            bids,
            rivals_able=rivals_able,
            rival_count=len(rival_budgets),
            market_covers_roster=_market_covers(roster, market),
            max_bytes=budget_bytes,
        )
        body = banner + body
        if failed:
            body = _append_notes(body, [f"{len(failed)} roster player(s) unmatched, run `faab roster`"])
        print(body)
        return _maybe_send(args, body, week)

    candidates = waiver_model.find_candidates(
        usage,
        through_week=through,
        rostered_gsis_ids={p.gsis_id for p in roster if p.gsis_id},
        trending_by_name=trending,
        available_gsis_ids=available,
    )
    bids = {
        c.name: waiver_model.suggested_bid(
            c.role_strength, c.trending_adds, own_budget, rival_budgets=rival_budgets
        )
        for c in candidates
    }

    body = banner + report.fit_waivers_to_ntfy(
        candidates, week, own_budget, bids, stopgap=available is None, max_bytes=budget_bytes
    )
    notes: list[str] = []
    if failed:
        # An unresolved roster line is not in rostered_gsis_ids, so a player the owner
        # already holds can be suggested as a claim. Say so rather than look confident.
        notes.append(f"{len(failed)} roster line(s) unmatched, so an owned player may appear.")
    if available is not None:
        notes.append(f"Filtered to {len(available)} players you can claim.")
    if available_problems:
        notes.append(f"{len(available_problems)} pasted line(s) unmatched.")
    body = _append_notes(body, notes)
    print(body)

    if args.send:
        try:
            notify.send(body, title=f"Week {week} waivers", priority="high")
        except notify.NotifyError as exc:
            print(f"\nDelivery failed: {exc}", file=sys.stderr)
            return 2
        print("\nSent to ntfy.")
    return 0


def _parse_now(text: str) -> datetime:
    if not text:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"--now needs a UTC offset, got {text!r}")
    return moment


def _points_for(args: argparse.Namespace, league: League, games: list) -> object:
    """Projected points per Yahoo roster entry, used only to order replacement options."""
    index = _build_index(args.data)
    through = nflverse.completed_week(games, league.season)
    stats = nflverse.load_weekly_player_stats(league.season, args.data)
    projections = build_projections(stats, through_week=through, scoring=league.scoring)

    def points(entry: yahoo.YahooPlayer) -> float | None:
        player, _ = yahoo_state.resolve_player(entry, index)
        projection = projections.get(player.gsis_id) if player and player.gsis_id else None
        return projection.points if projection else None

    return points


def _gameday_check(
    args: argparse.Namespace,
    league: League,
    client: yahoo.Client | None,
    kickoff: gameday_mod.Kickoff,
    keys: list[str],
    all_kickoffs: list[gameday_mod.Kickoff],
    state: gameday_mod.State,
    games: list,
) -> int:
    record = args.send and not args.force
    try:
        if client is None:
            raise yahoo.YahooError("no Yahoo token or credentials. Run `python -m faab auth`.")
        league_key = yahoo_state.resolve_league_key(client, league)
        own = yahoo.own_team(yahoo.league_teams(client, league_key))
        roster = yahoo.team_roster(client, own.team_key, kickoff.week)
    except (yahoo.YahooError, yahoo_auth.AuthError) as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        if record and any(state.checks.get(key) == "failed_notified" for key in keys):
            return 1
        body = gameday_mod.render_failure(str(exc), kickoff, league.timezone)
        print(body)
        if args.send:
            try:
                notify.send(body, title="Lineup check failed", priority="high", tags="warning")
            except notify.NotifyError as send_exc:
                print(f"Delivery failed: {send_exc}", file=sys.stderr)
                return 2
            if record:
                for key in keys:
                    state.checks[key] = "failed_notified"
        return 1

    playing = gameday_mod.playing_teams(all_kickoffs, kickoff.week)
    known = gameday_mod.season_teams(all_kickoffs)
    prefix = f"{kickoff.week}:"
    alerted = {b[len(prefix):] for b in state.bye_alerts if b.startswith(prefix)}
    missing = gameday_mod.missing_starters(roster, league.starters)
    problems = gameday_mod.find_problems(
        roster, kickoff, playing, already_alerted_byes=alerted, known_teams=known
    )
    if problems:
        try:
            points = _points_for(args, league, games)
        except Exception as exc:  # noqa: BLE001 - ordering is optional, the alert is not
            print(f"replacements left unranked: {exc}", file=sys.stderr)
        else:
            problems = gameday_mod.find_problems(
                roster, kickoff, playing, points=points, already_alerted_byes=alerted,
                known_teams=known,
            )

    if not problems and not missing:
        print(f"week {kickoff.week} {kickoff.when.isoformat()}: every starter is clear")
        if record:
            for key in keys:
                state.checks[key] = "done"
        return 0

    body = gameday_mod.fit_to_ntfy(problems, kickoff, league.timezone, missing=missing)
    print(body)
    if args.send:
        urgent = bool(missing) or any(problem.urgent for problem in problems)
        try:
            notify.send(
                body,
                title=f"Lineup alert: {len(problems) + missing} starter(s)",
                priority="urgent" if urgent else "high",
                tags="rotating_light" if urgent else "warning",
            )
        except notify.NotifyError as exc:
            print(f"Delivery failed: {exc}", file=sys.stderr)
            return 2
        print("Sent to ntfy.")
    if record:
        for key in keys:
            state.checks[key] = "done"
        state.bye_alerts.extend(
            f"{prefix}{problem.player.player_key}"
            for problem in problems
            if problem.kind == "bye"
        )
    return 0


def _schedule_failure(
    args: argparse.Namespace, league: League, state: gameday_mod.State, now: datetime, problem: str
) -> None:
    """Push that no check can run, at most once a day.

    Without kickoff times no check is ever due, so the cron log would be the only trace.
    """
    key = f"schedule-{now.date().isoformat()}"
    print(f"no usable schedule: {problem}", file=sys.stderr)
    if not args.send or state.notices.get(key) == "failed_notified":
        return
    try:
        notify.send(
            gameday_mod.render_failure(f"no usable NFL schedule: {problem}", None, league.timezone),
            title="Lineup check failed",
            priority="high",
            tags="warning",
        )
    except notify.NotifyError as exc:
        print(f"Delivery failed: {exc}", file=sys.stderr)
        return
    state.notices[key] = "failed_notified"


def _gameday_kickoffs(
    args: argparse.Namespace, league: League, state: gameday_mod.State, now: datetime
) -> tuple[list, list[gameday_mod.Kickoff]] | None:
    """The schedule and this season's kickoffs, or None once the failure has been pushed.

    A fetch failure falls back to the cached copy of any age, since kickoff times rarely move
    within a week. A schedule with no kickoffs this season is as useless as none, because
    nothing in it can ever come due.
    """
    try:
        games = nflverse.load_games(args.data)
    except (FetchError, ValueError, OSError) as exc:
        try:
            games = nflverse.load_cached_games(args.data, SCHEDULE_CACHE_MAX_AGE_SECONDS)
        except (ValueError, OSError) as cache_exc:
            _schedule_failure(args, league, state, now, f"{exc}; {cache_exc}")
            return None
        print(f"schedule refresh failed, using the cached copy: {exc}", file=sys.stderr)
    kickoffs = gameday_mod.kickoffs(games, league.season)
    if not kickoffs:
        _schedule_failure(args, league, state, now, f"no {league.season} games in the schedule")
        return None
    return games, kickoffs


def cmd_gameday(args: argparse.Namespace) -> int:
    """Check this team's starters before each kickoff. Cron runs it every five minutes."""
    league = load_league(args.league)
    now = _parse_now(args.now)
    state_path = args.data / "gameday_state.json"
    with gameday_mod.single_run(args.data / "gameday.lock") as held:
        if not held:
            print("another game-day check is running")
            return 0
        state = gameday_mod.State.load(state_path)
        schedule = _gameday_kickoffs(args, league, state, now)
        if schedule is None:
            if args.send and not args.force:
                state.save(state_path, now)
            return 1
        games, all_kickoffs = schedule
        if args.force:
            upcoming = [k for k in all_kickoffs if k.when > now]
            pending = [(upcoming[0], [])] if upcoming else []
        else:
            pending = gameday_mod.due(all_kickoffs, now, state)
        if not pending:
            print("no check due")
            return 0
        client = _yahoo_client(args)
        status = 0
        for kickoff, keys in pending:
            try:
                status = max(
                    status,
                    _gameday_check(args, league, client, kickoff, keys, all_kickoffs, state, games),
                )
            finally:
                # Saved after every kickoff, so a later failure cannot discard an alert
                # that was already sent. A failed save repeats an alert rather than losing
                # one, which is the right way round for a lineup warning.
                if args.send and not args.force:
                    state.save(state_path, now)
        return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faab", description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_DIR, help="cache directory")
    parser.add_argument("--roster", type=Path, default=ROSTER_PATH)
    parser.add_argument("--league", type=Path, default=LEAGUE_PATH)

    sub = parser.add_subparsers(dest="command", required=True)

    lineup = sub.add_parser("lineup", help="start/sit report for the upcoming week")
    lineup.add_argument("--send", action="store_true", help="push to ntfy")
    lineup.add_argument("--week", type=int, default=0, help="override the week")
    lineup.add_argument("--through-week", type=int, default=0, help="override stats week")
    lineup.add_argument("--snapshot", action="store_true", help="save the report to data/")
    lineup.add_argument(
        "--if-local",
        default="",
        metavar="DAY:HOUR",
        help="exit quietly unless the league-local clock matches, e.g. sun:9",
    )
    lineup.add_argument("--source", choices=("auto", "yahoo", "paste"), default="auto")
    lineup.set_defaults(func=cmd_lineup)

    waivers = sub.add_parser("waivers", help="waiver claim sheet for the coming week")
    waivers.add_argument("--send", action="store_true", help="push to ntfy")
    waivers.add_argument("--week", type=int, default=0)
    waivers.add_argument("--through-week", type=int, default=0)
    waivers.add_argument(
        "--if-local",
        default="",
        metavar="DAY:HOUR",
        help="exit quietly unless the league-local clock matches, e.g. tue:20",
    )
    waivers.add_argument("--source", choices=("auto", "yahoo", "paste"), default="auto")
    waivers.set_defaults(func=cmd_waivers)

    roster = sub.add_parser("roster", help="show how each roster player resolved")
    roster.add_argument("--source", choices=("auto", "yahoo", "paste"), default="auto")
    roster.set_defaults(func=cmd_roster)

    gameday = sub.add_parser("gameday", help="alert on starters who will not play")
    gameday.add_argument("--send", action="store_true", help="push alerts to ntfy")
    gameday.add_argument(
        "--force", action="store_true", help="check the next kickoff now, recording nothing"
    )
    gameday.add_argument("--now", default="", help="pretend it is this ISO time, with offset")
    gameday.set_defaults(func=cmd_gameday)

    auth = sub.add_parser("auth", help="authorize with Yahoo, or refresh the token")
    auth.add_argument(
        "--code",
        default="",
        help="the redirected URL, or just the code, from the browser address bar",
    )
    auth.set_defaults(func=cmd_auth)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "if_local", ""):
            league = load_league(args.league)
            if not is_scheduled_now(args.if_local, league.timezone):
                return 0
        return args.func(args)
    except (
        ConfigError,
        FetchError,
        FileNotFoundError,
        ValueError,
        yahoo.YahooError,
        yahoo_auth.AuthError,
    ) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
