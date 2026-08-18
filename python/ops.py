"""Knowing whether the machine is still working.

Five cron jobs keep this site current, and their failure mode is total
silence: a job that stops running looks exactly like a week in which nothing
happened. The symptoms - a briefing that stops updating, an AI squad never
committed, a roundup that never appears - surface days later and all look like
a bug in the feature rather than a job that isn't running.

Three things in here, in order of how much they matter to one person
maintaining this alone:

  1. A heartbeat. Every job records that it started, and whether it finished.
     `/api/ai/status` reports which jobs are overdue, so "is the box still
     doing its work" is one URL rather than four SSH sessions.

  2. An alert. If FPL_ALERT_WEBHOOK is set, a failed or overdue job pushes a
     message to it. This is the only part of the system that reaches OUT rather
     than waiting to be asked, and it is the difference between finding out on
     the night and finding out when someone emails to ask why the site is stale.

  3. Backups. The SQLite file holds the entire published archive and the whole
     predicted-versus-actual track record.

None of this is allowed to break a job. Every function here swallows its own
errors and reports them: an alerting system that can take down the thing it
watches is worse than no alerting system.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import db

# Discord's own limit, and the reason the long-form drafts stay behind the
# phone URL rather than being pushed: a Reddit draft is routinely longer than
# this, and a message truncated mid-argument is worse than a link to the whole
# thing.
DISCORD_HARD_LIMIT = 2000

# How long after its expected interval a job is considered overdue.
#
# Generous on purpose. Every one of these jobs is idempotent and several have
# windows wider than their interval, so a single missed run genuinely costs
# nothing - and an alert that fires on a run that was five minutes late is an
# alert that gets muted, after which none of this exists.
STALE_GRACE = 2.0

# What each job's normal interval is, in hours. Read alongside the cron file;
# these are the same schedule expressed in the one place the app can see it.
JOB_INTERVALS = {
    "deadline-watch": 1,
    "daily-refresh": 24,
    "gameweek-report": 24,
    "seo-report": 24,
    "indexnow": 24,
}

# Where nightly database copies go, and how many are kept.
#
# Fourteen is two weeks: long enough that a corruption noticed on a Monday can
# be rolled back past the weekend, short enough that the backups cannot grow to
# rival the volume. The file is a few megabytes, so this is tens of megabytes
# in total.
BACKUP_KEEP = 14


def _now():
    return datetime.now(timezone.utc)


def utcnow():
    return _now().isoformat()


# ---------------------------------------------------------------------------
#  The heartbeat
# ---------------------------------------------------------------------------

def record_start(job):
    """Note that `job` has begun. Returns the row id, or None if the write
    failed - in which case the job carries on unrecorded, which is the correct
    trade: the work matters, the bookkeeping doesn't."""
    try:
        with db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO job_run (job, started_at, status)
                   VALUES (?, ?, 'running')""", (str(job), utcnow()))
            return cur.lastrowid
    except sqlite3.Error:
        return None


def record_finish(run_id, status="ok", detail=None):
    """Close out a run. A row left at 'running' is a job that died hard -
    killed, out of memory, box rebooted - and being able to tell that apart
    from a clean failure is most of the value of recording it at all."""
    if run_id is None:
        return False
    try:
        with db.connect() as conn:
            conn.execute(
                """UPDATE job_run SET finished_at = ?, status = ?, detail = ?
                   WHERE id = ?""",
                (utcnow(), str(status), (str(detail)[:500] if detail else None),
                 int(run_id)))
        return True
    except sqlite3.Error:
        return False


def tracked(job, main_fn):
    """Run `main_fn`, record the outcome, and alert if it failed.

    The wrapper for the two jobs that are standalone scripts rather than
    `jobs.py` subcommands - `indexnow.py` and `seo_report.py`. Without it they
    would sit in JOB_INTERVALS having never recorded anything and would be
    reported as permanently overdue, which is the one thing a staleness check
    must not do: an alert that is always firing is an alert nobody reads.

    Returns the exit code, so a caller can hand it straight to sys.exit.
    """
    run_id = record_start(job)
    try:
        rc = main_fn()
    except SystemExit as e:
        # Both scripts exit through sys.exit rather than by returning, so the
        # code arrives here as an exception. Anything non-zero is a failure
        # worth recording and pushing.
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        record_finish(run_id, "ok" if rc == 0 else "failed",
                      None if rc == 0 else f"exit code {rc}")
        if rc != 0:
            alert(f"{job} exited {rc}.")
        return rc
    except Exception as e:
        record_finish(run_id, "failed", str(e))
        alert(f"{job} failed: {e}")
        raise

    rc = rc or 0
    record_finish(run_id, "ok" if rc == 0 else "failed",
                  None if rc == 0 else f"exit code {rc}")
    if rc != 0:
        alert(f"{job} exited {rc}.")
    return rc


def last_runs():
    """The most recent run of each job, newest first."""
    try:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT job, started_at, finished_at, status, detail
                     FROM job_run
                    WHERE id IN (SELECT MAX(id) FROM job_run GROUP BY job)
                    ORDER BY started_at DESC""")]
    except sqlite3.Error:
        return []


