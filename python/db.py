"""SQLite persistence for FPL Companion.

Scope, deliberately narrow: this DB holds ONLY the time-series state that
doesn't exist anywhere else - which squad a manager (or the AI) had in a given
gameweek, and what it was predicted/actually scored. Player master data (names,
teams, cost, ratings) is NOT duplicated here. Rows carry `element_id` and are
joined against the existing pandas/CSV pipeline at read time, so there's one
source of truth for who a player is.

Two processes touch this file: the live web app (reads) and the cron jobs
(writes). WAL mode is therefore not optional - without it a writer blocks every
reader for the duration of its transaction.

The file itself must live OUTSIDE the Docker image. The GH Actions -> ghcr.io
-> docker pull cycle replaces the image wholesale on every deploy, so an
in-image DB would be silently wiped each time. FPL_DB_PATH points at a mounted
volume in production; the default is repo-root ./state/ for local runs.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

# Relative to python/, which is the working directory both locally (uvicorn)
# and in the container (Dockerfile WORKDIR) - same convention as ../data/.
DEFAULT_DB_PATH = "../state/fpl_companion.db"

# The AI Manager bot is modelled as a manager with a reserved id, so it can
# reuse manager_team/manager_team_picks verbatim instead of needing a parallel
# schema. Real FPL entry ids start at 1, so 0 can never collide.
AI_MANAGER_FPL_ID = 0

SCHEMA = """
-- One row per (manager, gameweek). fpl_id 0 is the AI Manager bot.
-- Surrogate `id` exists purely so picks reference one integer instead of
-- repeating (fpl_id, gameweek) on all 15 child rows; UNIQUE(fpl_id, gameweek)
-- is the real identity and what lookups go through.
CREATE TABLE IF NOT EXISTS manager_team (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fpl_id           INTEGER NOT NULL,
    gameweek         INTEGER NOT NULL,
    points           INTEGER,
    predicted_points REAL,
    bank             REAL,
    value            REAL,
    active_chip      TEXT,
    captured_at      TEXT NOT NULL,
    UNIQUE (fpl_id, gameweek)
);

CREATE TABLE IF NOT EXISTS manager_team_picks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    manager_team_id  INTEGER NOT NULL REFERENCES manager_team(id) ON DELETE CASCADE,
    element_id       INTEGER NOT NULL,
    position         INTEGER NOT NULL,          -- 1-11 start, 12-15 bench
    is_captain       INTEGER NOT NULL DEFAULT 0,
    is_vice_captain  INTEGER NOT NULL DEFAULT 0,
    multiplier       INTEGER NOT NULL DEFAULT 1,
    cost             REAL,                      -- frozen at capture time
    predicted_points REAL,                      -- frozen at capture time
    actual_points    INTEGER,
    UNIQUE (manager_team_id, position)
);
CREATE INDEX IF NOT EXISTS idx_picks_team ON manager_team_picks (manager_team_id);

-- AI Best-XV: a fresh budget-constrained optimum per gameweek. No bank, no
-- transfers, no carry-over - which is exactly why it doesn't live in
-- manager_team alongside things that do.
CREATE TABLE IF NOT EXISTS ai_team_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    gameweek         INTEGER NOT NULL,
    formation        TEXT NOT NULL,
    budget           REAL NOT NULL,
    squad_cost       REAL NOT NULL,
    predicted_points REAL NOT NULL,             -- frozen; never recomputed
    actual_points    INTEGER,                   -- backfilled once the GW finishes
    generated_at     TEXT NOT NULL,
    UNIQUE (gameweek)
);

CREATE TABLE IF NOT EXISTS ai_team_snapshot_picks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id      INTEGER NOT NULL REFERENCES ai_team_snapshot(id) ON DELETE CASCADE,
    element_id       INTEGER NOT NULL,
    position         INTEGER NOT NULL,          -- 1-11 start, 12-15 bench
    is_captain       INTEGER NOT NULL DEFAULT 0,
    is_vice_captain  INTEGER NOT NULL DEFAULT 0,
    cost             REAL NOT NULL,
    predicted_points REAL NOT NULL,
    actual_points    INTEGER,
    UNIQUE (snapshot_id, position)
);
CREATE INDEX IF NOT EXISTS idx_ai_picks_snapshot ON ai_team_snapshot_picks (snapshot_id);

-- What the AI Manager changed each week and why. Created now so the bot can be
-- dropped in later without a migration; nothing writes to it yet.
CREATE TABLE IF NOT EXISTS ai_transfer_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gameweek        INTEGER NOT NULL,
    kind            TEXT NOT NULL,              -- 'transfer' | 'chip'
    out_element_id  INTEGER,
    in_element_id   INTEGER,
    chip            TEXT,
    cost_hit        INTEGER NOT NULL DEFAULT 0,
    rationale       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_transfer_gw ON ai_transfer_log (gameweek);

