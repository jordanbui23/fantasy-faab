# fantasy-faab

Weekly FAAB waiver research for a keeper fantasy football league on Yahoo. A cron wakes
collectors, a deterministic layer computes the parts that must not be guessed, and the tool
writes a ranked claim sheet with a dollar bid and a drop candidate per target. On game days
it checks your starters before every kickoff and pushes an alert when one will not play.

**The bot recommends. You submit the bid.** Yahoo removed write access from its Fantasy
Sports API, so no claim is ever placed automatically. See `docs/RESEARCH.md`.

## League shape

A ten-team keeper league on FAAB rather than rolling waiver priority, with a $100 budget. A
player drafted and held to the end of the season is keeper-eligible the next year at
his draft round minus one. A player added off waivers is NOT, and neither route is available
to him later that season; only a trade confers keeper eligibility on a player the owner did
not draft.

So a waiver claim is a rental for the rest of this season and nothing more. The objective has
one term, not two: rest-of-season points. There is no keeper equity to price into a bid, which
is why the model does not carry any and must not grow one.

## Why a rival model and not just projections

Everyone in the league can read the same projections. The edge is knowing what the other
managers will bid, which comes from three league-local signals:

1. **Remaining budget per team.** Yahoo exposes `faab_balance` for every team, not just yours.
2. **Roster holes per team.** A manager with no startable RB2 bids differently on a handcuff
   back than one who is set at the position.
3. **Bidding history per manager.** Yahoo's transaction log carries the winning bid on every
   completed claim, so aggression is measurable across a season.

Sleeper's national trending-adds feed adds a fourth, weaker signal: how hard the wider
fantasy public is chasing a player. It knows nothing about this league and must not stand in
for the three above.

## Status

Both scheduled reports and the game-day check run against live Yahoo data. Rosters,
budgets and the claimable pool come from the API, and the pasted files are only a fallback.
The rival-bidding model, which is the actual edge, is unblocked and not yet built.

| Component | State |
| --- | --- |
| `docs/RESEARCH.md` | done, findings carry confidence labels |
| `src/faab/cache.py` | working, atomic writes and a TTL disk cache |
| `src/faab/collectors/sleeper.py` | working, no auth needed |
| `src/faab/collectors/nflverse.py` | working, no auth needed |
| `src/faab/names.py` | working, name to `gsis_id` resolution |
| `src/faab/roster.py` | working, hand-typed roster file |
| `src/faab/paste.py` | working, reads text pasted from Yahoo's web pages |
| `src/faab/league.py` | working, slot rules from `league.toml` and a local override |
| `src/faab/model/usage.py` | working, recency-weighted usage per player |
| `src/faab/model/project.py` | working, projects points from volume |
| `src/faab/model/assign.py` | working, exact slot assignment |
| `src/faab/model/need.py` | working, prices each claim as an add/drop swap |
| `src/faab/model/lineup.py` | working, Sunday start/sit |
| `src/faab/model/waiver.py` | working as a stopgap, no rival model yet |
| `src/faab/report.py` | working, plain text under the notification limit |
| `src/faab/notify.py` | working, ntfy push |
| `src/faab/crontab.py` | working, tested crontab block editing |
| `bin/faab-cron` | working, hourly cron with an in-tool clock gate |
| Scheduling | working, topic held as a cron variable |
| `src/faab/collectors/yahoo_auth.py` | working, OAuth2 with a self-refreshing token |
| `src/faab/collectors/yahoo.py` | working, leagues, teams, rosters, statuses, winning bids |
| `src/faab/yahoo_state.py` | working, Yahoo's league view in the models' shapes |
| `src/faab/gameday.py` | working, pre-kickoff check for starters who will not play |
| Rival budget and roster-hole model | not built; every input is now readable |
| Keeper equity per candidate | not applicable, a waiver add is never keeper-eligible |

## Architecture