def last_success(job):
    """When `job` last completed cleanly, as an ISO string, or None.

    The figure that actually matters. A job that has run every hour for a day
    and failed every time has a very recent 'last run' and has not worked since
    yesterday."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                """SELECT finished_at FROM job_run
                    WHERE job = ? AND status = 'ok' AND finished_at IS NOT NULL
                    ORDER BY id DESC LIMIT 1""", (str(job),)).fetchone()
        return row["finished_at"] if row else None
    except sqlite3.Error:
        return None


def _parse(stamp):
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def stale_jobs(intervals=None, grace=STALE_GRACE, now=None):
    """Jobs that should have succeeded by now and haven't.

    A job that has NEVER succeeded is reported as stale with `never=True`
    rather than skipped. On a fresh deployment that is the normal state for a
    few hours and is exactly what you want to see; a fortnight later it means
    the cron line was never installed, which is the single most common way this
    kind of setup is silently broken.
    """
    intervals = intervals or JOB_INTERVALS
    now = now or _now()
    out = []
    for job, hours in intervals.items():
        stamp = last_success(job)
        when = _parse(stamp)
        deadline = timedelta(hours=hours * grace)
        if when is None:
            out.append({"job": job, "last_success": None, "never": True,
                        "overdue_hours": None, "expected_every_hours": hours})
        elif now - when > deadline:
            out.append({"job": job, "last_success": stamp, "never": False,
                        "overdue_hours": round((now - when).total_seconds() / 3600, 1),
                        "expected_every_hours": hours})
    return out


def latest_player_post(now=None):
    """When the nightly player write-up last produced something.

    Separate from the job heartbeat above, and the distinction is the whole
    point: `gameweek-report` succeeding says the job ran, not that it wrote
    anything. Those two came apart for a fortnight - every run green, no post
    on any of them - and there was no single place that showed it.

    Keys match the `player_post` row and `db.recent_player_posts`, so the same
    field names mean the same thing wherever a caller meets them. `post_date`
    and `days_ago` are None when nothing has ever been written.
    """
    empty = {"post_date": None, "gameweek": None, "code": None,
             "angle": None, "days_ago": None}
    try:
        with db.connect() as conn:
            row = conn.execute(
                """SELECT post_date, gameweek, code, angle FROM player_post
                    ORDER BY post_date DESC LIMIT 1""").fetchone()
    except sqlite3.Error:
        return empty

    if not row:
        return empty

    out = dict(row)
    when = _parse(out.get("post_date"))
    out["days_ago"] = ((now or _now()) - when).days if when else None
    return out


def health():
    """The whole picture, for /api/ai/status."""
    stale = stale_jobs()
    return {
        "last_runs": last_runs(),
        "stale": stale,
        # One boolean, so a phone glancing at the endpoint doesn't have to
        # interpret anything.
        "healthy": not stale,
        "alerting": bool(webhook_url()),
        "channels": configured_channels(),
        "backups": backup_status(),
        "player_post": latest_player_post(),
    }


def prune_job_runs(keep_days=60):
    """Keep the heartbeat table from growing forever. deadline-watch alone
    writes 24 rows a day, which is ~9,000 a year for a table whose only query
    asks about the last few."""
    cutoff = (_now() - timedelta(days=keep_days)).isoformat()
    try:
        with db.connect() as conn:
            return conn.execute("DELETE FROM job_run WHERE started_at < ?",
                                (cutoff,)).rowcount
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
#  Alerting
# ---------------------------------------------------------------------------

# The Discord channels this app can post to, and the variable holding each
# one's webhook URL.
#
# Four rather than one because they want completely different notification
# settings on a phone. `alerts` should buzz at 3am; `content` should be waiting
# in the morning; `seo` is a weekly read. One channel carrying all of them ends
# up muted, which is the same as having none.
#
# Every channel falls back to `alerts` when its own variable is unset, so a
# single FPL_ALERT_WEBHOOK gets you everything in one place and splitting them
# out later is one variable at a time rather than an all-or-nothing switch.
CHANNELS = {
    # Scheduled jobs that failed or stopped running.
    "alerts": "FPL_ALERT_WEBHOOK",
    # Post drafts, as they become worth posting: the nightly player write-up,
    # the briefing when it goes postable, the roundup when it lands.
    "drafts": "FPL_DRAFTS_WEBHOOK",
    # Deadline reminders, and what the AI Manager did with its squad.
    "gameweek": "FPL_GAMEWEEK_WEBHOOK",
    # The weekly Search Console / Bing digest.
    "seo": "FPL_SEO_WEBHOOK",
    # Ko-fi donations, relayed by /api/kofi. Note this is the DISCORD webhook
    # to post into - FPL_KOFI_TOKEN is the separate secret Ko-fi sends to prove
    # a delivery is really from them, and FPL_KOFI_HANDLE is the public name in
    # the site's donate button. Three Ko-fi variables doing three jobs.
    "kofi": "FPL_KOFI_WEBHOOK",
    # Questions typed into the box on /faq. This channel IS the storage: the
    # endpoint writes nothing to the database and records no address, so a
    # message that fails to send is a question nobody ever sees. Point it
    # somewhere you read.
    #
    # Like the Ko-fi relay, the text here is written by a stranger, which is
    # why _payload_for disables mentions for every Discord post rather than
    # per call site.
    "questions": "FPL_QUESTIONS_WEBHOOK",
}

# Discord rejects a message over 2000 characters outright. The payload builder
# trims to this, which leaves room for the "FPL Buddy: " prefix and for Discord
# to render the Markdown.
DISCORD_LIMIT = 1900


def webhook_url(channel="alerts"):
    """The webhook for `channel`, falling back to the alerts one.

    The fallback is what makes this usable before it is fully configured: set
    one variable and everything arrives somewhere, rather than three of the
    four features silently doing nothing because you only set up one channel.
    """
    variable = CHANNELS.get(channel, CHANNELS["alerts"])
    url = os.environ.get(variable, "").strip()
    if url:
        return url
    return os.environ.get(CHANNELS["alerts"], "").strip()


def configured_channels():
    """Which channels would actually deliver, and whether via their own webhook
    or the alerts fallback. Reported by `jobs.py status`."""
    out = {}
    for channel, variable in CHANNELS.items():
        own = bool(os.environ.get(variable, "").strip())
        out[channel] = ("own" if own
                        else "fallback" if webhook_url(channel) else "off")
    return out


def _payload_for(url, message):
    """Shape the message for whatever is on the other end.

    Three receivers cover essentially every free option someone would actually
    use for this: a Discord webhook, a Slack webhook, or ntfy (and anything
    else that accepts a plain POST body). Detected from the URL, because asking
    someone to set a second environment variable describing the first one is
    how a setting ends up wrong.

    Detection wins over the override, and that ordering is the whole point.
    It used to be the other way round - FPL_ALERT_FORMAT first, detection as
    the fallback - which meant one stray value in .env silently made every
    channel POST a plain body to Discord, and Discord answered every one of
    them with:

        400 {"_misc": ["Expected \"Content-Type\" header to be one of ..."]}

    A Discord webhook URL can only ever want Discord's JSON, so letting a
    setting overrule that was offering a way to be wrong for no benefit. The
    override now applies only where detection has nothing to go on - an ntfy
    topic, a self-hosted receiver, anything not recognisably Discord or Slack.
    """
    if "discord.com" in url or "discordapp.com" in url:
        kind = "discord"
    elif "hooks.slack.com" in url:
        kind = "slack"
    else:
        kind = os.environ.get("FPL_ALERT_FORMAT", "").strip().lower() or "text"

    if kind == "discord":
        # `allowed_mentions: {"parse": []}` disables every ping this message
        # could produce - @everyone, @here, roles and users - while leaving the
        # text itself untouched.
        #
        # Not paranoia: the Ko-fi relay puts a DONOR-SUPPLIED name and message
        # into a Discord post, and a webhook can ping @everyone by default. A
        # stranger naming themselves "@everyone" would otherwise notify the
        # whole server, for free, from a public endpoint. Set here rather than
        # at that one call site so no future sender has to remember it.
        return {"json": {"content": message[:DISCORD_LIMIT],
                         "allowed_mentions": {"parse": []}}}
    if kind == "slack":
        return {"json": {"text": message[:3000]}}
    return {"data": message[:3000].encode("utf-8")}


def notify(channel, message, prefix=None):
    """Push a message to one channel. Returns True if it went.

    Silent no-op when that channel has no webhook and no fallback, which is the
    default. This is opt-in infrastructure, and logging "no webhook configured"
    every night would add noise to the log nobody reads in order to complain
    about not being able to escape it.

    `prefix` is prepended when given. The alerts channel uses it so a failure
    is identifiable at a glance; the drafts channel does not, because those
    messages are drafts meant to be read as written and a prefix is one more
    thing to delete before posting.

    Cannot raise. This runs in the failure path of a cron job, and a
    notification call that threw would turn "one job failed" into "the job that
    reports failures also failed", losing the original error.
    """
    try:
        ok, _detail = send(channel, message, prefix=prefix)
        return ok
    except Exception:
        return False


def send(channel, message, prefix=None):
    """Push a message and say what happened: (ok, detail).

    The reporting half of `notify`. Split out because the two callers want
    opposite things: a cron job wants a boolean and no possibility of an
    exception, while `jobs.py status --test-alert` exists solely to explain a
    failure and was inheriting that silence - a bare "FAILED" sends you looking
    at your network when Discord has plainly said 401.

    `detail` is always a short human-readable string. For an HTTP error it
    carries the status and the start of the response body, which is where
    Discord puts the actual reason.
    """
    url = webhook_url(channel)
    if not url:
        return False, "no webhook configured for this channel"

    # A URL that still has the quotes it was written with in .env, or that
    # picked up a stray character, fails in a way that reads as a network
    # problem. Naming it costs one check and saves an hour.
    if not url.startswith(("http://", "https://")):
        return False, (f"the URL does not begin with http:// or https:// "
                       f"(it starts {url[:12]!r}) - check for quotes around "
                       f"the value in .env")

    body = f"{prefix}: {message}" if prefix else message
    try:
        import requests
        kwargs = _payload_for(url, body)
        response = requests.post(url, timeout=10, **kwargs)
        if response.status_code < 300:
            return True, f"HTTP {response.status_code}"
        text = (response.text or "").strip().replace("\n", " ")[:200]
        hint = {
            401: " - the webhook was deleted, or the URL is wrong",
            403: " - Discord refused it; check the webhook still has access to that channel",
            404: " - no such webhook; it was deleted, or the URL is truncated",
            429: " - rate limited, try again in a moment",
        }.get(response.status_code, "")
        return False, f"HTTP {response.status_code}{hint} :: {text}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def notify_once(channel, kind, ref, message, prefix=None):
    """Push a message at most once for a given (kind, ref).

    Every notification here is triggered by a window rather than an instant -
    "the deadline is about a day away" is four hours wide and is checked
    hourly - so this is what stops the same message arriving four times.

    The claim is taken BEFORE the send, deliberately. Sending first and
    recording afterwards means a successful push followed by a failed write
    repeats every hour until the window closes; claiming first means the worst
    case is one message lost to a webhook outage. `db.clear_notification` is
    there for resending that one by hand.
    """
    if not webhook_url(channel):
        return False
    if not db.mark_notified(kind, ref):
        return False
    return notify(channel, message, prefix=prefix)


def alert(message, subject="FPL Buddy"):
    """Push an ops alert. The failure path - kept as its own name because it is
    called from places that should not have to know about channels."""
    return notify("alerts", message, prefix=subject)


def alert_if_stale():
    """Push a summary of overdue jobs. What a daily check should call.

    One message listing all of them rather than one per job: five separate
    pushes at 03:00 because the box was off overnight is how notifications get
    turned off.
    """
    stale = stale_jobs()
    if not stale:
        return False
    lines = []
    for row in stale:
        if row["never"]:
            lines.append(f"• {row['job']} has never completed successfully")
        else:
            lines.append(f"• {row['job']} last succeeded {row['overdue_hours']}h "
                         f"ago (expected every {row['expected_every_hours']}h)")
    return alert("scheduled jobs are overdue:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
#  Backups
# ---------------------------------------------------------------------------

def backup_dir():
    """state/backups/, alongside the database it copies.

    On the same volume, which is worth being honest about: this protects
    against corruption, a bad migration and a mistaken DELETE, and not against
    losing the volume. Off-box copies are a host-level job - `restic`, a
    Backblaze bucket, whatever the Proxmox host already does - and pretending
    a sibling directory is one would be worse than not having it.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(db.db_path())), "backups")
    os.makedirs(path, exist_ok=True)
    return path


