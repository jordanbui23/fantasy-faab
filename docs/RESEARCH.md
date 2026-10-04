# Research: platform APIs and data sources

Findings as of 2026-09-23. Every load-bearing claim carries a confidence label: `confirmed` (2+ independent sources plus a
manual check), `supported` (one good source), `unverified` (a tool's silence, or one
weak source).

Re-check any absence claim before it drives a decision.

---

## 1. Yahoo Fantasy Sports API

The league lives here, so this is the one that matters.

### Access is gated by manual approval, and is read-only

**`confirmed`.** Yahoo replaced self-serve app creation with an application form and began
enforcing it in waves from 2026-07-22.

Sources:

1. Yahoo's own access page, read live 2026-09-23 (`https://sports.yahoo.com/developer/access/`):
   "The Yahoo Fantasy Sports API currently provides read access only. Write access is not
   available at this time." It also warns that "incomplete or insufficiently detailed
   submissions cannot be evaluated and will be closed without further correspondence".
2. [yfpy issue #84](https://github.com/uberfastman/yfpy/issues/84), multiple independent
   reporters. The create-app form at `developer.yahoo.com/apps/create/` offers only
   OpenID Connect and TW Auction, so a new app cannot request the Fantasy Sports scope at
   all. Apps created under the old flow lost access at the cutover.
3. [r/fantasyfootballcoding PSA, 2026-05-19](https://www.reddit.com/r/fantasyfootballcoding/comments/1thaux8/psa_yahoo_fantasy_football_devs_changes_have_been/).

Reported turnaround in that thread is one to three weeks. Several approved apps still
return `403 "This application is not authorized to perform this action"` afterwards, which
the thread reads as approvals never being bound to the issued consumer key.

**Consequence for this project: the bot recommends, the human submits the bid.** No
automated FAAB claim. Library methods that still advertise it
(`yahoo_fantasy_api.Team.claim_player(faab=...)`, `yahoofantasy` write ops) cannot get the
scope.

### Applying

- Form: `https://sports.yahoo.com/developer/access/`.
- State personal, single-league use explicitly. The form asks for it.
- **Corrected 2026-09-23: the app must be NEW.** An earlier version of this bullet, sourced
  from the yahoo-fantasy-mcp README, said to reuse an existing app's App ID. Yahoo's own
  approval email says the opposite: "Existing apps won't pick up the permission, so it has
  to be a new one." Section 7f has the full sequence.
- Redirect URI must be `https://`. `https://localhost:8000` was accepted on 2026-09-23 and
  the code exchange then succeeded (`confirmed` by observation), so a domain you control is
  not required. The browser cannot reach localhost, which is expected: the authorization
  code is in the address bar.
- Diagnostic that separates the gate from a credentials bug: a `refresh_token` grant against
  `api.login.yahoo.com/oauth2/get_token` returns 200 while `/fantasy/v2/game/nfl` returns
  403. That is approval, not OAuth.

### Fields that matter, assuming approval

| Endpoint | Field | Why it matters |
| --- | --- | --- |
| `league/{key}/teams` | `faab_balance` | Every rival's remaining budget, live |
| `league/{key}/transactions` | winning bid amount | Per-manager bid ledger across the season |
| `league/{key}/draftresults` | round, pick | Baseline for the +1-round keeper cost |
| `league/{key}/settings` | `uses_faab`, `waiver_time`, roster slots, scoring | League shape |
| `league/{key}/players;status=FA` / `;status=W` | pool, `percent_owned` | Candidate set |
| `player/{key}/percent_owned`, `/draft_analysis` | ownership trend | Demand proxy |

`faab_balance` appears on opponent teams, not only your own. **`supported`**: it is present
on every `<team>` inside the standings and matchups samples in Yahoo's official docs and in
the [YFAR API guide](https://macraesdirtysocks.github.io/YFAR/articles/Yahoo_API_Guide.html).
Verify against the real league once access exists.

League key format is `{game_key}.l.{league_id}`, for example `461.l.1000`.

### Losing bids are the gap

**`supported`.** The API returns successful transactions only. A
[2022 r/fantasyfootballcoding thread](https://www.reddit.com/r/fantasyfootballcoding/comments/vst2v0/getting_unsuccessful_faab_bids_from_yahoo_api/)
reports getting winning bid amounts but no losing ones, and speculates they are simply not
recorded in the API. Yahoo's own commissioner help text says bids stay hidden until a player
clears, then the winning bid shows in the transaction log, with multi-bid players listed
under a "FAAB Bids" tab.

That tab is web-only. It is the single richest signal for modeling this league, because it
shows who was outbid and by how much. Getting it means reading the logged-in Yahoo page,
which is fragile and sits against the spirit of the API terms. Deferred, not rejected.
Recorded here so the tradeoff is not rediscovered.

Do not assume `;status=unsuccessful` works. That was a guess in the thread above, never
confirmed. **`unverified`.**

---

## 2. ESPN

Unofficial and undocumented. Cookie authentication, no write path.

- Base URL is `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/...`. The older
  `fantasy.espn.com` host stopped working around 2024-04. **`confirmed`**
  ([espn-api issue #539](https://github.com/cwendt94/espn-api/issues/539), plus an
  independent writeup at stmorse.github.io).
- Private leagues need the `espn_s2` and `SWID` cookies, copied by hand from a logged-in
  browser. This cannot be automated; ESPN added a captcha to the login path. **`confirmed`**
  (espn-api discussion #150, ffscrapr ESPN vignette).
- Maintained Python wrapper: `cwendt94/espn-api`.
- ESPN prunes historical seasons without notice. Archive any JSON you fetch. **`supported`**
  (espn-api issue #650).

Lower priority. The league is on Yahoo.

---

## 3. Sleeper

Free, unauthenticated, read-only. No approval of any kind.

- Docs: `https://docs.sleeper.com/`. "No API Token is necessary, as you cannot modify
  contents via this API." **`confirmed`** (read live).
- Asks callers to stay under 1,000 requests per minute. **`supported`**.
- `GET /v1/players/nfl/trending/add?lookback_hours=48&limit=100` and the `drop` equivalent.
  This is national add velocity, not league-local, but it moves before a Tuesday waiver run
  and is the best free proxy for who the field is chasing.
- `GET /v1/players/nfl` is a full player dump with cross-platform IDs, useful for joining
  Yahoo IDs to nflverse IDs.

The trending endpoint is a **crowd** signal. It does not know your league. Do not let it
stand in for the rival-budget model.

---

## 4. nflverse

Free, no auth.

### Read the CSV releases directly, not through `nfl_data_py`

**`confirmed`**, measured on the development host 2026-09-23.

`pip install nfl-data-py` fails. Version 0.3.3 pins `pandas<2.0`, pip resolves that to
pandas 1.5.3, which ships no cp312 wheel, so it builds from source and dies. The wider
problem is the host. `ldd --version` reports a glibc older than 2.28, so numpy 2.5.3 and
pyarrow have no usable wheel either and fall back to a source build that fails.

nflverse publishes a `.csv` and a `.csv.gz` alongside every `.parquet`, and recommends
linking to release URLs for direct access. The standard library reads those with no
dependency at all. Verified with `curl -sIL -o /dev/null -w '%{http_code}'` against
each release used here, all 200:

| Release | File |
| --- | --- |
| `stats_player` | `stats_player_week_{season}.csv.gz` |
| `snap_counts` | `snap_counts_{season}.csv.gz` |
| `injuries` | `injuries_{season}.csv` |
| `depth_charts` | `depth_charts_{season}.csv` |
| `players` | `players.csv` |

Schedules come from a different repo: `github.com/nflverse/nfldata/raw/master/data/games.csv`.

Note `player_stats/player_stats_{season}.parquet` is 404. The release tag was renamed
to `stats_player` with a `stats_player_week_` prefix, so an older tutorial's URL fails.

### Columns that carry the signal

`stats_player_week_2026.csv.gz` has 150 columns and, for week 2 of 2026, 2,225 rows.
The ones that matter for a waiver decision: `target_share`, `air_yards_share`, `wopr`,
`targets`, `carries`, `receiving_air_yards`, `rushing_epa`, `receiving_epa`,
`fantasy_points_ppr`. `snap_counts_2026.csv.gz` has 2,994 rows and carries
`offense_snaps` and `offense_pct`.

A blank cell means the player did not record the stat, and `float("")` raises, so every
numeric read goes through `nflverse.as_float`.

### Update cadence

From [nflverse's data schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html):
play-by-play and derived player stats nightly after each game day, with raw JSON usually
inside 15 minutes of a game ending; snap counts at 0, 6, 12 and 18 UTC subject to Pro
Football Reference; schedules every 5 minutes in season. **`supported`.**

A Tuesday morning run therefore has Monday night in it.

---

## 5. Projections and expert rankings

### The free FantasyPros mirror is stale and cannot be used

**`confirmed`**, measured 2026-09-23. This closes the "does `ffverse rankings` remove the
need for a FantasyPros key" question, and the answer is no.

`github.com/dynastyprocess/data/raw/master/files/db_fpecr.csv.gz` downloads, and its
1,528,918 rows carry the right shape, including `ecr_type` values for weekly (`wo`, `wp`,
`wsf`) and rest-of-season (`ro`, `rp`, `rsf`) rankings. But its newest `scrape_date` is
**2025-08-08**, more than a year old. The mirror stopped updating. Do not build on it for
rankings.

It remains useful for one thing: it carries a `yahoo_id` column, so it is a fallback
Yahoo id bridge. Sleeper is the better one, see below.

### FantasyPros direct

`GET https://api.fantasypros.com/public/v2/json/nfl/{season}/consensus-rankings` with
`type=WW` for waiver wire and `type=ROS` for rest of season, and
`.../projections?week=N&scoring=HALF`. `x-api-key` header. Free tier is
non-production personal use, premium comes with a paid HOF subscription. **`supported`**
([support article, 2026-08-05](https://support.fantasypros.com/hc/en-us/articles/49749297704475-How-do-I-request-access-to-the-FantasyPros-API)).
The free tier silently returns fewer players than it claims exist, so check for
truncation. **`supported`.**

### Yahoo's own projections are probably the better source

Once Yahoo access is approved, the player resource returns projected points computed in
**this league's own scoring settings**, which a generic PPR or half-PPR projection cannot
match. The team resource shows `team_projected_points` in the docs samples, and the
league `players` sub-resource takes league scoring into account. **`unverified`** at the
per-player weekly level; check it on the first authenticated call before paying anyone.

---

## 6. Player id joins

Yahoo, nflverse and Sleeper each use their own player ids.

**Corrected 2026-09-23.** An earlier version of this section said Sleeper was the id
bridge, on the strength of "6,750 of its 12,228 records carry a `yahoo_id`". That count
was taken over the WHOLE dump, including retired and free-agent records, and the active
subset is far worse. The claim survived a review and was only caught when the pipeline
produced `NO GSIS ID` for eleven of thirteen players on a test roster. This is the
classic population error: a ratio whose denominator is not the population you will actually
query.

Measured over the records that have BOTH a position and a team, which is the population
a roster lookup draws from:

| Source | Records | With `gsis_id` | With `yahoo_id` |
| --- | --- | --- | --- |
| Sleeper, position and team present | 2,743 | 566 | 757 |
| nflverse `players.csv`, status `ACT` | 13,956 | 13,956 | not carried |

So **nflverse's `players.csv` is the id authority**, and name matching is the bridge.
Every one of the 1,306 player ids in the 2026 weekly stats is present in it, and the
snap-count join lands 1,597 of 1,600 through its `pfr_id`. `gsis_id` is the hub that
weekly stats, snap counts and depth charts all key on.

Sleeper is still required, for two things nflverse does not have:

- **Team defenses.** A defense is not a player, so it is absent from nflverse entirely.
  Sleeper carries 32 records with `position: "DEF"`, `player_id` set to the team
  abbreviation, and the nickname in `last_name`.
- **Live injury status.** Sleeper's `injury_status` field is the only free source here.

Yahoo will join by name too, since it returns display names. That means the name
normalizer is load-bearing for the whole pipeline, which is why it refuses to guess.

Note one collision the matcher must survive: Josh Allen is both the `ACT` Buffalo
quarterback and a `DEV` Tampa Bay center in the same directory. Narrowing by position,
then team, then preferring the single active player resolves it without a hint.

---

## 7. Prior art

Not to copy wholesale, but worth reading before building:

- `derekrbreese/fantasy-football-mcp-public`: Yahoo MCP server, lineup optimizer, waiver
  tools. Closest to this project's shape.
- `michaelfromyeg/fantasy-sports-toolkit`: portable skills with a swappable data provider.
  Its `waiver-wire` skill assigns a FAAB bid and a drop candidate per claim, which is the
  same output shape wanted here.
- `jaymishra-source/fantasy-football-ai-comanager`: ESPN plus GitHub Actions plus Discord.
  Demonstrates the cron-to-Discord loop. Note it puts ESPN cookies in GitHub Secrets, which
  this project deliberately avoids by running the cron locally.
- Jake Moses's agent-only league (`jake-moses.com/ai-league.html`): twelve models managing
  teams. Useful for the session model, specifically waking an agent on an event rather than
  running it continuously, and for loop guards and spend accounting.

---

## 7b. Yahoo per-player projections exist, and past production is a poor substitute

**`supported`** (seen on a week 3 roster page, 2026-09-23). Yahoo shows a
`Proj Pts` column per player, in league scoring. That was an open question in section 8
and it is now answered for the web UI. Whether the gated API returns the same field is
still **`unverified`**.

This matters more than it looks. The reports currently rank on
`Usage.weighted_recent_points`, which is points ALREADY SCORED weighted toward recent
weeks. Compared against Yahoo's projections on a real roster, that metric
agreed on two contested lineup decisions, a third receiver and a tight end, and disagreed on
two, the quarterback and the flex.

The pattern is that recent production works where a role changed and fails where two good
players are separated by matchup and talent rather than by usage. Quarterback is the worst
case, because both options play every snap so a snap-share signal says nothing.

So a projection is the better ranking input, and the report's disclaimer is not a fix for
using the weaker one. Section 8 tracks the decision about where to get one.

## 7c. Calibrating the projection, and what it still gets wrong

The projection separates volume from efficiency and pulls each player's per-opportunity
rates toward the league rate, hardest for touchdowns. Prior strengths were chosen two ways
that agreed.

Treating touchdowns as Beta-Binomial, the prior strength matching the real spread between
players is p(1-p)/variance. At a 0.03 rushing touchdown rate with a between-player spread
near 0.01, that is about 290 carries.

Measured against Yahoo's projections for 13 players on one real roster, mean absolute
error fell monotonically as the touchdown prior rose and flattened in the hundreds:

| Touchdown prior | 75 | 150 | 250 | 400 | 600 |
| --- | --- | --- | --- | --- | --- |
| Mean absolute error | 2.14 | 2.03 | 1.96 | 1.92 | 1.91 |

A yardage prior of 25 beat every heavier value. 400 was chosen for touchdowns because it
takes nearly all the gain without sitting at the edge of the searched range.

**`unverified` as accuracy, `supported` only as an order of magnitude.** The error above is
IN-SAMPLE: the same 13 players chose the prior and then scored it, so the figure measures
fit and not prediction. It must not be quoted as how close the projection lands. Re-check it
against a week these priors never saw before any decision rests on the number. Reproduce the grid by varying `YARDS_PRIOR` and `TOUCHDOWN_PRIOR` in
`faab.model.project` and scoring against a saved copy of Yahoo's projection column.

Scoring settings were also settled empirically rather than assumed. Full point per
reception with 4-point passing touchdowns gave a mean absolute error of 2.14 against
Yahoo, against 2.90 at half a point and 3.77 at zero, and 6-point passing touchdowns were
worse at every reception value. That matches the defaults in `Scoring`, which independently
reproduce nflverse's published `fantasy_points_ppr` exactly.

Replacing points already scored with this projection took in-sample mean absolute error
from 3.20 to 1.92.

**It changed no lineup decision in week 3.** Both metrics pick the same ten starters. The
gain is calibration, not a different answer: the old metric sat several points above Yahoo
on a lead running back and on a quarterback, and the projection puts both within a point.
That matters for reading a close call and for sizing a waiver bid, and it is worth having on
those grounds alone.

An earlier version of this section claimed the projection corrected the flex. That was
false and worth recording. The measurement came from a run against stale bytecode in which
the recency weights had been flattened to (1,1,1) by a mutation test, and the restore left
the compiled file in place because the mutation preserved the source's byte length and
landed in the same second. Under correct weights the projection prefers the committee back at
flex, the same as the old metric and still against Yahoo. Mutation runs now set
`PYTHONDONTWRITEBYTECODE=1`.

### The one error that still changes a lineup

The model has no prior-season data, so it knows a player only from the current season. That
is why it can prefer a high-volume passer to a dual-threat quarterback whom Yahoo ranks
several points higher. Passing volume over two games was the whole gap. Yahoo presumably
weights a rushing role that earlier seasons show and two games do not.

The fix is a better prior, not a bigger fudge. Blending last season's own rates in as the
prior mean, instead of the league mean, would carry that information without hardcoding an
opinion about any player. nflverse publishes the earlier seasons already.

A second known error does not currently change a lineup. A pass-catching back in a
three-way committee projected at nearly double Yahoo's figure, because his recent targets
overstate his forward role. Volume is backward looking, so a role about to shrink is
projected as though it will hold.

## 7d. Keeper eligibility, and why it keeps the bid model simple

**`confirmed`** against the league's rules, 2026-09-23. A drafted player held to the end of the
season is keeper-eligible the next year at his draft round minus one. A player added off
waivers is not, at any point that season. Only a trade confers eligibility on a player the
owner did not draft.

Every waiver claim is therefore a rental. A bid buys rest-of-season points and no future
asset, so the objective stays one term and the model carries no keeper equity. That closes
the largest planned feature in section 8 by making it inapplicable rather than by building
it.

One consequence is unpriced and worth stating. `suggested_bid` sizes a bid from role
strength and contention, and it does not know how many weeks remain. Because an add is a
rental, the same player is worth less in week 13 than in week 3 simply because fewer games
follow. Left unmodelled deliberately: a weeks-remaining term would change every dollar
figure, and it should not be added on reasoning alone while bid outcomes are unobservable
without the Yahoo API.

## 7e. Scoring predictions out of sample

Section 7c's error figure is in-sample and cannot be quoted as accuracy. The honest test is a
week the priors never saw: record both models' projections before kickoff, then compare each
against the player's actual `fantasy_points_ppr` once the stats land.

Two kinds of contested call decide whether either model earns anything. A committee back
whose recent targets put this model far above Yahoo tests whether the volume
signal reads a real role or two noisy games; if Yahoo wins, the model needs a depth-chart
term. A high-volume passer preferred over a dual-threat quarterback that Yahoo ranks higher
tests whether the missing prior-season data costs real points.

Score a week by re-projecting with `through_week` set to the week before, once its stats
land. This model's side needs no stored prediction, because the leakage fix means a
projection built with a week 3 cutoff cannot see week 3. Yahoo's side does. Its projection
cannot be recomputed from public data, so it has to be recorded before the games. The week 3
record exists, but it names the players on a real roster, so it is kept outside this repo.

## 7f. Approval is per app, and the console showing Read is not proof of it

**`confirmed` 2026-09-23 by observation.** A Yahoo developer app whose API Permissions page
displays "Fantasy Sports - Read" can still be refused by every Fantasy endpoint.

What was observed. The authorization-code flow completes normally and returns a working
bearer token, and that token refreshes successfully with HTTP 200. Every Fantasy Sports
request then fails with HTTP 403 and the body `"This application is not authorized to perform
this action."` Two independently obtained tokens behaved identically, and the second was
issued after the app's permissions were edited and saved, so token age is not the cause.

The decisive detail is which endpoint fails. `/fantasy/v2/game/nfl` carries no private user
data and needs no user grant, and it is refused exactly like `users;use_login=1`. A missing
USER consent cannot explain that, so the block is on the APPLICATION.

Two things this rules out for anyone debugging the same symptom. It is not the scope
parameter: Yahoo's Fantasy permissions are set on the app rather than requested per
authorization, and the token response carries no `scope` field at all, only `access_token`,
`refresh_token`, `expires_in` and `token_type`. And it is not the redirect URI, which is
validated during the code exchange and would fail there rather than at the API.

This is now **`confirmed`** by Yahoo's own approval email, which sets out three steps and
states that access is turned on only when all three are complete. Approval alone grants
nothing. A DocuSign API Access and Use Agreement must be signed, a NEW app must be created
with the Fantasy Sports permission, and that app's Client ID must be submitted on a
Developer Application Confirmation Form.

The email is explicit that the app must be new: "Existing apps won't pick up the
permission, so it has to be a new one." So the permission is bound to an app that Yahoo
enables by hand, which is exactly what the 403 reports. Creating a correctly configured app
and authorizing against it produces a working, refreshable token that every Fantasy
endpoint still refuses until Yahoo flips the switch.

The practical consequence is that a 403 here is a WAITING state, not a bug, and no amount of
re-authorizing will clear it. Confirm the DocuSign is signed and the Client ID was submitted
on the confirmation form rather than on the original request form, whose acknowledgement
email looks similar and quotes a 1 to 2 week review.

Distinguishing a 403 from a 401 matters here and is worth keeping. Yahoo returns 401 for a
bad or expired token and 403 for an application that is not permitted, so the status alone
separates "re-authorize" from "the app is wrong" without any further calls.

## 7g. Access is live, and what the API actually returns

**`confirmed` 2026-10-04 by observation.** Yahoo's "your access is live" email arrived ten
days after the confirmation form. The token issued during the 403 period then worked
unchanged after one silent refresh, so no re-authorization was needed. The API host is
`fantasysports.yahooapis.com`; `fantasy.yahooapis.com` does not resolve.

Every field section 1 assumed is present for this league, read with `format=json`:

| Request | What came back |
| --- | --- |
| `league/{key}/settings` | `uses_faab`, waiver type and timing, weeks, trade deadline |
| `league/{key}/teams` | `faab_balance` and `waiver_priority` on every team, not only your own |
| `league/{key}/transactions` | `faab_bid` on completed claims, including $0 claims |
| `league/{key}/teams/roster;week=N` | every rostered player with `selected_position` |
| `league/{key}/players;status=A` | the free-agent pool, paged |
| `team/{key}/matchups;weeks=N` | `team_projected_points` for each side |

So the three league-local signals in the README are all readable, and the pasted budgets
and available-players files can be replaced.

**Per-player projections were not found.** `players/stats;type=week;week=N` returned
`player_points` for each player and no projected field, on both a roster and a free-agent
request. That is one method, so the result is **`unverified`** as an absence. Yahoo's web
page shows a per-player projection (section 7b), and the matchup carries a team-level one,
so the data exists somewhere. Until it is found the model's own projection stays the input,
and the conservative minimum against Yahoo can only use pasted figures.

The JSON shape is unusual and worth knowing before writing a parser. Collections are
objects keyed `"0"`, `"1"` and so on, plus a `"count"` key, rather than arrays. A resource's
fields are split across a list of single-key objects. A parser should walk for named keys
rather than index fixed positions.

## 8. Open questions

- ~~**Keeper cost for an undrafted waiver add.**~~ **Closed 2026-09-23, `confirmed`
  against the league's rules.** A player added off waivers is never keeper-eligible. Only a drafted player
  held to the end of the season, or a player acquired by trade, can be kept. A waiver claim
  is therefore a rental for the remainder of the current season, worth exactly its
  rest-of-season points, and the bid model must not carry a keeper term. This closes the
  question rather than answering it: the feature it would have fed does not exist.
- ~~**Does the real league expose opponent `faab_balance`?**~~ **Closed 2026-10-04,
  `confirmed`.** Every team carries it. See section 7g.
- ~~**Are FAAB bid amounts present on completed transactions for this league?**~~ **Closed
  2026-10-04, `confirmed`.** `faab_bid` is on completed claims. Losing bids remain absent.
- **Does Yahoo return per-player weekly projected points in league scoring?** Still open.
  The weekly stats request returns points scored and no projection (section 7g, one method,
  `unverified`). Check other requests before paying FantasyPros.

## 9. Environment constraints

Recorded because they will bite again, and because they are the reason the collectors use
the standard library rather than the usual data stack. Measured on the development host
2026-09-23.

- The system `python3` is older than 3.12 and end of life. Use a separately installed
  Python 3.12.
- `ldd --version` reports a glibc older than 2.28. Modern numpy, pandas and pyarrow wheels
  target `manylinux_2_28`, so pip falls back to a source build and fails.
- Consequence: **no pandas, no numpy, no pyarrow, no `nfl_data_py`.** Parse CSV with the
  `csv` module. If a future component genuinely needs a dataframe, that is a decision to
  bring to the owner, not to solve by fighting the toolchain.