```
cron, hourly, on this box
  |
  +-- bin/faab-cron           installs the entries; the clock gate is in the tool
  |
  +-- faab/cli.py             lineup | waivers | gameday | roster | auth, --if-local gate
        |
        +-- faab/cache.py     atomic writes, TTL disk cache, truncation floors
        |
        +-- faab/collectors/  read-only fetch, one module per source
        |     nflverse.py     stats, snaps, injuries, depth charts, AND player ids
        |     sleeper.py      add/drop velocity, team defenses, injury status
        |     yahoo_auth.py   OAuth2 code exchange and refresh, read-only
        |     yahoo.py        rosters, statuses, FAAB balances, winning bids
        |
        +-- faab/names.py     typed name -> gsis_id, refuses to guess
        +-- faab/yahoo_state.py   Yahoo players resolved to gsis ids, worse status wins
        +-- faab/roster.py    the hand-typed roster file, the fallback without a token
        +-- faab/paste.py     pasted Yahoo pages: projections, and the fallback pool
        +-- faab/league.py    slot rules and budget, league.toml plus a local override
        |
        +-- faab/model/       deterministic, no language model
        |     usage.py        recency-weighted points, snap trend, opportunity share
        |     project.py      points for the coming week, from volume not from luck
        |     assign.py       exact slot assignment, Hungarian algorithm
        |     lineup.py       start/sit: fill slots, exclude bye and out, flag ties
        |     waiver.py       unpriced role changes, and a bid as a share of budget
        |     need.py         each claim as an add/drop swap, priced by the solver
        |     rival budgets, roster holes                               [blocked]
        |
        +-- faab/gameday.py   kickoff windows, starters who will not play, replacements
        +-- faab/report.py    plain text, trimmed to fit one notification
        +-- faab/notify.py    ntfy push
        +-- faab/crontab.py   every byte written into the crontab, unit tested
        |
        +-- data/             cached sources, snapshots, logs, all gitignored
```

No language model runs in either report today. Both are arithmetic the owner can check,
which is the point: a recommendation must be re-derivable from its snapshot. A model will
be added for prose once the Yahoo data gives it something the owner cannot see by eye.

Rankings come from a projection, not from points already scored. Those are different
questions, and the difference is touchdowns. Early in 2026, two quarterbacks threw for nearly
identical yardage, while one scored passing touchdowns well above the league rate and the
other well below it. Ranking on points scored preferred the first by a wide margin for what
was nearly identical play. So `model/project.py` projects volume, which carries
over to next week, and pulls per-opportunity rates toward the league rate by how much has
been seen, hardest for touchdowns.

The gain is calibration rather than a different answer. On the week it was built, both
metrics chose the same ten starters, and the projection simply sat far closer to Yahoo's
own numbers. Calibration, the in-sample caveat on its error figure, and the two known
errors that remain are in `docs/RESEARCH.md` section 7c.

Filling the slots is an exact assignment, not a sort. Taking the best player for each
slot in turn loses points whenever two slots accept different position sets: a review
found a case where it left a slot empty with an eligible player on the bench. The problem
is tiny, a dozen slots against two dozen players, so `model/assign.py` solves it exactly
and its tests check every answer against brute force.

Ranking a waiver claim and sizing its bid pull in opposite directions, and both are
right. An unnoticed player is the better find, so being unchased lifts him up the
ranking. A chased player costs more to win, so public demand raises his bid. Those are
two separate numbers rather than one.

The deterministic layer is deliberately separate. Arithmetic that decides a dollar amount
does not belong inside a language model, and keeping it outside means every recommendation
can be re-derived from the snapshot months later.

Snapshots are retained so the bot can be scored against what actually cleared waivers.

### Player ids, and why name matching is the bridge

nflverse's player directory is the id authority. It carries a `gsis_id` on all 13,956 of
its `ACT` rows, and `gsis_id` is what weekly stats, snap counts and depth charts key on.
Names are matched against that directory, so `src/faab/names.py` is load-bearing for the
whole pipeline and refuses to guess: an unmatched or ambiguous name is reported with its
line number rather than resolved to a best guess.

Sleeper is still required for two things nflverse does not have. Team defenses are not
players, so they are absent from nflverse entirely. Live injury status has no other free
source.

An earlier version of this file said Sleeper was the id bridge, on a record count taken
over its whole dump rather than over its active players. `docs/RESEARCH.md` section 6
records the corrected measurement and how the error survived a review.

### No pandas, and that is deliberate

The host this was built on has a glibc older than 2.28, so modern numpy and pyarrow wheels
do not apply and pip falls back to a source build that fails. `nfl_data_py` additionally pins
`pandas<2.0`, which has no Python 3.12 wheel. nflverse publishes CSV beside every parquet
file, so the collectors parse CSV with the standard library and the only runtime
dependency is `requests`. Details in `docs/RESEARCH.md`.

## Why the cron runs locally

Not GitHub Actions. A Yahoo refresh token and ESPN session cookies are long-lived
credentials for real accounts, and putting them in a third-party secret store
buys nothing here. The cron runs on a machine you already control.