def backup_database(keep=BACKUP_KEEP):
    """Copy the database, then drop the oldest copies.

    Uses SQLite's own backup API rather than copying the file. That matters
    here specifically: the database runs in WAL mode with a cron writer and a
    web reader on it at once, so a filesystem copy can catch a write in
    progress and produce a file that opens fine and is missing the last
    transaction. The backup API takes a consistent snapshot of a live database,
    which is the whole reason it exists.
    """
    source_path = db.db_path()
    if not os.path.exists(source_path):
        return {"written": False, "detail": "no database file to back up"}

    stamp = _now().strftime("%Y-%m-%d")
    target = os.path.join(backup_dir(), f"fpl_companion-{stamp}.db")
    try:
        source = sqlite3.connect(source_path)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except sqlite3.Error as e:
        return {"written": False, "detail": f"backup failed: {e}"}

    removed = _prune_backups(keep)
    return {"written": True, "path": target,
            "bytes": os.path.getsize(target), "removed": removed}


def _prune_backups(keep):
    try:
        names = sorted(n for n in os.listdir(backup_dir())
                       if n.startswith("fpl_companion-") and n.endswith(".db"))
    except OSError:
        return 0
    removed = 0
    for name in names[:-keep] if keep > 0 else names:
        try:
            os.remove(os.path.join(backup_dir(), name))
            removed += 1
        except OSError:
            continue
    return removed


