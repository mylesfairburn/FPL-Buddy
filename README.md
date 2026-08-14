# FPL Buddy

A Fantasy Premier League companion that rates every player with a trained
points model, picks two AI squads a week, and analyses the team you actually
own.

**Live at [fpl.mfhost.co.uk](https://fpl.mfhost.co.uk)** — no account needed,
just your FPL ID.

---

## What it does

**Player ratings.** Every player in the game scored by a gradient-boosted model
trained on completed-season data, one model per position. Ratings drive
predicted points for each of the next eight gameweeks, and each of the ~570
players gets [their own page](https://fpl.mfhost.co.uk/players/a-z) with their
underlying numbers written out in prose.

**Two AI squads, every gameweek.**

- *AI Manager* — a bot that plays the actual game. It carries a bank, takes
  transfers, eats hits when they're worth it, and plays chips. Its squad is
  frozen at each deadline and scored against reality afterwards.
- *Best XI* — the highest-scoring legal squad for the upcoming gameweek, solved
  as an integer linear program under the £100m budget and the three-per-club
  rule.

Both are committed **before** the deadline and never recomputed, so the
predicted-versus-actual record is honest. A gameweek that wasn't captured shows
as a gap rather than being reconstructed from later numbers.

**Your team.** Enter your FPL ID to load your squad: predicted points per
player, an optimised starting XI, transfer suggestions, chip advice, and live
scoring while matches are on — with provisional bonus points labelled as
provisional, because they move.

**Fixture rotation.** Pairs of clubs whose fixtures alternate, so one of them
always has a good game. Ranked over the next eight gameweeks on never-stuck
coverage rather than raw average difficulty.

## How it's built

| | |
|---|---|
| Backend | FastAPI, single uvicorn worker (the rated pool is held in memory) |
| Model | scikit-learn, one bundle per position, trained on full completed seasons |
| Optimiser | PuLP — an ILP per squad, cached per gameweek |
| Storage | SQLite in WAL mode, on a mounted volume |
| Frontend | Server-rendered Jinja2 templates, vanilla JS, no build step |
| Deploy | GitHub Actions → ghcr.io → watchtower, behind a Cloudflare tunnel |

Some deliberate choices worth calling out:

- **Player pages are keyed on FPL's `code`, not `id`.** FPL reassigns `id`
  every summer; `code` is stable for a player's career. Keying URLs on `id`
  would silently repoint every page at a different footballer each August.
- **Predicted points are frozen at write time and never recomputed.** If the
  model improves mid-season and old snapshots were re-derived from it, every
  "predicted X, scored Y" comparison would retroactively change and the track
  record would mean nothing.
- **Preseason stats are labelled as last season's.** FPL's bootstrap still
  serves last season's totals until the first deadline, so the pages ask the
  season clock before choosing a heading rather than captioning them with the
  current season and being wrong on every player at once.

## Running it locally

```bash
pip install -r python/requirements.txt
cd python && uvicorn main:app --reload
```

Then open http://127.0.0.1:8000. It reads `data/` directly and writes SQLite to
`state/`, so no configuration is needed for a local run.

Trained models are **not** in this repo — see
[Trained models](#trained-models) below.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FPL_DB_PATH` | `../state/fpl_companion.db` | SQLite location |
| `FPL_DATA_ROOT` | `../data` | Data root; point at a volume in production |
| `FPL_REFRESH_TOKEN` | *(unset)* | Gates `/api/refresh` and `/api/mode`. **Set this in production** — both trigger a full pipeline run |
| `FPL_SITE_URL` | `https://fpl.mfhost.co.uk` | Absolute origin for canonicals, Open Graph and the sitemap |
| `FPL_KOFI_HANDLE` | `mylesfairburn` | Ko-fi handle; unset hides the button entirely |

### Tests

```bash
python tests/run_tests.py
```

Around 520 cases covering routing, SEO, the JSON API contracts, and a security
pass — injection, XSS, traversal, auth, fuzzing and payload limits. Writes a
readable HTML report to `tests/reports/`, and exits non-zero if anything
critical or high fails. See [tests/README.md](tests/README.md).

### Trained models

`data/models/*.pkl` are kept out of this repo. They're the one part that can't
be regenerated from public data in an afternoon, and they live on the
deployment's data volume instead.

To produce your own:

```bash
cd python && python train_model.py
```

That trains from every season in `data/seasons/` from 2025-26 onward and writes
the bundles into `data/models/`.

## Deployment

See [deploy/README.md](deploy/README.md) — volumes, the cron jobs, and the
persistence traps that are easy to hit and silent when you do.

## Licence

Source-available, not open source. The code is published so it can be read,
learned from and audited; it is **not** licensed for commercial use or
redistribution. See [LICENSE](LICENSE) for the exact terms.

## Support

If FPL Buddy is useful to you, [buy me a coffee on
Ko-fi](https://ko-fi.com/mylesfairburn). It's free to use and has no ads.

---

Not affiliated with the Premier League or Fantasy Premier League. Player data
comes from the public FPL API.