## Setup

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q
```

The suite is offline by default. To exercise the live Sleeper and nflverse calls:

```sh
FAAB_LIVE_TESTS=1 .venv/bin/python -m pytest tests/ -q
```

Authorize Yahoo once with `python -m faab auth`, which prints a URL, then
`python -m faab auth --code '<redirected URL>'`. The app's credentials go in `.env` as
`YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET` and `YAHOO_REDIRECT_URI`. If your account has more
than one league, put `yahoo_league_id = <id>` in `league.local.toml`, which is gitignored
because it identifies you. Then `python -m faab roster` should list your Yahoo roster.

Without a token, three things stand in. Edit `league.toml` so the slots match the league,
and put your team name in `league.local.toml`. Write the roster, one player per line, into `data/roster.txt`; running any command
creates a template there. Export the ntfy topic, which is the credential, because anyone
holding it can read and publish to the topic:

```sh
export FAAB_NTFY_TOPIC=your-private-topic-name
```

Cron reads no shell profile, so `--install` copies that topic into the crontab as a cron
variable. It refuses rather than install entries that could never deliver, and it rejects
any topic outside letters, digits, underscore and hyphen, because a crontab variable runs
to end of line with no quoting.

Check that every name resolved, then read a report without sending it:

```sh
.venv/bin/python -m faab roster
.venv/bin/python -m faab lineup
.venv/bin/python -m faab waivers
```

Add `--send` to push one to the phone. Install the schedule once both look right:

```sh
bin/faab-cron --show
bin/faab-cron --install
```

## The game-day check

`faab gameday` exists for one outcome: a starter who will not take the field is reported
while he can still be moved. NFL teams must name their inactive players 90 minutes before
kickoff, and Yahoo locks each player at his own game's start. So for every kickoff time the
check runs twice inside that window: 75 minutes before, once the inactive lists should be
out, and 15 minutes before, for anything that changed late.

It reads your roster from Yahoo, takes every starter whose game is in that kickoff, and
pushes an urgent alert for anyone Yahoo marks out, inactive or on a reserve list, for a
starter whose team has a bye, and for an empty starting slot. Questionable, doubtful and
any unfamiliar status get a normal alert, and so does a team abbreviation the schedule does
not know. Each alert names the bench players who can fill the slot, healthy and not yet
locked, best projected first. An all-clear check sends nothing.

A failure is never quiet. If Yahoo cannot be read, or returns a roster for the wrong week,
the check pushes "the lineup check failed, check by hand" once and keeps retrying until
kickoff. If the schedule cannot be fetched and the cached copy is more than three days old,
it pushes the same warning once a day. If the push itself fails, the check stays due and the
next run tries again, so a failure can repeat an alert but never lose one. Cron runs it every five minutes, and a run
with nothing due reads only the cached schedule. `bin/faab-cron --run gameday` checks the
next kickoff immediately and records nothing, which is the way to test it.

What it cannot catch is a starter who is active, plays, and scores nothing. Before kickoff
he looks like every other active starter. `docs/RESEARCH.md` section 7h has the details.

## Scheduling, and why the clock gate is in the tool

The report goes out Sunday at 9am Eastern and the claim sheet Tuesday at 8pm Eastern.
Cron fires hourly in UTC, and `--if-local sun:9` decides whether to do any work. That
split exists because a server usually runs UTC while the schedule is Eastern, so a fixed UTC hour
would drift by one when daylight saving changes. An hourly wake plus a gate is correct
year round, and the gate is unit tested against both sides of the change.

### Why the topic is a cron variable and not a file

An earlier version kept the topic in a `.env` file that `bin/faab-cron` sourced. Sourcing
a file runs it, and that produced five review findings across two rounds: a stray `exit`,
`return`, `read` or `set -e` in the file changed the result, command substitution captured
anything the file printed, appending a good line could not repair a file that already
failed, and `chmod` cannot revoke a descriptor a reader already holds. Every finding came
from the same choice rather than from five separate bugs.

A cron variable removes the whole class. Nothing is executed, nothing is parsed, there is
no file to protect, and the value never enters the repo.

Editing the crontab is its own hazard, because a mistake destroys jobs this project did
not create. That logic therefore lives in `src/faab/crontab.py` rather than in the shell
script, where it has a test suite covering what must SURVIVE an edit: an unrelated job
whose command merely names this repo, and the owner's own `FAAB_NTFY_TOPIC` set for
something else. Both were deleted by an earlier substring filter. The module matches one
exact marked block, refuses to guess when the markers are unbalanced, and clears the topic
after the entries so a job appended later cannot inherit it. The shell only reads the
crontab, hands the text over, and writes the result back, and it aborts rather than write
when the read fails.

## Secrets

Nothing credential-bearing is committed. `secrets/`, `data/`, `.env` and the OAuth token
files are gitignored. The ntfy topic lives in the crontab rather than in any file in this
repo, so there is nothing here to protect and nothing to leak through a stale copy. Verify `git status` shows no league data before any commit; the
transaction log and roster data are other people's information as much as yours.