def backup_status():
    """What backups exist, for the status endpoint. A backup nobody has
    verified the existence of is a backup nobody has."""
    try:
        names = sorted(n for n in os.listdir(backup_dir())
                       if n.startswith("fpl_companion-") and n.endswith(".db"))
    except OSError:
        return {"count": 0, "latest": None}
    if not names:
        return {"count": 0, "latest": None}
    latest = names[-1]
    try:
        size = os.path.getsize(os.path.join(backup_dir(), latest))
    except OSError:
        size = None
    return {"count": len(names), "latest": latest, "latest_bytes": size}


# ---------------------------------------------------------------------------
#  Disk
# ---------------------------------------------------------------------------

def prune_old_element_summaries(keep_seasons=1):
    """Delete cached per-player API responses for seasons that are over.

    `data/seasons/<season>/element_summaries/` gains a file per player per
    season - about 700 small JSON files - and nothing ever removed them. The
    current season's cache is the offline fallback for the whole pipeline and
    is never touched; a finished season's is dead weight, because the
    per-gameweek CSV derived from it is what training actually reads.
    """
    import seasons
    try:
        current = seasons.current_season()
        all_seasons = sorted(seasons.available_seasons())
    except Exception as e:
        return {"removed": 0, "detail": f"could not list seasons: {e}"}

    keep = set(all_seasons[-keep_seasons:]) | {current}
    removed_files, removed_seasons = 0, []
    for season in all_seasons:
        if season in keep:
            continue
        directory = os.path.join(seasons.SEASONS_DIR, season, "element_summaries")
        if not os.path.isdir(directory):
            continue
        try:
            for name in os.listdir(directory):
                if name.endswith(".json"):
                    os.remove(os.path.join(directory, name))
                    removed_files += 1
            os.rmdir(directory)
            removed_seasons.append(season)
        except OSError:
            continue
    return {"removed": removed_files, "seasons": removed_seasons}
