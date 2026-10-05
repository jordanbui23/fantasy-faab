"""League configuration.

Roster slots and scoring are not secret, so `league.toml` is committed. Anything that
identifies the league or its members (league id, team id, the roster itself) stays out
of it and lives in gitignored files or the environment.

`tomllib` is in the standard library from Python 3.11, so this costs no dependency.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from faab.model.project import Scoring

# Slots that accept more than one position. FLEX is Yahoo's W/R/T.
MULTI_POSITION_SLOTS: dict[str, tuple[str, ...]] = {
    "FLEX": ("RB", "WR", "TE"),
    "WRRB": ("RB", "WR"),
    "SUPERFLEX": ("QB", "RB", "WR", "TE"),
}

DEFAULT_SLOTS: dict[str, int] = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "FLEX": 1,
    "K": 1,
    "DEF": 1,
}


class ConfigError(ValueError):
    """Raised when league.toml is missing something the pipeline needs."""


def _read_scoring(raw: object, source: Callable[[str | None], str] = lambda key: "") -> Scoring:
    """Read the [scoring] table, falling back to the nflverse-equivalent defaults.

    `source` prefixes an error with the file that supplied a key, or the table for None.
    """
    if raw is None:
        return Scoring()
    if not isinstance(raw, dict):
        raise ConfigError(f"{source(None)}[scoring] must be a table")
    defaults = Scoring()
    values: dict[str, float] = {}
    for name in defaults.__dataclass_fields__:
        supplied = raw.get(name, getattr(defaults, name))
        if isinstance(supplied, bool) or not isinstance(supplied, (int, float)):
            raise ConfigError(f"{source(name)}scoring.{name} must be a number, got {supplied!r}")
        if not math.isfinite(supplied):
            # An infinite or undefined weight reaches every projection and then the
            # assignment weights, where it destroys the ordering without any error.
            raise ConfigError(f"{source(name)}scoring.{name} must be finite, got {supplied!r}")
        values[name] = float(supplied)
    unknown = set(raw) - set(defaults.__dataclass_fields__)
    if unknown:
        by_file: dict[str, list[str]] = {}
        for key in sorted(unknown):
            by_file.setdefault(source(key), []).append(key)
        raise ConfigError(
            "; ".join(
                f"{origin}unknown scoring keys: {', '.join(keys)}"
                for origin, keys in by_file.items()
            )
        )
    return Scoring(**values)


# Positions a waiver claim may be for. A team holding two starting quarterbacks has no use
# for a third, so an owner can narrow this in league.local.toml.
DEFAULT_CLAIM_POSITIONS = ("QB", "RB", "WR", "TE", "K")
KNOWN_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})


def _read_claim_positions(raw: object, origin: Path) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_CLAIM_POSITIONS
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{origin}: claim_positions must be a non-empty list")
    positions: list[str] = []
    for value in raw:
        position = str(value).strip().upper() if isinstance(value, str) else ""
        if position not in KNOWN_POSITIONS:
            raise ConfigError(f"{origin}: claim_positions has an unknown position {value!r}")
        if position not in positions:
            positions.append(position)
    return tuple(positions)


@dataclass(frozen=True)
class League:
    teams: int = 12
    faab_budget: int = 100
    slots: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SLOTS))
    season: int = 2026
    timezone: str = "America/New_York"
    own_team: str = ""
    scoring: Scoring = field(default_factory=Scoring)
    yahoo_league_id: int = 0
    claim_positions: tuple[str, ...] = DEFAULT_CLAIM_POSITIONS

    @property
    def starters(self) -> int:
        return sum(self.slots.values())

    def accepts(self, slot: str, position: str) -> bool:
        """Whether `position` may fill `slot`."""
        allowed = MULTI_POSITION_SLOTS.get(slot)
        if allowed is not None:
            return position in allowed
        return position == slot

    def slot_order(self) -> list[str]:
        """Every slot, expanded by its count, most constrained first.

        The ordering no longer decides correctness. `model.assign` solves the whole
        assignment at once, so a FLEX cannot take the only eligible back whatever order
        it sees. The order is kept because it is the order a lineup reads in.
        """
        single = [s for s in self.slots if s not in MULTI_POSITION_SLOTS]
        multi = [s for s in self.slots if s in MULTI_POSITION_SLOTS]
        expanded: list[str] = []
        for slot in single + multi:
            expanded.extend([slot] * self.slots[slot])
        return expanded


# A gitignored file beside league.toml whose keys override it. It exists for values that
# identify their owner, such as a team name, so the committed file can stay publishable.
LOCAL_OVERRIDE = "league.local.toml"


def _read_toml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc


def _overlay(base: dict, override: dict) -> dict:
    """Merge `override` over `base`, one table deep.

    A table in the override updates the matching table key by key, so overriding one
    scoring weight does not silently reset the others to their defaults.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _origin(path: Path, local: dict | None, key: str, sub: str | None = None) -> Path:
    """The file that supplied `key`, or `key.sub`, so an error names the file to fix."""
    table = local or {}
    supplied = key in table if sub is None else (
        isinstance(table.get(key), dict) and sub in table[key]
    )
    return path.with_name(LOCAL_OVERRIDE) if supplied else path


def load_league(path: Path) -> League:
    """Read league.toml, then `league.local.toml` beside it when present.

    Falls back to the documented defaults when neither exists.
    """
    base = _read_toml(path)
    local = _read_toml(path.with_name(LOCAL_OVERRIDE))
    if base is None and local is None:
        return League()
    raw = _overlay(base or {}, local or {})

    slots_raw = raw.get("slots", DEFAULT_SLOTS)
    if not isinstance(slots_raw, dict) or not slots_raw:
        raise ConfigError(f"{_origin(path, local, 'slots')}: [slots] must be a non-empty table")

    slots: dict[str, int] = {}
    for name, count in slots_raw.items():
        slot = str(name).upper()
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            origin = _origin(path, local, "slots", name)
            raise ConfigError(f"{origin}: slot {slot} must be a non-negative integer")
        if count:
            slots[slot] = count
    if not slots:
        raise ConfigError(f"{_origin(path, local, 'slots')}: every slot count is zero")

    teams = raw.get("teams", 12)
    budget = raw.get("faab_budget", 100)
    season = raw.get("season", 2026)
    yahoo_league_id = raw.get("yahoo_league_id", 0)
    for label, value, low in (
        ("teams", teams, 2),
        ("faab_budget", budget, 0),
        ("season", season, 1999),
        ("yahoo_league_id", yahoo_league_id, 0),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            origin = _origin(path, local, label)
            raise ConfigError(f"{origin}: {label} must be an integer of at least {low}")

    return League(
        teams=teams,
        faab_budget=budget,
        slots=slots,
        season=season,
        timezone=str(raw.get("timezone", "America/New_York")),
        own_team=str(raw.get("own_team", "")),
        yahoo_league_id=yahoo_league_id,
        claim_positions=_read_claim_positions(
            raw.get("claim_positions"), _origin(path, local, "claim_positions")
        ),
        scoring=_read_scoring(
            raw.get("scoring"), lambda key: f"{_origin(path, local, 'scoring', key)}: "
        ),
    )
