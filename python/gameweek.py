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

import threading
import time
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


# bootstrap-static is 1.8 MB and every caller without an events list in hand
# pulls the whole thing to ask what gameweek it is. /player/<slug> asks on every
# render - 570 pages, so a crawl once cost about a gigabyte of FPL's bandwidth
# to learn one boolean - and /api/live/<gw> is polled by every open tab.
#
# Two minutes because nothing in the request path needs a deadline to the
# second; the hourly cron watcher acts on those, and its windows are hours wide.
_EVENTS_TTL_S = 120

# Sync endpoints run in a threadpool. Held only around the two slots, never
# across the HTTP call - that would serialise the site behind one slow fetch.
_EVENTS_LOCK = threading.Lock()
_EVENTS_CACHE = {"events": None, "at": 0.0}


def get_events(force=False):
    """The events[] array from bootstrap-static: one entry per gameweek with
    deadline_time / is_current / is_next / finished / data_checked.

    Cached for _EVENTS_TTL_S; `force` bypasses it for callers that ACT on the
    clock rather than render it.

    A failed fetch serves the last good list, not []. [] is a real answer -
    "no gameweek has started" - which the player pages render as "these are
    last season's numbers". A stale clock beats one that is a year wrong.
    """
    now = time.monotonic()
    if not force:
        with _EVENTS_LOCK:
            cached, at = _EVENTS_CACHE["events"], _EVENTS_CACHE["at"]
        if cached is not None and (now - at) < _EVENTS_TTL_S:
            return cached

    data = _get(f"{BASE}/bootstrap-static/")
    events = (data or {}).get("events") or []
    if not events:
        # Only a process that has never succeeded returns [].
        with _EVENTS_LOCK:
            stale = _EVENTS_CACHE["events"]
        return stale if stale is not None else []

    with _EVENTS_LOCK:
        _EVENTS_CACHE["events"] = events
        _EVENTS_CACHE["at"] = time.monotonic()
    return events


def clear_events_cache():
    """Drop the cached clock. For tests."""
    with _EVENTS_LOCK:
        _EVENTS_CACHE["events"] = None
        _EVENTS_CACHE["at"] = 0.0


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


def deadline_for(gameweek, events=None):
    """One gameweek's deadline as FPL states it, or None if it isn't listed.

    Handed to the front end so a page describing a decision that has not been
    committed yet can say when it will be, rather than leaving the reader to
    work out whether what they are looking at is final."""
    events = events if events is not None else get_events()
    for e in events:
        if e.get("id") == int(gameweek):
            return e.get("deadline_time")
    return None


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


def started_teams(gameweek):
    """Team ids with at least one kicked-off fixture in `gameweek`.

    The distinction `get_event_live` cannot make. That call returns a points
    total for every element in the game, and a player whose match is still
    hours away scores 0 in it - indistinguishable from a player who has been on
    the pitch for ninety minutes and done nothing. On a Saturday lunchtime that
    is most of the league, so a squad reads as a disaster when in truth it has
    barely started.

    A club with a double gameweek counts as started once EITHER fixture has: by
    then there is a real score to show, which beats a fixture tile for the
    second match.

    Returns None - not an empty set - when the fixtures call fails. Empty would
    mean "nobody has played", which would blank every score on the pitch on the
    strength of one failed request. None means "can't tell", and the callers
    fall back to showing scores, which is what they did before this existed.
    """
    data = _get(f"{BASE}/fixtures/?event={int(gameweek)}")
    if data is None:
        return None
    out = set()
    for fixture in data:
        if not fixture.get("started"):
            continue
        for side in ("team_h", "team_a"):
            if fixture.get(side) is not None:
                out.add(int(fixture[side]))
    return out


