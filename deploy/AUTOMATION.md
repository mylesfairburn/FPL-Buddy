# What runs itself, and what still needs you

An audit of everything this deployment does without being asked, what was
missing, and what is deliberately left manual. Written for the case this
project is actually in: one person maintaining it, with no second pair of eyes
and no team to notice when something quietly stops.

The organising question throughout is not "can this be automated" — most things
can — but **"what currently fails silently?"** A job that breaks loudly is an
inconvenience. A job that stops running and looks identical to a quiet week is
the thing that costs a month.

---

## The jobs, as they stand

| Job | When | What breaks if it stops |
|---|---|---|
| `deadline-watch` | hourly, :05 | AI squads never get committed; briefings never freeze or go postable; real teams never captured |
| `daily-refresh` | 03:00 | Ratings go stale; actual scores never backfilled; no roundups; no backups |
| `gameweek-report` | 03:15 | The briefing stops updating; no nightly player write-up |
| `indexnow` | 03:30 | New pages take longer to be indexed |
| `seo-report` | 04:00 | No Search Console / Bing history accumulates |

Three of those five have the property that makes them dangerous: the site keeps
serving perfectly, just with older and older content. Nothing 500s. Nothing
appears in a log anyone reads.

---

## Added

### 1. A heartbeat, and an alert — `ops.py`, `job_run` table

Every scheduled command now records that it started and how it finished. The
important query is not "did it run" but **"when did it last SUCCEED"** — a job
failing hourly has a very recent last run and has not worked since yesterday.

- `python jobs.py status` prints every job, its last run, its last success, and
  what is overdue. Exits non-zero when something is wrong, so it is usable from
  another monitor without parsing the output.
- `/api/ai/status` carries the same thing under `jobs`, so it can be checked
  from a phone.
- If `FPL_ALERT_WEBHOOK` is set, a failed or overdue job pushes a message to
  Discord, Slack or ntfy. **This is the single highest-value item in this
  document.** Everything else on the site waits to be asked; this is the only
  part that comes and finds you.

### 1a. Discord as the hub — five channels

Alerts turned out to be the least interesting thing to push. Five channels now,
each with its own variable, each falling back to `FPL_ALERT_WEBHOOK` when unset
— so one variable gets you everything in one place, and splitting them out
later is one variable at a time rather than all-or-nothing.

| Channel | Variable | Carries |
|---|---|---|
| `alerts` | `FPL_ALERT_WEBHOOK` | Jobs that failed or stopped running |
| `drafts` | `FPL_DRAFTS_WEBHOOK` | The nightly player write-up, the briefing when it goes postable, the roundup when it lands |
| `gameweek` | `FPL_GAMEWEEK_WEBHOOK` | Deadline reminders at ~24h and ~2h, and what the AI Manager did with its squad |
| `seo` | `FPL_SEO_WEBHOOK` | The weekly Search Console digest |
| `kofi` | `FPL_KOFI_WEBHOOK` | Donations, relayed by `POST /api/kofi` |

A sixth, `DISCORD_DEPLOY_WEBHOOK`, lives in GitHub's repo secrets rather than
`.env` — the workflow runs on GitHub's machines and never sees the server.

**The Ko-fi one is a relay, not a passthrough.** Ko-fi can POST to any URL but
cannot post to Discord directly, so `/api/kofi` translates the payload — and
that translation is where three problems get fixed. Ko-fi includes the donor's
**email** in every payload, which has no reason to be in a chat channel given
what the privacy policy promises. The donor's name and message are free text
typed by a stranger, so markdown is defanged and newline floods collapsed. And
the endpoint is publicly reachable by necessity — Ko-fi's servers cannot
authenticate — so it verifies Ko-fi's `verification_token` in constant time,
caps the body at 16KB, deduplicates retries through the `notification` table,
and does not exist at all unless `FPL_KOFI_TOKEN` is set.

That last point generalised into a fix worth having anyway: every Discord
message now sends `allowed_mentions: {"parse": []}`. A webhook can ping
`@everyone` by default, and a stranger naming themselves `@everyone` on a
public donation form would otherwise notify the whole server for free.

**`drafts` is the one that changes the routine.** The nightly player write-up
was a feature that ended with "remember to open a URL every morning". Pushed to
a channel it's a notification you read, copy and post — which is the difference
between it being used daily and being used twice.

Splitting them matters because they want different notification settings on a
phone: `alerts` should buzz at 3am, `drafts` should be waiting in the morning,
`seo` is a weekly read. One channel carrying all four ends up muted, which is
the same as having none.

Every message is deduplicated through the `notification` table. This is not
optional: each one fires from a **window** rather than an instant — "the
deadline is about a day away" is four hours wide and checked hourly — so
without a ledger a single reminder arrives four times. The windows are
deliberately wider than the poll interval, which is what makes a missed run
harmless, so deduplication cannot come from narrowing them. The claim is taken
*before* the send, so a webhook outage costs one message rather than repeating
it every hour until the window closes; `db.clear_notification` resends by hand.

Long-form drafts stay behind the phone URL. Discord caps a message at 2000
characters and a Reddit draft is routinely longer, so pushing the lot would
truncate an argument mid-sentence — the exact failure the drafts were rewritten
to stop doing. Each push carries the Discord-length version plus a link.

Test the whole thing with:

```bash
docker exec fpl-buddy python jobs.py status --test-alert
```

That sends one message per configured channel. Four messages landing in four
channels is the only way to catch the thing most likely to be wrong: a webhook
pointing at the wrong channel, which no amount of checking the variable reveals.

A row still reading `running` hours later means the job died hard — killed, out
of memory, box rebooted — which is worth being able to tell apart from a clean
failure.

