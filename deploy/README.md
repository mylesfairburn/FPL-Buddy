# Deploying the DB + scheduled jobs (Proxmox VM)

Everything here runs on the **host**, not inside the app container. The app runs
a single uvicorn worker holding rated data in an in-memory `state` dict, so a
second worker (or a job that imported and mutated that state) would drift out of
sync with it. The jobs instead write to SQLite and then poke `/api/refresh` so
the live process reloads.

## 1. Persistent volumes

**Two** directories must live outside the image. The GitHub Actions → ghcr.io →
`docker pull` cycle replaces the image wholesale on every deploy, so anything
written inside it is destroyed each time you ship.

| Host path | Container path | Holds |
|---|---|---|
| `/srv/fpl-companion/state` | `/app/state` | SQLite — saved teams, AI snapshots, the deadline ledger |
| `/srv/fpl-companion/data` | `/app/data-live` | Everything the nightly job writes: `gameweek_stats.csv`, the bootstrap cache, per-player summaries |
| `/srv/fpl-companion/secrets` | `/app/secrets` (read-only) | The Google service-account JSON that `GSC_KEY` points at |

**These host paths still say `fpl-companion` after the rename to FPL Buddy, on
purpose.** They hold the live database and every file the nightly job has
written, and renaming a directory the app is mid-flight over is the one step
here that can lose data. The container, the image and the cron jobs are all
called `fpl-buddy`; only these paths lag, and nothing reads them but this file
and `docker-compose.yml`. If you do want to rename them later, do it on its own
rather than alongside anything else:

```bash
docker compose down                                  # not `stop` - release the mounts
mv /srv/fpl-companion /srv/fpl-buddy
sed -i 's#/srv/fpl-companion#/srv/fpl-buddy#g' docker-compose.yml
docker compose up -d
curl -s localhost:8000/api/ai/status | python3 -m json.tool   # storage must read `bind`
```

If that last check reports `anon`, stop and read *Recovering data from orphaned
anonymous volumes* below — the app will look healthy with an empty database.

The second one is easy to miss. Without it the app still runs, but every
nightly write lands in the image layer and is wiped on the next deploy — which
silently drops the app back to **preseason ratings** until 03:00 rebuilds them.

Note that neither is mounted at `/app/data`. That directory holds the CSVs
baked into the image; mounting over it would hide them. `FPL_DATA_ROOT` points
at the mount instead, and `ensure_seeded()` copies the image's copy across on
first boot — per-file and missing-only, so a nightly-updated file is never
reverted to the image's stale version.

```bash
mkdir -p /srv/fpl-companion/state /srv/fpl-companion/data
```

```bash
docker run -d --name fpl-buddy \
  -p 8000:8000 \
  -v /srv/fpl-companion/state:/app/state \
  -v /srv/fpl-companion/data:/app/data-live \
  -e FPL_DATA_ROOT=/app/data-live \
  -e FPL_REFRESH_TOKEN="$(cat /srv/fpl-companion/refresh_token)" \
  ghcr.io/mylesfairburn/fpl-buddy:latest
```

Or with compose — see `deploy/docker-compose.yml`, which sets both.

### Verifying it took effect

```bash
curl -s localhost:8000/api/ai/status | python -m json.tool
```

Check `db.storage` and `data.storage`. Both must read `bind` (or `volume`):

| Value | Meaning |
|---|---|
| `bind` | An explicit `-v host:container`. Correct — survives redeploys |
| `volume` | A **named** Docker volume. Also fine |
| `anon` | **This is the bug.** An anonymous volume, new on every `docker run` |
| `image` | No mount at all; writing into the image layer |
| `unknown` | Couldn't read `/proc/self/mountinfo` — check by hand |

`anon` is the one to watch for, and it is why this check exists rather than
just printing the path. The Dockerfile declares `VOLUME ["/app/state"]`, so if
the container is ever started **without** `-v`, Docker quietly creates a fresh
anonymous volume each time the container is recreated. The path looks right,
`journal_mode` is `wal`, the app reports healthy — and the database is empty,
with the previous deploy's data stranded in an orphaned volume.

