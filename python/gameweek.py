"""The season clock: what gameweek is it, and has a deadline just passed.

Everything time-sensitive in the app reads from here rather than deriving its
own answer, so there's one definition of "the gameweek reset".

Deadlines are irregular by design in FPL - midweek rounds land Tue/Wed evening,
an early Saturday kickoff pulls the deadline to 11:00, international breaks skip
weeks entirely, and double/blank gameweeks compress or drop rounds. So nothing
here assumes a weekly cadence or a fixed day: `deadline_time` from the live
events feed is the only source of truth, and the watcher polls hourly rather
than trying to predict when to look.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import seasons

BASE = "https://fantasy.premierleague.com/api"

# Deadlines older than this that we've never processed are recorded as skipped
# rather than acted on: the model's `next_gameweeks` predictions roll forward
# once fixtures finish, so a Best XI "for" a long-past gameweek would actually
# be built from some later gameweek's numbers. Better to have a visible gap.
MAX_BACKFILL_HOURS = 24


def _get(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return None


def get_events():
    """The events[] array from bootstrap-static: one entry per gameweek with
    deadline_time / is_current / is_next / finished / data_checked."""
    data = _get(f"{BASE}/bootstrap-static/")
    return (data or {}).get("events") or []


def parse_deadline(value):
    """FPL stamps these as e.g. '2026-08-21T17:30:00Z'. Returns an aware UTC
    datetime, or None if unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def current_gameweek(events=None):
    """The gameweek in play (deadline passed, not yet finished). Falls back to
    the latest gameweek whose deadline has passed, since is_current is briefly
    absent between rounds."""
    events = events if events is not None else get_events()
    for e in events:
        if e.get("is_current"):
            return e.get("id")
    started = started_gameweeks(events)
    return started[-1] if started else None


def next_gameweek(events=None):
    """The gameweek being picked for right now - i.e. the one whose deadline
    hasn't passed yet. This is what an AI squad should be optimised FOR."""
    events = events if events is not None else get_events()
    for e in events:
        if e.get("is_next"):
            return e.get("id")
    now = datetime.now(timezone.utc)
    upcoming = [e.get("id") for e in sorted(events, key=lambda x: x.get("id") or 0)
                if (parse_deadline(e.get("deadline_time")) or now) > now]
    return upcoming[0] if upcoming else None


def started_gameweeks(events=None, now=None):
    """Gameweek ids whose deadline has passed, oldest first."""
    events = events if events is not None else get_events()
    now = now or datetime.now(timezone.utc)
    out = []
    for e in sorted(events, key=lambda x: x.get("id") or 0):
        dl = parse_deadline(e.get("deadline_time"))
        if dl and dl <= now and e.get("id") is not None:
            out.append(e["id"])
    return out


def newly_passed_deadlines(events=None, now=None, is_processed=None,
                           max_backfill_hours=MAX_BACKFILL_HOURS):
    """Gameweeks whose deadline has passed and that haven't been handled yet.

    Returns (gameweek, deadline_iso, fresh) tuples. `fresh` is False when the
    deadline passed longer than max_backfill_hours ago - the caller records
    those as skipped instead of generating a snapshot from stale predictions.
    That also stops a first run against a mid-season DB from trying to
    reconstruct every gameweek since August."""
    events = events if events is not None else get_events()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_backfill_hours)
    out = []
    for e in sorted(events, key=lambda x: x.get("id") or 0):
        gw, dl = e.get("id"), parse_deadline(e.get("deadline_time"))
        if gw is None or dl is None or dl > now:
            continue
        if is_processed and is_processed(gw):
            continue
        out.append((gw, e.get("deadline_time"), dl >= cutoff))
    return out


# How close to a deadline the AI teams get committed. The point of waiting is
# team news: press conferences land in the 24-48h before a deadline, and FPL
# flags players as it learns. Deciding early means picking someone who was
# ruled out on Friday morning. Wider than the hourly poll interval so a run
# always lands inside it - two runs inside the window is harmless, the commit
# is idempotent.
COMMIT_WINDOW_MINUTES = 100