The staleness grace is deliberately generous (2× the expected interval). Every
job here is idempotent and several have windows wider than their interval, so a
single missed run genuinely costs nothing, and an alert that fires on a run five
minutes late is an alert that gets muted — after which none of this exists.

### 2. Backups — `ops.backup_database()`

The SQLite file holds the entire published archive, every frozen prediction and
the whole predicted-versus-actual track record. It existed in exactly one place.

A dated copy is now written nightly to `state/backups/`, keeping 14 days. It
uses SQLite's own backup API rather than copying the file, which matters
specifically here: the database runs in WAL mode with the cron writer and the
web reader on it at once, and a filesystem copy can catch a write in progress
and produce a file that opens fine and is missing the last transaction.

**Worth being honest about the limit.** These sit on the same volume as the
database. They protect against corruption, a bad migration and a mistaken
`DELETE`. They do not protect against losing the volume. Off-box copies are a
host-level job — `restic`, a Backblaze bucket, whatever the Proxmox host
already does for its other guests — and pretending a sibling directory is one
would be worse than not having it. **This is the biggest remaining gap.**

### 3. Tests gate the deploy — `.github/workflows/deploy.yaml`

The image published on every push to `main` regardless of whether anything
worked. The ~830-case suite ran only when someone remembered to. A broken route
could reach the live site before anyone knew there was one.

There is now a `test` job that `build` depends on. It installs the pinned
dependencies, trains the models from the in-repo `data/seasons/2025-26/` (about
five seconds — the bundles are gitignored, so a fresh checkout has none), runs
the suite, and uploads the HTML report as an artifact even on failure.

A third `notify` job posts the outcome to Discord — success, tests-failed, or
built-but-broken — and runs on `if: always()` so a **failed** run reports too,
which is the run you actually want to hear about. Set
`DISCORD_DEPLOY_WEBHOOK` under Settings → Secrets and variables → Actions;
unset, the step is skipped and nothing fails.

Training in CI is a real check as well as a fix: if a data or code change ever
breaks training, it breaks here rather than the next time the models are
rebuilt by hand months later.

The gate is `run_tests.py`'s own rule — non-zero on any **critical** or **high**
failure. Medium and below are reported and do not block. That is deliberate:
the suite carries a handful of standing medium findings about third-party CDN
assets, and a gate that is always red is a gate everyone learns to ignore.

### 4. Log rotation — `deploy/fpl-buddy.logrotate`

`deadline-watch` alone writes 24 entries a day, forever, and nothing ever
truncated `/var/log/fpl-buddy-*.log`. On a Proxmox host, a full disk takes down
considerably more than this site. Weekly, eight kept, compressed.

Not installed by anything — copy it to `/etc/logrotate.d/` and check it with
`logrotate -d`, because logrotate silently skips a config file whose
permissions are wrong, which looks exactly like it working.

### 5. Dependency updates — `.github/dependabot.yml`

Monthly PRs for pip, Actions and the base image. Worth having *because* this is
a one-person project: nobody else is watching for a CVE in `fastapi` or
`requests`. Every PR now runs the full suite before it can be merged, so an
update that breaks the pipeline says so rather than being taken on trust.

Monthly rather than weekly — eleven direct dependencies at a weekly cadence
produces a stream of PRs that get rubber-stamped, which is worse than reading a
handful properly once a month.

### 6. Disk housekeeping — `ops.prune_old_element_summaries()`, `retention.py`

Four things accumulated and none were ever removed:

- `data/seasons/<old>/element_summaries/` — ~700 JSON files per finished season.
  The current season's cache is the offline fallback for the whole pipeline and
  is never touched; a finished season's is dead weight, because the
  per-gameweek CSV derived from it is what training reads.
- `job_run` rows — pruned to 60 days.
- Superseded post drafts and nightly player write-ups — see `retention.py`.

---

## Left manual, on purpose

**Posting to social media.** The drafts are generated; posting is not. Automated
submissions to Reddit get accounts banned, and a banned account is a worse
outcome than a week with no post. More to the point, "is this week's page
actually worth posting" is a judgement only a person can make, and the whole
value of the drafts is that they make acting on that judgement cheap.

**Retraining the model.** It takes five seconds and could run nightly. It
should not. Predictions are frozen at each deadline precisely so the published
track record means something; silently swapping the model underneath that
record every night would make "predicted 62.4, scored 71" a claim about a model
that no longer exists. Retraining is a decision, and it belongs to a person who
then looks at the MAE before and after.

**Anything that writes to a frozen row.** Every published briefing and roundup
refuses to be overwritten. That refusal is the only thing making the archive
honest, and no amount of convenience is worth an escape hatch that a stray job
can find.

---

## What I would do next, in order

1. **Off-box backups.** The one genuinely unmitigated risk. Everything else
   here degrades; this one loses the archive. A nightly `restic` push of
   `state/` to any remote is an afternoon.
2. **An uptime check from outside the network.** Everything above notices that
   a job stopped. Nothing notices that the Cloudflare tunnel is down or the
   container is wedged, because the thing that would report it is inside the
   thing that is broken. Any free external pinger against `/api/ai/status` with
   a match on `"healthy": true` covers this.
3. **A staging target.** Right now `main` is production. The CI gate makes that
   much safer than it was, but a suite is not a smoke test on real data.

---

## Commands worth knowing

```bash
docker exec fpl-buddy python jobs.py status
```

```bash
docker exec fpl-buddy python jobs.py purge --dry-run
```

```bash
docker exec fpl-buddy python jobs.py gameweek-roundup --gameweek 12 --replace
```

```bash
docker exec fpl-buddy python jobs.py player-spotlight --replace
```