`journal_mode` should be `wal`, and row counts should be non-zero once a
deadline has passed.

### Recovering data from orphaned anonymous volumes

If you have been running with `anon`, the old data still exists — one volume
per deploy, unreferenced but not deleted. **Do not run `docker volume prune`
until you have recovered it.**

```bash
# Newest first, with sizes. The big ones are your databases.
docker volume ls -qf dangling=true | while read v; do
  printf '%s  %s\n' \
    "$(docker run --rm -v "$v":/v alpine du -sh /v 2>/dev/null | cut -f1)" "$v"
done | sort -h
```

Inspect a candidate, then copy it into the real location:

```bash
docker run --rm -v <volume-id>:/v alpine ls -la /v
docker run --rm -v <volume-id>:/v -v /srv/fpl-companion/state:/out \
  alpine cp /v/fpl_companion.db /out/
```

Stop the container first, and take a copy of whatever is currently in
`/srv/fpl-companion/state` before overwriting it.

## 1a. Authenticating to a private GHCR package

The image is published to `ghcr.io/mylesfairburn/fpl-buddy` as a **private**
package, so the host has to log in before it can pull. Two things need those
credentials, and forgetting the second is the one that hurts.

### The token

Not your GitHub password — GHCR doesn't accept one. Create a **classic**
personal access token at *GitHub → Settings → Developer settings → Personal
access tokens → Tokens (classic)*, with **`read:packages` and nothing else**.
The server only ever pulls; a token that can also write or reach your repos is
a token that can do more damage than the job requires if the box is
compromised.

Fine-grained tokens are the usual recommendation elsewhere, but their package
support is patchy — classic is the reliable path for GHCR.

### Logging in

```bash
read -rs GHCR_TOKEN                                  # paste, then Enter
echo "$GHCR_TOKEN" | docker login ghcr.io -u mylesfairburn --password-stdin
unset GHCR_TOKEN
```

`read -rs` keeps the token off the screen, and `--password-stdin` keeps it out
of shell history and out of `ps` output, where `-p <token>` would put it for
any other user on the box to read.

Verify:

```bash
docker pull ghcr.io/mylesfairburn/fpl-buddy:latest
```

### Watchtower needs the same credentials

**This is the step that gets missed.** Watchtower polls the registry to decide
whether a newer image exists. Against a private package with no credentials it
gets a 401, treats it as "nothing new", and simply stops deploying — no error
you'd notice, just pushes that never reach the server. You find out weeks later
when a change you shipped isn't live.

Watchtower reads `/config.json` inside its own container, so mount the host's
Docker credentials at that path:

```yaml
  watchtower:
    image: containrrr/watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /root/.docker/config.json:/config.json:ro
```

Read-only: Watchtower has no reason to write to it, and that socket already
gives the container root-equivalent access to the host.

Check it took:

```bash
docker logs apps-watchtower-1 --tail 30
```

A successful poll names the image and reports whether it's up to date. A 401 or
"unauthorized" means the mount isn't right.

### Two things that will bite later

`~/.docker/config.json` stores the token **base64-encoded, not encrypted**.
That is obfuscation, not protection — anyone who can read the file has the
token. `chmod 600 /root/.docker/config.json`, and treat a host compromise as a
token compromise: revoke it on GitHub rather than assuming it's still private.

If you set an expiry on the token, **write the date down**. When it lapses,
deploys stop in exactly the silent way described above. GitHub emails a warning
first, which is easy to miss.

## 2. Lock down `/api/refresh`

It triggers a full pipeline run (about a minute of CPU) and now drives DB
writes, so it must not be anonymously reachable.

```bash
openssl rand -hex 32 > /srv/fpl-companion/refresh_token
chmod 600 /srv/fpl-companion/refresh_token
```