def imminent_deadlines(events=None, now=None, window_minutes=COMMIT_WINDOW_MINUTES):
    """Gameweeks whose deadline is close but has NOT passed yet.

    Returns (gameweek, deadline_iso, minutes_left) oldest first. This is the
    window the AI teams are committed in - as late as the schedule reliably
    allows, so the squad reflects the freshest availability data."""
    events = events if events is not None else get_events()
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=window_minutes)
    out = []
    for e in sorted(events, key=lambda x: x.get("id") or 0):
        gw, dl = e.get("id"), parse_deadline(e.get("deadline_time"))
        if gw is None or dl is None:
            continue
        if now < dl <= horizon:
            out.append((gw, e.get("deadline_time"), int((dl - now).total_seconds() // 60)))
    return out


# When the briefing becomes postable, roughly a day out.
#
# The final edition already freezes 100 minutes before a deadline, which is the
# right moment for a version that will never change - but it is the wrong moment
# to be posting a link. Most managers make their transfers the day before, and a
# post an hour before kickoff reaches the people who had already decided.
#
# So there are two moments, not one. This is the earlier: a full rebuild on the
# freshest data, marked as safe to post, while there is still a day for someone
# to act on it.
#
# The window is four hours wide against an hourly poll, so a run always lands
# inside it even if the box misses one. Everything downstream is idempotent -
# the stage only ever moves forward - so landing in it twice is harmless.
PREVIEW_WINDOW_HOURS = (22, 26)


def preview_due(events=None, now=None, window_hours=PREVIEW_WINDOW_HOURS):
    """Gameweeks whose deadline is about a day away.

    Returns (gameweek, deadline_iso, hours_left) oldest first. Same shape as
    imminent_deadlines() above, and read the same way - the two are the two
    stages of publishing one edition."""
    events = events if events is not None else get_events()
    now = now or datetime.now(timezone.utc)
    earliest = now + timedelta(hours=window_hours[0])
    latest = now + timedelta(hours=window_hours[1])
    out = []
    for e in sorted(events, key=lambda x: x.get("id") or 0):
        gw, dl = e.get("id"), parse_deadline(e.get("deadline_time"))
        if gw is None or dl is None:
            continue
        if earliest <= dl <= latest:
            out.append((gw, e.get("deadline_time"),
                        round((dl - now).total_seconds() / 3600, 1)))
    return out


def gameweek_is_finished(gameweek, events=None):
    """True once FPL has both finished the round and confirmed its stats
    (`data_checked`). Bonus points aren't final until data_checked flips, so
    backfilling actual scores any earlier captures provisional numbers."""
    events = events if events is not None else get_events()
    for e in events:
        if e.get("id") == gameweek:
            return bool(e.get("finished")) and bool(e.get("data_checked"))
    return False


def detect_mode(fixtures_path=None, events=None):
    """'inseason' once the first gameweek's deadline has passed, 'preseason'
    before it. Replaces the manual toggle that used to live in the navbar.

    Primary signal is the events list. If the API can't be reached, falls back
    to the cached fixtures CSV - any kickoff in the past means the season is
    underway. If neither is available we can't tell, and 'preseason' is the safe
    answer: it's the mode that works off last season's data and so never needs
    current-season gameweek history."""
    events = events if events is not None else get_events()
    fixtures_path = fixtures_path or seasons.fixtures_path()
    deadlines = [e.get("deadline_time") for e in events if e.get("deadline_time")]
    if deadlines:
        when = parse_deadline(min(deadlines))
        if when:
            return "inseason" if datetime.now(timezone.utc) >= when else "preseason"

    try:
        fixtures = pd.read_csv(fixtures_path)
        kickoffs = pd.to_datetime(fixtures.get("kickoff_time"), errors="coerce", utc=True)
        if kickoffs.notna().any() and kickoffs.min() <= pd.Timestamp.now(tz="UTC"):
            return "inseason"
    except (FileNotFoundError, OSError, ValueError, AttributeError):
        pass
    return "preseason"


def get_event_live(gameweek):
    """Per-element actual stats for a gameweek (`/event/{gw}/live/`).

    One call covers all ~700 players, which is what makes backfilling real
    scores onto a frozen snapshot cheap. Returns {element_id: total_points}."""
    data = _get(f"{BASE}/event/{int(gameweek)}/live/")
    if not data:
        return {}
    out = {}
    for el in data.get("elements", []):
        stats = el.get("stats") or {}
        if el.get("id") is not None and stats.get("total_points") is not None:
            out[int(el["id"])] = int(stats["total_points"])
    return out