def finished_teams(gameweek):
    """Team ids with no unfinished fixture left in `gameweek`.

    The counterpart to started_teams, and the one automatic substitutions need.
    A starter on nought minutes is only definitely not playing once his match is
    over - before that he is a substitute who might still come on, and
    replacing him would move the score for a reason nobody watching could see.

    A club with a double gameweek only counts once BOTH fixtures are done, which
    is the conservative direction: it delays a substitution rather than making
    one that the second match then contradicts.

    Returns None when the fixtures call fails - "can't tell", which the callers
    read as "substitute nobody".
    """
    data = _get(f"{BASE}/fixtures/?event={int(gameweek)}")
    if data is None:
        return None
    playing, seen = set(), set()
    for fixture in data:
        for side in ("team_h", "team_a"):
            team = fixture.get(side)
            if team is None:
                continue
            seen.add(int(team))
            if not fixture.get("finished"):
                playing.add(int(team))
    return seen - playing


def event_minutes(gameweek):
    """{element_id: minutes} for a gameweek.

    Auto-substitutions turn on minutes, not points: a starter who came on for
    ten minutes and did nothing scored the same nought as one who never left
    the bench, and only the second is replaced.
    """
    out = {}
    for element, stats in get_event_live_stats(gameweek).items():
        minutes = stats.get("minutes")
        if minutes is not None:
            out[element] = int(minutes)
    return out


# FPL's element_type ids, in the order bootstrap-static lists them.
_POSITION_BY_TYPE = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def element_index():
    """{element_id: {"pos", "team", "team_code", "web_name", "cost"}} straight
    from bootstrap-static.

    Deliberately not the rated player pool. The backfill jobs that need a
    player's position in order to apply substitutions run hourly and are cheap
    by design - building the rated pool to learn that Dubravka is a goalkeeper
    would make the cheapest job on the schedule one of the most expensive.

    It carries the name and shirt as well as the position because the other
    caller is the fallback that keeps a stored pick on the pitch when the rated
    pool has lost him - see ai_manager._squad_from_rows. A card needs a name and
    a shirt to be drawn at all, and both are right here in the same response the
    position came from.
    """
    data = _get(f"{BASE}/bootstrap-static/")
    out = {}
    for el in (data or {}).get("elements") or []:
        if el.get("id") is None:
            continue
        cost = el.get("now_cost")
        out[int(el["id"])] = {
            "pos": _POSITION_BY_TYPE.get(el.get("element_type")),
            "team": el.get("team"),
            "team_code": el.get("team_code"),
            "code": el.get("code"),
            "web_name": el.get("web_name"),
            "status": el.get("status"),
            "cost": round(float(cost) / 10.0, 1) if cost is not None else None,
        }
    return out


def event_fixtures(gameweek, team_short=None):
    """{team_id: [{"opponent", "was_home", "difficulty", "started", "finished"}]}
    for one gameweek.

    The projection horizon starts at the round whose deadline has NOT passed, so
    the moment a gameweek kicks off it drops out of every player's
    `next_gameweeks` list - and the pitch showing that gameweek lost the one
    thing it could still say about a player yet to play: who he is playing.
    Every such card fell back to a blank tile. This is that gameweek's fixture
    list, asked for directly, because by then it is a fact rather than a
    forecast.

    Returns {} if the call fails - the tiles go back to being blank, which is
    where they were.
    """
    data = _get(f"{BASE}/fixtures/?event={int(gameweek)}")
    if not data:
        return {}
    short = team_short or {}
    out = {}
    for fixture in data:
        home, away = fixture.get("team_h"), fixture.get("team_a")
        if home is None or away is None:
            continue
        for team, opponent, is_home, difficulty in (
                (home, away, True, fixture.get("team_h_difficulty")),
                (away, home, False, fixture.get("team_a_difficulty"))):
            out.setdefault(int(team), []).append({
                "opponent": short.get(int(opponent)) or str(opponent),
                "was_home": is_home,
                "difficulty": difficulty,
                "started": bool(fixture.get("started")),
                "finished": bool(fixture.get("finished")),
            })
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