Pass it as `FPL_REFRESH_TOKEN` to both the container and the cron jobs. When the
variable is set, the endpoint requires a matching `X-Refresh-Token` header and
returns 403 otherwise. When it is unset (local dev) the endpoint stays open so
nothing breaks for a local run.

Better still, don't expose it at all: keep port 8000 bound to localhost and put
the reverse proxy in front, denying `/api/refresh` from outside.

## 2a. Visitor numbers

Set `FPL_CF_ANALYTICS_TOKEN` to the site token from **Cloudflare dashboard →
Analytics & Logs → Web Analytics**, choosing *add the JS snippet manually*
rather than automatic setup — automatic injects the same beacon at the edge, and
with both in place every page view is counted twice.

Blank, no beacon is served and the Content-Security-Policy stays entirely
`'self'`. That is the right setting for a staging stack, and it is why a local
run never appears in the numbers.

Three sources measure this site and they disagree on purpose:

| Source | Counts | Blind to |
|---|---|---|
| Search Console (`seo-report`) | Clicks from Google search | Direct, Bing, social, anything not Google |
| Cloudflare edge analytics | Requests at the proxy | Nothing — which is the problem: bots and scrapers are in there, and a whole office behind one NAT is one "unique" |
| Cloudflare Web Analytics | Page views a browser actually rendered | Anyone running an ad blocker; and one person on a phone and a laptop counts twice |

The third is the closest thing to "real people" and is still not a headcount —
no analytics product is. Read it for **trend and relative page popularity**,
not as an attendance register.

**This adds a third-party origin back to the CSP** (`static.cloudflareinsights.com`
for the script, `cloudflareinsights.com` for the beacon POST), which partially
reverses the decision to vendor Bootstrap. Both are added only while the token
is set. It also makes `/privacy` say the site runs analytics — if the beacon is
ever removed, that page has to be changed back.

## 3. Cron

Two jobs, because they answer to different clocks.

**Hourly deadline watcher.** Runs two phases per poll.

*Before* a deadline (inside the final ~100 minutes) it commits both AI squads.
The timing is the point: injuries and suspensions are confirmed in the day or
two before a deadline and FPL updates player status as it learns, so a squad
chosen early can be built around someone since ruled out. The window is wider
than the poll interval so a run always lands inside it; committing twice is a
no-op because the stored rows are the ledger.

*After* a deadline it captures real managers' picks, replaces their in-app
drafts with the official team, and backfills the AI squads only if the
pre-deadline window was missed (flagged `committed_after_deadline`).
 FPL deadlines are irregular — midweek rounds land
Tuesday/Wednesday evening, an early Saturday kickoff pulls the deadline to
11:00, international breaks skip weeks, and double/blank gameweeks compress or
drop rounds entirely. There is no daily slot that reliably lands just after one,
so this polls `deadline_time` hourly and acts when one has newly passed. It's
cheap and idempotent — a second run in the same hour is a no-op.

**Daily heavy refresh at 03:00.** Deliberately late rather than at midnight: a
Monday night game can finish around 22:00 and FPL's bonus-point finalisation
(the `data_checked` flag) often lags full time by an hour or more. A midnight
run risks freezing provisional scores that later change.

**Nightly rebuild at 03:15.** Rebuilds the current gameweek briefing and writes
that night's in-depth player write-up, sharing one rating run between them.
Must come before the IndexNow ping at 03:30, which reads the live sitemap.

**The roundup rides on `deadline-watch`, not on `daily-refresh`.** It used to be
nightly, on the reasoning that it waits for the same `data_checked` flag the
actual-points backfill does. True, but that flag flips at no fixed hour, and
once a day is not often enough for a page whose entire subject is what just
happened. The 2026-27 opener made the point: the last match ended Monday
evening, the flags were still down at 03:00 Tuesday, so the run found nothing
and the next attempt was 03:00 Wednesday — two days after the round ended and
two days before the next deadline. It now publishes within the hour. The
backfill runs immediately before it in both jobs, because the roundup's
scorecard prints a number only the backfill writes.

