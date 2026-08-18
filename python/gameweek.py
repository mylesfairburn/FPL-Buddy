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


def deadlines_within(events=None, now=None, window_hours=(0, 24)):
    """Gameweeks whose deadline falls inside a window of hours from now.

    Returns (gameweek, deadline_iso, hours_left) oldest first - the same shape
    every other window function here returns, so they are all read the same
    way.

    Every window built on this is wider than the hourly poll interval, so a run
    always lands inside one even if the box misses a poll. That means the
    window can and will match on consecutive runs, and anything acting on it
    has to be idempotent - the edition builders are, and the notifications go
    through db.mark_notified.
    """
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


def preview_due(events=None, now=None, window_hours=PREVIEW_WINDOW_HOURS):
    """Gameweeks whose deadline is about a day away.

    The moment the briefing becomes postable. See PREVIEW_WINDOW_HOURS above
    for why this is a day out rather than at the deadline itself."""
    return deadlines_within(events, now, window_hours)


# When a deadline reminder is pushed to Discord, in hours before it.
#
# Two, because they do different jobs. The day-out one is the same moment the
# briefing becomes postable and is a prompt to post it. The two-hour one is a
# prompt to check your own team, which is a different thing entirely and lands
# when there is still time to act.
#
# Both windows are at least two hours wide against an hourly poll, so a single
# missed run costs nothing. They overlap the poll deliberately and are
# deduplicated by the notification ledger rather than by narrowing them.
REMINDER_WINDOWS = {"day": (22, 26), "final": (1, 3)}


def reminders_due(events=None, now=None):
    """(kind, gameweek, deadline_iso, hours_left) for every reminder that
    should be sent right now, across both windows."""
    out = []
    for kind, window in REMINDER_WINDOWS.items():
        for gw, deadline, hours in deadlines_within(events, now, window):
            out.append((kind, gw, deadline, hours))
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


def latest_finished_gameweek(events=None):
    """The highest gameweek FPL has both finished and had its stats checked.

    This is the round the roundup writes up, and only this one. The alternative
    - every finished gameweek without a roundup - looks like a harmless
    backfill and isn't: a roundup names how many managers owned a player, and
    the only ownership figure available now is today's. Writing up October in
    January would print January's ownership under an October headline, on a
    page that then freezes and says so forever.

    Restricting it to the newest round means a missed night costs nothing (the
    next run still writes it, and a week has to pass before a newer round can
    displace it) and a first run against a mid-season database writes one
    roundup rather than twenty wrong ones.
    """
    events = events if events is not None else get_events()
    finished = [e.get("id") for e in events
                if e.get("id") is not None
                and e.get("finished") and e.get("data_checked")]
    return max(finished) if finished else None


def detect_mode(fixtures_path=None, events=None):
    """'inseason' once the first gameweek's deadline has passed, 'preseason'
    before it.

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


def get_event_live_stats(gameweek):
    """The same call, but the WHOLE stats block per element rather than just
    the points total: minutes, goals, assists, bonus, bps and the expected-goal
    columns.

    Kept separate from get_event_live() rather than replacing it. That function
    feeds the actual-points backfill, which wants exactly one integer per
    element and would otherwise have to know which key to reach for on a dict
    that FPL changes the shape of between seasons. This one feeds the gameweek
    roundup, which is written precisely to say WHY a score happened - "returned
    nothing from 0.8 expected goals" needs the column that sentence names.

    Returns {element_id: stats_dict}, empty if the API is unreachable. An empty
    result is a thin roundup, not a failed job.
    """
    data = _get(f"{BASE}/event/{int(gameweek)}/live/")
    if not data:
        return {}
    out = {}
    for el in data.get("elements", []):
        if el.get("id") is None:
            continue
        stats = el.get("stats")
        if isinstance(stats, dict):
            out[int(el["id"])] = stats
    return out