-- Idempotency ledger for the hourly deadline watcher. Without this, a job that
-- runs every hour would redo the same gameweek's snapshotting 168 times a week.
CREATE TABLE IF NOT EXISTS processed_deadline (
    gameweek      INTEGER PRIMARY KEY,
    deadline_time TEXT NOT NULL,
    status        TEXT NOT NULL,                -- 'processed' | 'skipped'
    detail        TEXT,
    processed_at  TEXT NOT NULL
);

-- FPL ids we've actually seen looked up. Snapshotting runs over THIS list, not
-- all ~11M managers.
CREATE TABLE IF NOT EXISTS known_manager (
    fpl_id     INTEGER PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- The squad a manager is CURRENTLY working on in this app, for the upcoming
-- gameweek. One row per manager: this replaces the old per-device localStorage
-- draft, so the same FPL id sees the same team from any device.
--
-- Lifecycle: the user edits it freely until the gameweek deadline; when that
-- deadline passes the watcher overwrites it with what they actually picked in
-- the official game and re-points it at the next gameweek. `source` records
-- which of those two a row is, so the UI can say whether it's showing your
-- own draft or your real team.
CREATE TABLE IF NOT EXISTS manager_draft (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fpl_id      INTEGER NOT NULL UNIQUE,
    gameweek    INTEGER,
    bank        REAL,
    source      TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'official'
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_draft_picks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id        INTEGER NOT NULL REFERENCES manager_draft(id) ON DELETE CASCADE,
    element_id      INTEGER NOT NULL,
    position        INTEGER NOT NULL,           -- 1-11 start, 12-15 bench
    is_captain      INTEGER NOT NULL DEFAULT 0,
    is_vice_captain INTEGER NOT NULL DEFAULT 0,
    cost            REAL,
    UNIQUE (draft_id, position)
);
CREATE INDEX IF NOT EXISTS idx_draft_picks ON manager_draft_picks (draft_id);

-- One published gameweek report per gameweek: the newspaper page at
-- /gameweek/<n>.
--
-- Stored as a JSON payload rather than as normalised rows, which is a
-- deliberate exception to the rule the other tables follow. Three reasons.
-- The report is heterogeneous - form picks, differentials, fixture runs and
-- injury news have almost no columns in common, so a relational shape is four
-- more tables carrying one page. Nothing ever queries inside it: the only read
-- is "give me the whole of gameweek 17". And it must render identically
-- forever, which means the player names and numbers have to be frozen INTO the
-- row rather than joined out of a pipeline that has moved on since - a page
-- dated GW17 that quietly restates itself with GW30 prices is not an archive,
-- it's a lie.
--
-- That last point is why this duplicates player names the roadmap says not to
-- duplicate. The rule exists so live views don't drift out of sync with the
-- pipeline; a frozen edition has the opposite requirement.
--
-- `frozen` is the lifecycle flag. 0 = regenerated nightly, still moving with
-- the injury news. 1 = the deadline has passed, the edition is final and
-- nothing writes to it again. The same pre-deadline window that commits the AI
-- squads flips this, so the page and the squads are honest about the same
-- moment in time.
CREATE TABLE IF NOT EXISTS gw_report (
    gameweek      INTEGER PRIMARY KEY,
    payload       TEXT NOT NULL,               -- JSON: the whole edition
    deadline_time TEXT,
    frozen        INTEGER NOT NULL DEFAULT 0,
    generated_at  TEXT NOT NULL,
    frozen_at     TEXT
);
"""


# How long manager data is kept. UK GDPR requires a defined retention period
# rather than "forever", and the privacy page quotes this constant directly, so
# changing it here changes what the site promises. Two seasons is enough for
# year-on-year comparison, which is the only reason to keep it at all.
RETENTION_MONTHS = 24


def db_path():
    return os.environ.get("FPL_DB_PATH", DEFAULT_DB_PATH)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


# Anonymous Docker volumes are named with a 64-character hex id. A named volume
# or a bind mount is anything else.
_ANON_VOLUME = re.compile(r"/volumes/([0-9a-f]{64})/_data")


def storage_kind(path=None):
    """Where the DB file actually lives, from the container's point of view.

    This exists because the failure it detects is silent. The Dockerfile
    declares VOLUME ["/app/state"], so if the container is started without an
    explicit -v, Docker creates a *fresh anonymous volume* every time the
    container is recreated. The app boots, builds its schema and reports
    perfectly healthy - on an empty database, with the previous deploy's data
    stranded in an orphaned volume. "Is it a mount point" cannot tell these
    apart, because the anonymous volume is a mount point too.

    /proc/self/mountinfo carries the host-side path for each mount, which does
    distinguish them:

        bind    an explicit -v /srv/...:/app/state — survives redeploys
        volume  a NAMED docker volume — also survives redeploys
        anon    an anonymous volume — new on every `docker run`, THE BUG
        image   no mount at all; the file is a layer in the image itself

    Linux-only, and best-effort: any failure reports "unknown" rather than
    raising, because this is a diagnostic and must never be the reason a health
    endpoint goes down.
    """
    # Accepts either a file (the DB) or a directory (the data root).
    target = os.path.abspath(path or db_path())
    if not os.path.isdir(target):
        target = os.path.dirname(target)
    try:
        if not os.path.ismount(target):
            return "image"
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                # 4th and 5th fields are the root within the source filesystem
                # and the mount point inside this namespace.
                if len(fields) < 5 or fields[4] != target:
                    continue
                source = fields[3]
                if _ANON_VOLUME.search(source):
                    return "anon"
                return "volume" if "/volumes/" in source else "bind"
        return "unknown"
    except (OSError, IndexError):
        return "unknown"


# One table whose presence stands in for "the schema is applied". Checked per
# connection rather than latched per process - see connect().
# Every table SCHEMA creates, read out of SCHEMA itself rather than listed by
# hand. This used to be a single sentinel table, which worked exactly once: the
# check passed as soon as that one table existed, so a schema that grew a NEW
# table never applied it to a database created before the change. The symptom
# is "no such table" on a deployment that has been running fine for months and
# has just been updated - the one case where it is most expensive to debug.
#
# Every statement is CREATE TABLE IF NOT EXISTS, so re-running the whole script
# on an existing file is free and idempotent. That makes adding a table a
# one-line change here with no migration to remember.
_SCHEMA_TABLES = tuple(re.findall(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)", SCHEMA))


def _schema_present(conn):
    """Is the schema actually there, right now, on this file?

    SQLite caches a connection's schema in memory, so this is a microsecond
    lookup rather than a disk read - cheap enough to do on every connection,
    which is what makes the check trustworthy instead of a guess.

    Checks for EVERY table, not one of them. A file missing any single table
    needs the script re-run, and running it when it wasn't needed costs
    nothing.
    """
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return all(t in have for t in _SCHEMA_TABLES)


def _open(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_schema(conn):
    conn.execute("PRAGMA journal_mode = WAL")
    # Default FULL fsyncs on every commit; NORMAL is the usual pairing with WAL
    # and is durable against process crashes (only host power-loss can lose the
    # last transactions, which for snapshot data is recoverable by re-running
    # the job).
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def connect():
    """One connection per unit of work, committed on clean exit.

    Deliberately not a long-lived shared connection: FastAPI runs sync endpoints
    in a threadpool and sqlite3 connections aren't safe to share across threads.
    Opening a connection is microseconds, so pooling buys nothing at this size.
    `timeout` is the busy-wait when the cron writer holds the write lock.

    The schema is verified on EVERY connection, not remembered from the first
    one. A per-process flag looks like an obvious optimisation and is a trap:
    it makes the app assume a file it may no longer be talking to. Anything
    that swaps the database out underneath a running process - a redeploy that
    recreates the volume, a restore from backup, someone deleting the file -
    then produces "no such table" on every request until a human restarts it.
    Re-checking costs a memory lookup and removes that failure mode entirely.
    """
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = _open(path)
    try:
        if not _schema_present(conn):
            _apply_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the schema if absent and put the file in WAL mode.

    Safe to call repeatedly: every statement is CREATE ... IF NOT EXISTS, and
    journal_mode persists in the file header."""
    with connect():
        pass
    return db_path()


def healthcheck():
    """Path, WAL state and row counts - surfaced by /api/ai/status so a failed
    volume mount is visible instead of silently writing to a fresh empty file."""
    try:
        with connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in tables if not t.startswith("sqlite_")}
        storage = storage_kind()
        return {"available": True, "path": os.path.abspath(db_path()),
                "storage": storage, "persisted": storage in ("bind", "volume"),
                "journal_mode": mode, "counts": counts}
    except sqlite3.Error as e:
        return {"available": False, "path": os.path.abspath(db_path()),
                "storage": storage_kind(), "detail": str(e)}