**One thing still rides on `daily-refresh`**: the nightly housekeeping — the
database backup and the disk tidy-up.

**The staleness check at 05:00, last.** It rode on `daily-refresh` until it was
noticed doing the one thing a staleness check must never do. At 03:00 it judged
five jobs, three of which run later that same night, so on any night the
heartbeat table was young — a fresh deployment, a restored backup, a new volume
— it reported `gameweek-report`, `indexnow` and `seo-report` as having never
completed successfully, and sent an alert saying so, an hour before all three
ran perfectly. It now has its own line an hour after the last nightly job. **If
you add a job after 05:00, move that line down.**

Install with:

```bash
cp deploy/fpl-buddy.cron /etc/cron.d/fpl-buddy
chmod 644 /etc/cron.d/fpl-buddy
```

Edit the paths and token at the top of that file first.

### Log rotation

The six jobs append to `/var/log/fpl-buddy-*.log` and nothing truncates them.
`deadline-watch` alone writes 24 entries a day, forever.

```bash
cp deploy/fpl-buddy.logrotate /etc/logrotate.d/fpl-buddy
chmod 644 /etc/logrotate.d/fpl-buddy
logrotate -d /etc/logrotate.d/fpl-buddy
```

Run that last line. Logrotate silently skips a config file whose permissions
are wrong, which looks exactly like it working.

## 3a. Knowing whether the jobs are still running

Everything above fails **silently**. The site keeps serving perfectly, just
with older and older content — nothing 500s, and the only symptom is a briefing
that stopped changing.

Every scheduled command records that it ran and whether it succeeded:

```bash
docker exec fpl-buddy python jobs.py status
```

That prints each job's last run, its last **success** (the figure that actually
matters — a job failing hourly has a very recent last run and has not worked
since yesterday), what is overdue, how many backups exist, and whether alerting
is configured. It exits non-zero when something is wrong, so it can be driven
from another monitor without parsing the output. The same data is on
`/api/ai/status` under `jobs`.

### Discord as the hub

Set `FPL_ALERT_WEBHOOK` in `.env` and everything below arrives in one channel.
Set the other three to split it up — each falls back to the alerts webhook when
unset, so this is one variable at a time rather than all-or-nothing.

| Variable | Channel carries |
|---|---|
| `FPL_ALERT_WEBHOOK` | Jobs that failed or stopped running |
| `FPL_DRAFTS_WEBHOOK` | The nightly player write-up, the briefing when it goes postable, the roundup when it lands |
| `FPL_GAMEWEEK_WEBHOOK` | Deadline reminders (~24h and ~2h) and what the AI Manager did |
| `FPL_SEO_WEBHOOK` | The weekly Search Console digest |
| `FPL_KOFI_WEBHOOK` | Donations, relayed by `POST /api/kofi` (needs `FPL_KOFI_TOKEN` too) |

Worth splitting: they want different notification settings on a phone. Alerts
should buzz at 3am; content should be waiting in the morning; SEO is a weekly
read. One channel carrying all four ends up muted.

**Every variable also has to be named in `docker-compose.yml`.** One that
`.env` sets and compose doesn't list never reaches the container, and the
failure is silent — the same trap documented against `GSC_PROPERTY` above.

Then check every channel actually lands where you think:

```bash
docker exec fpl-buddy python jobs.py status --test-alert
```

That sends one message per channel. Four messages arriving in four channels is
the only way to catch a webhook pointing at the wrong channel, which no amount
of re-reading the variable will show you.

Deploy notifications are separate, because they come from GitHub rather than
the server: set `DISCORD_DEPLOY_WEBHOOK` under Settings → Secrets and variables
→ Actions.

See [AUTOMATION.md](AUTOMATION.md) for the full audit of what is automated,
what is deliberately not, and what is still missing.

## 4. First run

