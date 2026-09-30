# fantasy-faab

Weekly FAAB waiver research for a Yahoo keeper league. Read `README.md` for the
architecture and `docs/RESEARCH.md` for what each platform API does and does not expose.

## Load-bearing rules

- **Auto-commit and auto-push.** After a coherent, validated change, stage only that change's
  files, commit, then push with `git -C <repo> push origin <branch>` after the pre-push file
  check in `~/projects/AGENTS.md`. Never force-push.
- **Adversarial review before calling a code change done.** Run `/xreview` on the diff of the
  unit of work, once per unit, not once per commit. A high or critical finding is a hard gate.
  Docs-only and config-only changes skip it.
- **Never commit league data or credentials.** `data/`, `.env`, `secrets/` and the OAuth token
  files are gitignored. Rosters, transaction logs and FAAB balances are other managers'
  information. Check `git status` before every commit.
- **Gitignore `.opencode/` and `.omo/`.** Already done; keep it that way.
- **Keep `README.md` current.** A new collector, a changed data flow, a new dependency or a
  changed architecture updates the README in the same change.

## Research discipline

`docs/RESEARCH.md` is the spec of record for what the APIs do. Every load-bearing claim there
carries `confirmed`, `supported` or `unverified`. Two rules that produced that format:

- **A tool's silence is not evidence of absence.** An empty result means "not found by this
  method", never "does not exist". Record it as `unverified` with the method named.
- **Re-check an absence claim before it drives a decision.** Yahoo changed its access model
  mid-2026 with no notice, and ESPN prunes historical seasons without warning. Anything about
  platform behavior decays.

Do not restate a finding from that doc into code comments. Point at the doc.

## Never write a bid

Yahoo's API is read-only as of 2026-07, so an automated claim is not possible. Do not design
around a future write path, and do not reach for a browser-session workaround to place a
claim. The output of this project is a recommendation a human reads and submits.

## Determinism boundary

Money math stays out of the language model. Budget accounting, roster-hole detection, bid
distributions and keeper equity are computed in `model/` and handed to the model as facts. The
model reasons about them and writes prose. A recommendation must be re-derivable from its
snapshot without re-running any model.

@../AGENTS.md