# ---- known managers -------------------------------------------------------

def record_known_manager(fpl_id):
    """Remember an FPL id someone actually looked up, so the snapshot job has a
    finite, real list to walk."""
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO known_manager (fpl_id, first_seen, last_seen)
               VALUES (?, ?, ?)
               ON CONFLICT(fpl_id) DO UPDATE SET last_seen = excluded.last_seen""",
            (int(fpl_id), now, now))


def known_managers():
    with connect() as conn:
        return [r["fpl_id"] for r in conn.execute(
            "SELECT fpl_id FROM known_manager ORDER BY fpl_id")]


# ---- deadline ledger ------------------------------------------------------

def deadline_is_processed(gameweek):
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM processed_deadline WHERE gameweek = ?", (int(gameweek),)
        ).fetchone() is not None


def mark_deadline_processed(gameweek, deadline_time, status="processed", detail=None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO processed_deadline
                   (gameweek, deadline_time, status, detail, processed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(gameweek) DO UPDATE SET
                   status = excluded.status,
                   detail = excluded.detail,
                   processed_at = excluded.processed_at""",
            (int(gameweek), deadline_time, status, detail, utcnow()))


def processed_deadlines():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM processed_deadline ORDER BY gameweek")]


# ---- gameweek reports -----------------------------------------------------