```bash
docker exec fpl-buddy python jobs.py init-db
docker exec fpl-buddy python jobs.py deadline-watch
```

The first `deadline-watch` against a mid-season DB marks every already-passed
gameweek as `skipped` rather than reconstructing it. That's intentional: the
model's `next_gameweeks` predictions roll forward once fixtures finish, so a
squad built "for" a past gameweek would actually be built from a later
gameweek's numbers. A visible gap is better than a fabricated record.

To force a specific gameweek (testing, or catching up within the 24h window):

```bash
docker exec fpl-buddy python jobs.py deadline-watch --gameweek 5
```

## Data layout

`data/` is addressed through `python/seasons.py`, never by hardcoded path:

```
data/seasons/2025-26/    completed season - READ ONLY to the app (training data)
data/seasons/2026-27/    current season - the only directory ever written to
data/reference/          season-independent (positions, ClubElo)
data/models/             trained model bundles
```

Adding next season is creating `data/seasons/2027-28/` - no code change. The
trainer picks up every season from 2025-26 onward automatically, so rerunning
`python train_model.py` next August trains on two seasons instead of one.

`FPL_DATA_ROOT` overrides the root if you ever want data on a volume too.

### Retraining after a change to the model — READ BEFORE DEPLOYING ONE

A deploy does **not** ship a new model, and nothing warns you about it. Three
facts combine into that:

- `data/models/*.pkl` is gitignored, so a fresh checkout — and therefore the CI
  build context — has none.
- In production `FPL_DATA_ROOT=/app/data-live`, which is the mounted volume, so
  the bundles the app loads are the ones sitting on `/srv/fpl-companion/data/`.
- `seasons.ensure_seeded()` copies **only files that are missing**. It will never
  overwrite a bundle the volume already holds — which is correct, and is also
  why a new image cannot replace one.

So new inference code lands against whatever pickle was already there. That
combination used to be capable of producing confident, plausible, wrong numbers
on every page. It now can't: bundles carry a `MODEL_VERSION`, and
`rating_model.load_models()` refuses one it doesn't recognise rather than
scoring players off a feature list the model was never fitted on.

After deploying a change to `train_model.py`, retrain on the box:

```bash
docker exec fpl-buddy python train_model.py
```

then rebuild the rated pool so the site picks it up:

```bash
docker exec fpl-buddy python jobs.py daily-refresh
```

Training takes about five seconds and writes straight to the volume. Until you
run it, the app raises `StaleModelError` on boot with the same instruction —
loud, and on purpose.

### Nightly stats pull

`jobs.py daily-refresh` now pulls this season's per-gameweek player rows into
`data/seasons/<season>/gameweek_stats.csv`. That file is what `inseason` ratings
are built from - until it exists, the app serves preseason ratings off last
season's averages. It's ~one API call per player, hence nightly. Run it on its
own with `jobs.py refresh-stats`, or skip it in the daily run with
`--skip-stats`.

## What's stored

| Table | Holds |
|---|---|
| `manager_team` / `manager_team_picks` | One header + 15 picks per (manager, gameweek). `fpl_id 0` is reserved for the AI Manager bot. |
| `ai_team_snapshot` / `ai_team_snapshot_picks` | The AI Best XI optimum frozen at each deadline. |
| `ai_transfer_log` | Created for the AI Manager; nothing writes to it yet. |
| `processed_deadline` | Idempotency ledger for the hourly watcher. |
| `known_manager` | FPL ids someone has actually looked up — the snapshot job walks this, not all ~11M entries. |

Player master data (names, teams, costs, ratings) is **not** duplicated into
SQLite. Rows carry `element_id` and join against the existing pandas/CSV
pipeline at read time, so there's one source of truth for who a player is.

`predicted_points` is frozen at write time and never recomputed. If the model
improves mid-season and old snapshots were re-derived from it, every
"predicted X, actually scored Y" comparison would retroactively change and the
track record would be meaningless.