def save_gw_report(gameweek, payload, deadline_time=None):
    """Write (or rewrite) a gameweek's edition.

    Refuses to touch a frozen row. That refusal is the whole guarantee the
    archive rests on: once a deadline has passed, the only way to change what
    GW17 said is to go into SQLite by hand, which is a decision someone has to
    make deliberately rather than something a stray nightly run can do.

    Returns True if it wrote, False if the edition was already final."""
    import json
    with connect() as conn:
        row = conn.execute("SELECT frozen FROM gw_report WHERE gameweek = ?",
                           (int(gameweek),)).fetchone()
        if row and row["frozen"]:
            return False
        conn.execute(
            """INSERT INTO gw_report
                   (gameweek, payload, deadline_time, frozen, generated_at)
               VALUES (?, ?, ?, 0, ?)
               ON CONFLICT(gameweek) DO UPDATE SET
                   payload = excluded.payload,
                   deadline_time = excluded.deadline_time,
                   generated_at = excluded.generated_at""",
            (int(gameweek), json.dumps(payload, separators=(",", ":")),
             deadline_time, utcnow()))
        return True


def freeze_gw_report(gameweek):
    """Mark an edition final. Idempotent - the hourly watcher may call this
    more than once in the window before a deadline, and calling it on an
    already-frozen row must not move `frozen_at`."""
    with connect() as conn:
        cur = conn.execute(
            """UPDATE gw_report SET frozen = 1, frozen_at = ?
               WHERE gameweek = ? AND frozen = 0""",
            (utcnow(), int(gameweek)))
        return cur.rowcount > 0


def get_gw_report(gameweek):
    """One edition, payload already decoded. None if it was never published."""
    import json
    with connect() as conn:
        row = conn.execute("SELECT * FROM gw_report WHERE gameweek = ?",
                           (int(gameweek),)).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["payload"] = json.loads(rec["payload"])
    return rec


def gw_report_index():
    """Every published edition, newest first, WITHOUT the payloads.

    The archive list and the sitemap both only need to know which gameweeks
    exist and when they changed; pulling a few dozen full editions to render a
    list of links would be a waste that grows every week."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT gameweek, frozen, generated_at, frozen_at, deadline_time
                 FROM gw_report ORDER BY gameweek DESC""")]


# ---- retention ------------------------------------------------------------

def purge_expired(retention_months=RETENTION_MONTHS, now=None):
    """Delete manager data older than the retention period.

    The AI Manager (sentinel fpl_id 0) is exempt: it isn't a person, and its
    record is the whole point of the feature. Everything else ages out.

    Picks are removed by the ON DELETE CASCADE on manager_team, so deleting the
    header rows is sufficient - this can't leave orphaned child rows behind.
    """
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(retention_months * 30.44))).isoformat()

    with connect() as conn:
        teams = conn.execute(
            """DELETE FROM manager_team
               WHERE fpl_id != ? AND captured_at < ?""",
            (AI_MANAGER_FPL_ID, cutoff)).rowcount
        drafts_gone = conn.execute(
            "DELETE FROM manager_draft WHERE updated_at < ?", (cutoff,)).rowcount
        # A known_manager row is only useful while something references it.
        known = conn.execute(
            """DELETE FROM known_manager
               WHERE last_seen < ?
                 AND fpl_id NOT IN (SELECT fpl_id FROM manager_team)
                 AND fpl_id NOT IN (SELECT fpl_id FROM manager_draft)""",
            (cutoff,)).rowcount
    return {"cutoff": cutoff, "manager_gameweeks": teams,
            "drafts": drafts_gone, "known_managers": known}
