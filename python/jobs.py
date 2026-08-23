"""Scheduled jobs, run from the Proxmox host's cron (see deploy/README).

    python jobs.py deadline-watch    # hourly
    python jobs.py daily-refresh     # ~03:00 UK

Two jobs rather than one, because they answer to different clocks:

  deadline-watch has to be timing-accurate. FPL deadlines land at irregular
  times and days (midweek rounds, 11:00 Saturday starts, blank/double weeks),
  so there's no daily slot that reliably lands just after one. Hourly polling of
  `deadline_time` catches them all, and the work is cheap enough to do 24x/day.
  It carries three phases, all keyed off the same poll: publish the postable
  briefing about a day out, commit and freeze just before the deadline, capture
  real teams just after.

  daily-refresh is the heavy pipeline run, and wants to be LATE rather than
  timely: a Monday night game can finish ~22:00 and FPL's bonus-point
  finalisation (`data_checked`) often lags full time by an hour or more, so a
  literal midnight run risks freezing provisional scores.

Both run as a separate process from the web app. The app deliberately runs a
single worker holding rated data in an in-memory `state` dict, so these jobs
must not import-and-mutate that state - they write to SQLite, then poke
/api/refresh so the live process reloads.
"""

import argparse
import datetime
import os
import sys
import traceback

import requests

import ai_manager
import ai_team
import db
import drafts
import gameweek as gw_clock
import gw_report as gw_report_builder
import gw_roundup as gw_roundup_builder
import manager_history
import ops
import player_pages
import player_spotlight
import rating_model
import retention
import seasons
import social
from pipeline import run_pipeline
from rating_model import get_rated_position_dfs
from fetch_data import refresh_gameweek_stats
from squad_optimiser import DEFAULT_BUDGET, OptimisationError
from team_service import get_all_players, get_team_view

APP_URL = os.environ.get("FPL_APP_URL", "http://127.0.0.1:8000")
REFRESH_TOKEN = os.environ.get("FPL_REFRESH_TOKEN", "")


def log(msg):
    print(msg, flush=True)


def _rated_pool(mode=None):
    """Run the rating pipeline in-process and return the flat player pool.

    The job can't read the web app's in-memory state (different process), so it
    recomputes. That's the same work the app does at startup - a minute or so,
    which is fine for a scheduled job."""
    mode = mode or gw_clock.detect_mode()
    data = run_pipeline()
    # Same split as main.load_data, and it has to be the same or the two
    # disagree about who the good players are. This process rates the pool the
    # AI Manager commits a squad from and the briefing is written off; the app
    # rates the pool every page shows. Rating them on different form sources
    # would put the bot's transfers and the site's projections quietly out of
    # step, which is the kind of difference nobody notices until it is a
    # fortnight old. None = auto-detect, which counts played gameweeks.
    rating_mode = None if mode == "inseason" else mode
    try:
        position_dfs = get_rated_position_dfs(data["position_dfs"], mode=rating_mode)
    except ValueError as e:
        if mode != "inseason":
            raise
        log(f"  inseason ratings unavailable ({e}); using preseason")
        position_dfs = get_rated_position_dfs(data["position_dfs"], mode="preseason")
    return get_all_players(position_dfs).get("players", []), position_dfs


def ping_refresh():
    """Tell the running app to reload from disk. Non-fatal: the data is already
    committed to SQLite, and the app picks it up on its next restart anyway."""
    try:
        headers = {"X-Refresh-Token": REFRESH_TOKEN} if REFRESH_TOKEN else {}
        r = requests.post(f"{APP_URL}/api/refresh", headers=headers, timeout=300)
        log(f"  /api/refresh -> {r.status_code}")
        return r.status_code == 200
    except requests.exceptions.RequestException as e:
        log(f"  /api/refresh failed (app may be down): {e}")
        return False


def snapshot_managers(gameweek, position_dfs, next_gameweek=None):
    """Capture every known manager's real picks for a gameweek, and replace
    their in-app draft with those picks.

    This is the deadline reset: until it runs, the draft is whatever the user
    was building here; afterwards it IS their official team, re-pointed at the
    next gameweek so they can start editing again."""
    ids = db.known_managers()
    if not ids:
        log("  no known managers to snapshot")
        return 0, 0
    saved = replaced = 0
    for fpl_id in ids:
        try:
            # carry_forward off: this writes down what a manager ACTUALLY
            # picked. See the note on get_team_view.
            view = get_team_view(fpl_id, gameweek, position_dfs,
                                 carry_forward=False)
            if not view.get("available") or not view.get("squad"):
                log(f"  manager {fpl_id}: GW{gameweek} unavailable, skipping")
                continue
            gw_meta = view.get("gw") or {}
            manager_history.save_manager_gameweek(
                fpl_id, gameweek, view["squad"], gw_meta)
            saved += 1
            drafts.replace_with_official(
                fpl_id, view["squad"], gameweek,
                bank=gw_meta.get("bank"), next_gameweek=next_gameweek)
            replaced += 1
        except Exception as e:
            log(f"  manager {fpl_id}: FAILED ({e})")
    log(f"  snapshotted {saved}/{len(ids)} managers for GW{gameweek}; "
        f"{replaced} draft(s) replaced with official picks")
    return saved, replaced


def commit_ai_teams(gameweek, budget=DEFAULT_BUDGET, pool=None, late=False):
    """Lock in both AI squads for a gameweek.

    Idempotent by design: the stored rows ARE the ledger, so a second run in
    the same window sees the gameweek is already committed and does nothing.
    That matters because the commit window is wider than the poll interval, so
    two runs inside it is the normal case, not an edge case.
    """
    already_xv = ai_team.get_snapshot(gameweek) is not None
    already_mgr = ai_manager.get_gameweek(gameweek) is not None
    if already_xv and already_mgr:
        log(f"GW{gameweek}: AI teams already committed")
        return pool, []

    if pool is None:
        pool, _ = _rated_pool()
    detail = []
    committed_xi = committed_mgr = None

    if not already_xv:
        try:
            r = ai_team.generate_and_store(pool, gameweek, budget=budget)
            log(f"  AI Best XI: {r['formation']}, £{r['squad_cost']}m, "
                f"{r['predicted_points']} predicted pts")
            detail.append(f"best_xi={r['predicted_points']}")
            committed_xi = r
        except OptimisationError as e:
            log(f"  AI Best XI FAILED: {e}")
            detail.append(f"best_xi_failed={e}")

    if not already_mgr:
        try:
            m = ai_manager.run_gameweek(pool, gameweek, budget=budget)
            log(f"  AI Manager: {m['formation']}, {len(m['transfers'])} transfer(s), "
                f"chip={m['chip'] or 'none'}, {m['predicted_points']} predicted pts")
            for t in m["transfers"]:
                log(f"    {t['out']} -> {t['in']} (+{t['gain']}"
                    f"{'' if t['free'] else ', -4 hit'})")
            detail.append(f"manager={m['predicted_points']}")
            committed_mgr = m
        except OptimisationError as e:
            log(f"  AI Manager FAILED: {e}")
            detail.append(f"manager_failed={e}")

    if late:
        detail.append("committed_after_deadline")

    # What the bot actually did, pushed to the gameweek channel. Until now this
    # reasoning existed only in a log file - which is a shame, because a bot
    # that takes a -4 hit and explains why is the most interesting thing here.
    #
    # Only when something was genuinely committed on this run: the early return
    # above already covers the fully-committed case, and a run where both
    # optimisations failed has nothing to report but a failure, which belongs
    # in the alerts channel rather than this one.
    if committed_xi or committed_mgr:
        try:
            if ops.notify_once("gameweek", "ai_squad", str(gameweek),
                               social.channel_ai_squad(gameweek, committed_mgr,
                                                       committed_xi)):
                log("  pushed the squad to the gameweek channel")
        except Exception as e:
            log(f"  squad notification FAILED: {e}")

    return pool, detail


def pre_deadline_commit(events, budget=DEFAULT_BUDGET):
    """Commit the AI teams shortly BEFORE each deadline.

    Team news is the whole reason for the timing. Injuries and suspensions get
    confirmed in the day or two before a deadline, and FPL updates a player's
    status as it learns - so a squad chosen early can be built around someone
    who has since been ruled out. Committing inside the final window means the
    optimiser sees the same availability a human manager would, which is also
    what makes the frozen prediction a fair thing to judge later.
    """
    upcoming = gw_clock.imminent_deadlines(events)
    if not upcoming:
        return None
    pool = None
    for gw, _deadline, minutes_left in upcoming:
        log(f"GW{gw}: deadline in {minutes_left} min - committing AI teams on the latest team news")
        pool, _ = commit_ai_teams(gw, budget=budget, pool=pool)
        # The gameweek page freezes in the same window and for the same reason:
        # both are claims made on pre-deadline information, and an archive page
        # that kept updating after the deadline would be quietly rewriting
        # advice to match results nobody could have known.
        try:
            freeze_gameweek_report(gw)
        except Exception as e:
            log(f"GW{gw}: freezing the edition FAILED: {e}")
    return pool


def send_deadline_reminders(events):
    """Push a reminder about a day before each deadline, and again about two
    hours before.

    Both windows are wider than the hourly poll, so this is called repeatedly
    inside each one - `notify_once` is what makes that produce one message.
    Keyed on (kind, gameweek), so the two reminders for one gameweek are
    independent and a double gameweek gets its own pair.

    The briefing is read in so the message can carry the top captain pick and
    the injury flags. Absent - which it is before the first edition of a
    season - the reminder still goes, just shorter.
    """
    sent = 0
    for kind, gw, _deadline, hours in gw_clock.reminders_due(events):
        report = None
        try:
            rec = db.get_gw_report(gw)
            report = rec["payload"] if rec else None
        except Exception:
            pass
        label = (report or {}).get("deadline_label")
        message = social.channel_deadline_reminder(kind, gw, label, hours, report)
        if ops.notify_once("gameweek", f"deadline_{kind}", str(gw), message):
            log(f"GW{gw}: {kind} deadline reminder pushed ({hours}h out)")
            sent += 1
    return sent


def deadline_watch(budget=DEFAULT_BUDGET, force_gameweek=None):
    """Hourly. Three phases:

      1. About a DAY before a deadline - rebuild the gameweek briefing and mark
         it postable. This is when managers are actually making transfers, so
         it's when a link to the page is worth posting.
      2. Just BEFORE a deadline - commit the AI squads, as late as the poll
         interval allows, so they reflect the latest injury news, and freeze the
         briefing on the same information.
      3. Just AFTER one - capture real managers' picks, replace their drafts
         with the official team, and backfill anything the pre-commit missed
         (e.g. the box was down when the window passed).
    """
    db.init_db()
    events = gw_clock.get_events()
    if not events:
        log("No events from the FPL API (offline?) - nothing to do.")
        return 1

    # First, because it's the phase with the most slack: a preview that runs a
    # few minutes late is still a day early, whereas the commit window below is
    # 100 minutes wide and shouldn't be queued behind a full report rebuild.
    # Never fatal - a failed preview must not cost the deadline handling.
    try:
        preview_gameweek_report(events)
    except Exception as e:
        log(f"preview edition FAILED: {e}")

    # After the preview above, so a day-out reminder can quote the edition that
    # run has just published rather than yesterday's. Never fatal - a missed
    # notification must not cost the deadline handling below.
    try:
        send_deadline_reminders(events)
    except Exception as e:
        log(f"deadline reminders FAILED: {e}")

    pool = pre_deadline_commit(events, budget=budget)

    if force_gameweek is not None:
        pending = [(int(force_gameweek), None, True)]
    else:
        pending = gw_clock.newly_passed_deadlines(
            events, is_processed=db.deadline_is_processed)

    if not pending:
        log("No unprocessed deadlines.")
        return 0

    position_dfs = None
    for gw, deadline, fresh in pending:
        if not fresh:
            # Too old to reconstruct honestly: predictions have already rolled
            # forward to later fixtures, so a "GW{gw}" squad built now wouldn't
            # be one. Record it so we stop retrying every hour.
            log(f"GW{gw}: deadline passed >24h ago, marking skipped (can't rebuild honestly)")
            db.mark_deadline_processed(gw, deadline, "skipped", "stale: predictions rolled forward")
            continue

        log(f"GW{gw}: deadline passed - capturing real teams")
        if position_dfs is None:
            pool, position_dfs = _rated_pool()

        # Normally a no-op: the pre-deadline phase already committed these.
        # This is the safety net for a window the app slept through, and it
        # flags itself as late so a post-deadline squad is never mistaken for
        # one chosen on pre-deadline information.
        pool, detail = commit_ai_teams(gw, budget=budget, pool=pool, late=True)

        n, replaced = snapshot_managers(gw, position_dfs, next_gameweek=gw + 1)
        detail.append(f"managers={n}; drafts_replaced={replaced}")
        db.mark_deadline_processed(gw, deadline, "processed", "; ".join(detail))

    ping_refresh()
    return 0


def refresh_season_stats():
    """Pull this season's per-gameweek player rows into the current season's
    gameweek_stats.csv.

    This is what flips the app from preseason ratings (last season's averages)
    to inseason ones. Slow - roughly one API call per player - which is why it
    lives in the nightly job.
    """
    from pipeline import run_pipeline as _run
    data = _run()
    ids = data["players_df"]["id"]
    result = refresh_gameweek_stats(ids)
    if result.get("written"):
        gws = result.get("gameweeks") or []
        log(f"  gameweek stats: {result['rows']:,} rows for {result['season']}"
            f" (GW{gws[0]}-{gws[-1]})" if gws else "")
    else:
        log(f"  gameweek stats: {result.get('detail')}")
    return result


def _team_lookups():
    """(names, shorts, codes) keyed on FPL team id, or empty dicts.

    `codes` is the separate one worth explaining: `id` is the team's number for
    this season and is what fixtures and player rows reference, while `code` is
    the club's permanent number and is what the kit SVGs are drawn from. A page
    needs both, and using one where the other belongs draws the wrong shirt
    rather than raising anything."""
    try:
        from fetch_data import get_bootstrap_data
        teams = get_bootstrap_data()["teams"]
        return ({t["id"]: t["name"] for t in teams},
                {t["id"]: t["short_name"] for t in teams},
                {t["id"]: t.get("code") for t in teams})
    except Exception as e:
        log(f"  club names unavailable ({e}); the page will omit them")
        return {}, {}, {}


def _page_index(position_dfs):
    """Player page records with club names attached.

    Same two-step the app does: names come from a lookup that can hit the
    network, so a failure there costs the club names rather than the whole
    index. A report with a blank team column is still worth publishing."""
    pages = player_pages.build_index(position_dfs)
    names, shorts, _codes = _team_lookups()
    return player_pages.attach_team_names(pages, names, shorts)


def build_gameweek_report(force_gameweek=None, position_dfs=None, events=None,
                          stage=None):
    """Regenerate the current gameweek's edition of /gameweek/<n>.

    Nightly. Rewrites in place until the deadline, at which point the edition
    is frozen and this becomes a no-op for that gameweek - so this can run
    every night of the season without ever disturbing a published archive.

    Which gameweek: the NEXT one, not the current one. The page is read by
    people deciding transfers before a deadline, so its subject is the round
    being picked, not the round being played.

    `stage` promotes the edition - "preview" when the deadline is about a day
    out, which is what makes the drafts postable. It only ever moves forward:
    the nightly rebuild passes None and inherits whatever stage the edition had
    reached, because otherwise Saturday's 03:15 run would demote Friday's
    preview back to "don't post this yet" hours after you'd posted it."""
    db.init_db()
    events = events if events is not None else gw_clock.get_events()

    gw = force_gameweek if force_gameweek is not None else gw_clock.next_gameweek(events)
    if gw is None:
        log("No upcoming gameweek (season over, or the API is unreachable) - nothing to build.")
        return 0

    existing = db.get_gw_report(gw)
    if existing and existing["frozen"]:
        log(f"GW{gw}: edition is frozen - leaving it alone.")
        return 0

    if position_dfs is None:
        _pool, position_dfs = _rated_pool()
    pages = _page_index(position_dfs)
    if not pages:
        log(f"GW{gw}: no rated player pool - nothing to report on.")
        return 1

    # The rotation table is the one input that can fail on its own (it needs
    # fixtures and team strength). The other three sections don't depend on it,
    # so a failure here costs one section rather than the edition.
    rotation_df = None
    try:
        from fixture_rotator import get_rotation_data
        rotation_df = get_rotation_data(mode=gw_clock.detect_mode(),
                                        n_gameweeks=gw_report_builder.FIXTURE_HORIZON + 2)
    except Exception as e:
        log(f"  rotation table unavailable ({e}); skipping the fixtures section")

    deadline = None
    for ev in events or []:
        if ev.get("id") == gw:
            deadline = ev.get("deadline_time")
            break

    # Early in a season the ratings stand on last season's average, which
    # bunches every projection together; the armband is left out entirely for
    # as long as that's true. Asked here rather than inferred from the mode so
    # this agrees with what the pipeline actually did.
    ratings_provisional = rating_model.using_fallback_form()
    if ratings_provisional:
        log("  ratings still on last season's average - omitting the armband")

    report = gw_report_builder.build(
        pages, gw, rotation_df=rotation_df, deadline=deadline,
        season_label=seasons.current_season(),
        ratings_provisional=ratings_provisional)

    # Carry the stage forward from whatever is already stored, then apply any
    # promotion this run is asking for. Never backwards - see the docstring.
    previous_stage = social.stage_of(existing["payload"] if existing else None)
    report["stage"] = social.advance_stage(previous_stage, stage or "draft")

    written = db.save_gw_report(gw, report, deadline_time=deadline)
    if not written:
        log(f"GW{gw}: edition became frozen mid-run - not written.")
        return 0

    counts = (f"form={len(report['in_form'])} diff={len(report['differentials'])} "
              f"attack={len(report['attack_runs'])} defence={len(report['defence_runs'])} "
              f"news={len(report['news'])}")
    log(f"GW{gw}: edition rebuilt at stage '{report['stage']}' ({counts})")

    # Drafts are rewritten on every rebuild so the text always matches the page
    # as it currently stands. Whether they're worth posting yet is what the
    # stage in the header tells you.
    try:
        path = social.write_drafts(report, stage=report["stage"])
        log(f"  social drafts ({report['stage']}) -> {path}")
    except Exception as e:
        log(f"  social drafts FAILED (edition is still published): {e}")

    # The weekly outreach email, on the same job for the same reason the player
    # write-up is: the rated pool is already built and building it is the
    # expensive minute of this job. A separate cron line would pay that twice
    # and be a second thing to install.
    #
    # The miss bullet reads the LAST settled gameweek, not this one - this one
    # hasn't been played. get_snapshot returns None when that gameweek was
    # never snapshotted or hasn't been backfilled, and draft_outreach handles
    # None by saying so rather than by omitting the bullet silently.
    try:
        # `pages` is keyed on the season-stable code; get_snapshot wants a flat
        # pool it can index by element id, which is what the values are.
        previous = (ai_team.get_snapshot(gw - 1, pool=list(pages.values()))
                    if gw > 1 else None)
        path = social.write_outreach_draft(report, snapshot=previous)
        log(f"  outreach email -> {path}")
    except Exception as e:
        log(f"  outreach draft FAILED (edition is still published): {e}")

    # Pushed on the TRANSITION to preview, not on being at preview. The nightly
    # rebuild keeps running right up to the deadline and the edition stays at
    # that stage the whole time, so notifying on the state rather than the
    # change would send this every night for a week. The ledger would catch it
    # anyway; the check makes the intent explicit at the point it matters.
    if report["stage"] == "preview" and previous_stage != "preview":
        if ops.notify_once("drafts", "briefing_ready", str(gw),
                           social.channel_briefing_ready(report)):
            log("  pushed to the drafts channel - the briefing is postable")

    ping_refresh()
    return 0


def nightly(force_gameweek=None, stage=None):
    """The 03:15 job: rebuild the briefing, then write tonight's player post.

    One function rather than two cron lines, and one rated pool shared between
    them. Building that pool is the expensive part of the night - about a
    minute of four-core CPU - and a second job would run it again for no
    reason.

    The two are sequenced but not coupled. The briefing has several legitimate
    ways to do nothing (its edition is already frozen, the season is over) and
    the player write-up must still run on those nights - which is precisely why
    this is a wrapper rather than a call at the end of `build_gameweek_report`,
    where every one of those early returns would have skipped it silently.
    """
    db.init_db()
    events = gw_clock.get_events()

    # Computed once, here, and handed to both. Neither is given the chance to
    # decide it doesn't need it and quietly recompute.
    position_dfs = None
    try:
        _pool, position_dfs = _rated_pool()
    except Exception as e:
        log(f"rating the pool FAILED: {e}")
        return 1

    rc = 0
    try:
        rc = build_gameweek_report(force_gameweek=force_gameweek,
                                   position_dfs=position_dfs, events=events,
                                   stage=stage)
    except Exception:
        traceback.print_exc()
        rc = 1

    # Never fatal, and never allowed to change the exit code. The briefing is
    # what this job exists for; a night with no player worth writing up is a
    # normal outcome and must not make the run look failed to cron.
    try:
        build_player_spotlight(position_dfs=position_dfs, events=events)
    except Exception:
        traceback.print_exc()
        log("player write-up FAILED (the briefing is unaffected)")

    return rc


def build_player_spotlight(force_date=None, position_dfs=None, events=None,
                           replace=False):
    """Tonight's in-depth write-up of one player.

    Runs inside `gameweek-report` rather than on a cron line of its own. Both
    need the rated pool, and computing it is the expensive part of the night -
    roughly a minute of four-core CPU - so a separate job would double the
    machine's nightly load to save nothing.

    Idempotent on the date: `db.save_player_post` refuses to replace a post
    that already exists, so a manual catch-up run doesn't swap out the post you
    already read.

    Never fatal to its caller. A quiet pool that produces no candidate is a
    normal outcome, not a failure.
    """
    db.init_db()
    events = events if events is not None else gw_clock.get_events()

    day = force_date or datetime.date.today()
    if isinstance(day, str):
        day = datetime.date.fromisoformat(day)

    if not replace and db.get_player_post(day.isoformat()):
        log(f"{day}: a player write-up already exists - leaving it alone.")
        return 0

    gw = gw_clock.next_gameweek(events)
    if gw is None:
        log("No upcoming gameweek - nothing to write a player up for.")
        return 0

    if position_dfs is None:
        _pool, position_dfs = _rated_pool()
    pages = _page_index(position_dfs)
    if not pages:
        log("No rated player pool - nothing to write up.")
        return 1

    # The per-gameweek history is what the six in-season detectors read.
    # Without it only the three early-season angles can fire - which is the
    # normal state of the world in August, not a fault, so this failing costs
    # candidates rather than the job.
    history_df = None
    history_detail = ""
    try:
        import pandas as pd
        path = seasons.gameweek_stats_path()
        history_df = pd.read_csv(path)
        log(f"  gameweek history: {len(history_df):,} rows from {path}")
    except Exception as e:
        history_detail = str(e)
        log(f"  gameweek history unavailable ({e}); "
            "only the early-season angles can fire")

    fixtures_df = None
    try:
        from fetch_data import get_fixtures
        fixtures_df = get_fixtures()
    except Exception as e:
        log(f"  fixtures unavailable ({e}); the fixture angle will not fire")

    names, shorts, _codes = _team_lookups()

    candidate = player_spotlight.choose(
        pages, gw, history_df=history_df, fixtures_df=fixtures_df,
        team_names=names, team_shorts=shorts,
        recent_posts=db.recent_player_posts(), today=day)

    if candidate is None:
        # Preseason, an international break, or a fortnight in which everyone
        # interesting has already been written about. Saying so beats
        # publishing the least uninteresting player in the game.
        log(f"{day}: no player clears the bar tonight - nothing written.")

        # ...but say it somewhere a person will see: silence here is
        # indistinguishable from the job not running at all. Keyed on the date,
        # so a manual re-run never sends a second one.
        # The exception text stays in the log and out of the message: it is a
        # path inside the container, which tells a phone nothing and is not
        # something to be posting into a chat channel.
        reason = ("Nothing in the pool cleared the bar tonight. "
                  + ("There is no gameweek history for this season yet, so "
                     "only the early-season angles could run, and none of them "
                     "found anything either."
                     if history_detail else
                     "Every angle came back empty — a quiet week, or everyone "
                     "interesting has been written up in the last fortnight."))
        if ops.notify_once("drafts", "player_quiet", day.isoformat(),
                           social.channel_quiet_night(day.isoformat(), gw, reason)):
            log("  told the drafts channel there is nothing tonight")
        return 0

    post = player_spotlight.build(candidate, pages, gw, today=day,
                                  season_label=seasons.current_season())

    if not db.save_player_post(day.isoformat(), gw, post["code"], post["angle"],
                               post, replace=replace):
        log(f"{day}: another run wrote tonight's post first - not overwriting.")
        return 0

    log(f"{day}: {post['name']} ({post['angle']}, score {post['score']}) "
        f"for GW{gw}")
    try:
        path = social.write_player_drafts(post)
        log(f"  player drafts -> {path}")
    except Exception as e:
        log(f"  player drafts FAILED (the post is still stored): {e}")

    # Pushed to the drafts channel, keyed on the date so a re-run never sends
    # twice. This is what makes the feature a routine rather than a URL to
    # remember: the post is waiting when you wake up.
    if ops.notify_once("drafts", "player_post", day.isoformat(),
                       social.channel_player_post(post)):
        log("  pushed to the drafts channel")
    return 0


def preview_gameweek_report(events, position_dfs=None):
    """Publish the postable preview, about a day before each deadline.

    This is the earlier of the edition's two postable moments. The final
    version freezes 100 minutes out, which is the right time for a page that
    will never change again and the wrong time to be posting a link: by then
    the people it's for have made their transfers. The day before is when they
    are deciding, so the edition is rebuilt on the freshest data and marked
    safe to post while there is still time to act on it.

    Idempotent. The stage only moves forward, so a second run inside the window
    rebuilds the page and rewrites the same drafts rather than undoing
    anything."""
    due = gw_clock.preview_due(events)
    if not due:
        return 0
    for gw, _deadline, hours_left in due:
        log(f"GW{gw}: deadline in {hours_left}h - publishing the postable preview")
        try:
            build_gameweek_report(force_gameweek=gw, position_dfs=position_dfs,
                                  events=events, stage="preview")
        except Exception as e:
            log(f"GW{gw}: preview edition FAILED: {e}")
    return len(due)


def freeze_gameweek_report(gameweek):
    """Make an edition final. Called from the pre-deadline window, so the page
    freezes on the same team news the AI squads were committed on."""
    if db.freeze_gw_report(gameweek):
        log(f"GW{gameweek}: edition frozen - it is now a permanent archive page.")
        rec = db.get_gw_report(gameweek)
        if rec:
            # The stored payload is left saying "preview" - a frozen edition
            # can't be re-saved, which is the whole point of freezing it. The
            # page reads `frozen` first and only falls back to `stage`, so
            # there's nothing to keep in sync; only the drafts need telling.
            try:
                path = social.write_drafts(rec["payload"], stage="final")
                log(f"  social drafts ready to post -> {path}")
            except Exception as e:
                log(f"  social drafts FAILED: {e}")

        # This week's drafts being final is what makes last week's redundant.
        # Never fatal - a full disk is a problem, a failed tidy-up is not, and
        # this must not be the thing that stops an edition freezing.
        try:
            result = retention.on_briefing_frozen(gameweek)
            for line in result["log"]:
                log(line)
        except Exception as e:
            log(f"  cleaning up old briefing drafts FAILED: {e}")
        return True
    return False


def _roundup_scorecard(gameweek):
    """The AI Best XI's frozen projection for this round against what it
    actually scored, or None.

    None whenever either half is missing - a gameweek the box slept through has
    no snapshot, and one whose actuals haven't been backfilled yet has no
    score. Printing half of it would be worse than printing none: "projected
    62.4" with no outcome beside it is the exact shape of a claim nobody can
    check, which is what this section exists to avoid."""
    try:
        snap = ai_team.get_snapshot(gameweek)
    except Exception as e:
        log(f"  scorecard unavailable ({e})")
        return None
    if not snap or snap.get("actual_points") is None or snap.get("predicted_points") is None:
        return None
    predicted = round(float(snap["predicted_points"]), 1)
    actual = int(snap["actual_points"])
    return {"predicted": predicted, "actual": actual,
            "difference": round(actual - predicted, 1)}


def build_gameweek_roundup(force_gameweek=None, position_dfs=None, events=None,
                           replace=False):
    """Write up a settled gameweek: /gameweek/<n>/roundup.

    Runs inside the daily refresh rather than on a cron line of its own. The
    moment it has to hit is "after FPL confirms the round's stats", which is
    the same condition the actual-points backfill already waits for and already
    polls for daily - a second cron entry would be a second thing to keep in
    step with a schedule neither of them controls.

    Written once and never rewritten. `db.save_gw_roundup` refuses to overwrite,
    so this being called every night for the rest of the season is free.
    """
    db.init_db()
    events = events if events is not None else gw_clock.get_events()

    gw = (force_gameweek if force_gameweek is not None
          else gw_clock.latest_finished_gameweek(events))
    if gw is None:
        log("No settled gameweek to write up yet.")
        return 0

    if not replace and db.get_gw_roundup(gw) is not None:
        log(f"GW{gw}: roundup already written - leaving it alone.")
        return 0

    if position_dfs is None:
        _pool, position_dfs = _rated_pool()
    pages = _page_index(position_dfs)
    if not pages:
        log(f"GW{gw}: no rated player pool - nothing to write up.")
        return 1

    # The round's settled per-player stats. Without these the two player
    # sections are empty and the page is just results, which is thin but still
    # worth publishing - so this failing is not fatal.
    live_stats = {}
    try:
        live_stats = gw_clock.get_event_live_stats(gw)
        log(f"  GW{gw} live stats: {len(live_stats)} players")
    except Exception as e:
        log(f"  live stats unavailable ({e}); the player sections will be empty")

    # Fixtures drive the table, the shocks and the runs. Same deal - a failure
    # here costs two sections rather than the page.
    fixtures_df = None
    try:
        from fetch_data import get_fixtures
        fixtures_df = get_fixtures()
    except Exception as e:
        log(f"  fixtures unavailable ({e}); skipping the results sections")

    names, _shorts, codes = _team_lookups()

    roundup = gw_roundup_builder.build(
        pages, gw, live_stats=live_stats, fixtures_df=fixtures_df,
        team_names=names, team_codes=codes,
        season_label=seasons.current_season(),
        scorecard=_roundup_scorecard(gw))

    if not db.save_gw_roundup(gw, roundup, replace=replace):
        log(f"GW{gw}: a roundup was written by another run - not overwriting.")
        return 0

    counts = (f"scorers={len(roundup['top_scorers'])} "
              f"blanks={len(roundup['underperformers'])} "
              f"shocks={len(roundup['shocks'])} runs={len(roundup['momentum'])}")
    log(f"GW{gw}: roundup published ({counts})")

    try:
        path = social.write_roundup_drafts(roundup)
        log(f"  roundup social drafts -> {path}")
    except Exception as e:
        log(f"  roundup social drafts FAILED (the page is still published): {e}")

    # The roundup existing is what makes two other things historical: the
    # previous roundup's drafts, and every nightly player write-up arguing
    # about who to pick for a round that has now been played.
    try:
        result = retention.on_roundup_saved(gw)
        for line in result["log"]:
            log(line)
    except Exception as e:
        log(f"  cleaning up after the roundup FAILED: {e}")

    if ops.notify_once("drafts", "roundup_ready", str(gw),
                       social.channel_roundup_ready(roundup)):
        log("  pushed to the drafts channel")

    ping_refresh()
    return 0


def daily_refresh(skip_stats=False):
    """Once daily, late. Heavy pipeline rerun plus backfilling real scores onto
    frozen snapshots for any gameweek FPL has now finalised."""
    db.init_db()
    events = gw_clock.get_events()

    # Enforce the retention period the privacy policy publishes. Doing this in
    # the nightly job is what makes that promise true rather than aspirational.
    try:
        purged = db.purge_expired()
        if purged["manager_gameweeks"] or purged["drafts"] or purged["known_managers"]:
            log(f"  retention: removed {purged['manager_gameweeks']} gameweek(s), "
                f"{purged['drafts']} draft(s), {purged['known_managers']} id(s) "
                f"older than {db.RETENTION_MONTHS} months")
    except Exception as e:
        log(f"  retention purge FAILED: {e}")

    if not skip_stats:
        try:
            refresh_season_stats()
        except Exception as e:
            # Never let a slow scrape stop the backfill below from running.
            log(f"  gameweek stats refresh FAILED: {e}")

    for snap in ai_team.list_snapshots():
        gw = snap["gameweek"]
        if snap["actual_points"] is not None:
            continue
        res = ai_team.backfill_actuals(gw, events)
        if res.get("updated"):
            log(f"GW{gw}: AI Best XI scored {res['actual_points']} "
                f"(predicted {res['predicted_points']})")
            m = manager_history.backfill_manager_actuals(gw, events)
            log(f"GW{gw}: backfilled {m['updated']} manager picks")
        else:
            log(f"GW{gw}: not backfilled - {res.get('reason')}")

    # After the backfill, deliberately. The roundup's scorecard section reads
    # the snapshot's actual_points, which the loop above is what writes - build
    # it first and the one number on the page that costs something to publish
    # would be missing from every roundup by exactly one day.
    #
    # Never fatal. The backfill above is the job's real work and has already
    # committed; a failure writing up the round must not make this run look
    # like it did nothing.
    try:
        build_gameweek_roundup(events=events)
    except Exception as e:
        log(f"  gameweek roundup FAILED: {e}")

    daily_maintenance()

    ping_refresh()
    return 0


def daily_maintenance():
    """Back up the database and tidy the volume.

    Both hang off the daily refresh rather than a cron line of their own,
    because each wants to happen once a day and a separate entry is another
    thing that can silently not be installed.

    The staleness check is deliberately NOT here - it has its own 05:00 cron
    line, after every nightly job has run. See `check_staleness`.

    Every step is independently guarded. This is housekeeping attached to the
    end of the job that does the real work, and none of it is allowed to change
    whether that job succeeded.
    """
    try:
        result = ops.backup_database()
        if result.get("written"):
            log(f"  backup -> {result['path']} ({result['bytes'] / 1024:.0f} KB, "
                f"{result['removed']} old copy/copies removed)")
        else:
            log(f"  backup skipped: {result.get('detail')}")
    except Exception as e:
        log(f"  backup FAILED: {e}")

    try:
        pruned = ops.prune_job_runs()
        if pruned:
            log(f"  job history: removed {pruned} old row(s)")
    except Exception as e:
        log(f"  pruning job history FAILED: {e}")

    try:
        result = ops.prune_old_element_summaries()
        if result.get("removed"):
            log(f"  disk: removed {result['removed']} cached API responses from "
                f"{', '.join(result['seasons'])}")
    except Exception as e:
        log(f"  pruning old caches FAILED: {e}")


def check_staleness():
    """Alert on jobs that should have succeeded by now and haven't.

    Its own entry point rather than a phase of another job, because the one
    thing it must never do is judge a job that has not had its turn yet - and
    it cannot promise that from inside a job that runs at 03:00. Whatever calls
    this has to be the last thing of the night.

    Returns 0 whether or not anything was overdue. Overdue jobs are what it
    reports, not a failure of this check - exiting non-zero on them would make
    the staleness checker itself go stale in the very table it reads.
    """
    try:
        stale = ops.stale_jobs()
        if stale:
            for row in stale:
                log(f"  OVERDUE: {row['job']} "
                    + ("has never completed successfully" if row["never"]
                       else f"last succeeded {row['overdue_hours']}h ago"))
            if ops.alert_if_stale():
                log("  alert sent")
            elif ops.webhook_url():
                log("  alert FAILED to send")
            else:
                log("  no FPL_ALERT_WEBHOOK set - nothing was pushed")
        else:
            log("  all scheduled jobs are up to date")
    except Exception as e:
        log(f"  staleness check FAILED: {e}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("deadline-watch", help="hourly: freeze snapshots on a passed deadline")
    watch.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    watch.add_argument("--gameweek", type=int, default=None,
                       help="force a specific gameweek (testing / manual catch-up)")

    daily = sub.add_parser("daily-refresh", help="daily: refresh season stats, backfill actual scores")
    daily.add_argument("--skip-stats", action="store_true",
                       help="skip the slow per-player gameweek stats pull")
    sub.add_parser("refresh-stats", help="pull this season's gameweek stats only")
    sub.add_parser("init-db", help="create the SQLite file and schema, then exit")

    report = sub.add_parser("gameweek-report",
                            help="nightly: rebuild the current /gameweek page")
    report.add_argument("--gameweek", type=int, default=None,
                        help="force a specific gameweek (testing / manual catch-up)")
    report.add_argument("--stage", choices=("preview", "final"), default=None,
                        help="promote the edition. Normally done automatically - "
                             "'preview' about a day before the deadline, 'final' "
                             "when the hourly watcher freezes it - so this is for "
                             "publishing a postable version by hand.")

    roundup = sub.add_parser(
        "gameweek-roundup",
        help="write up the most recently settled gameweek (normally runs "
             "inside daily-refresh)")
    roundup.add_argument("--gameweek", type=int, default=None,
                         help="force a specific gameweek. Note that ownership "
                              "figures come from today's data, so writing up an "
                              "old round prints today's ownership under its "
                              "headline - see gameweek.latest_finished_gameweek.")
    roundup.add_argument("--replace", action="store_true",
                         help="overwrite an existing roundup. For one built "
                              "while the API was half-up; nothing scheduled "
                              "passes this.")

    spotlight = sub.add_parser(
        "player-spotlight",
        help="write up one player in depth (normally runs inside "
             "gameweek-report)")
    spotlight.add_argument("--date", default=None,
                           help="force a date, YYYY-MM-DD. Defaults to today.")
    spotlight.add_argument("--replace", action="store_true",
                           help="overwrite an existing post for that date")

    purge = sub.add_parser(
        "purge",
        help="delete superseded drafts and nightly player posts. Normally "
             "happens on its own when a briefing freezes and when a roundup "
             "is saved; this is for catching up after a stretch where the "
             "jobs weren't running.")
    purge.add_argument("--dry-run", action="store_true",
                       help="list what would be removed and remove nothing")

    status = sub.add_parser("status",
                            help="print the job heartbeat and what is overdue")
    status.add_argument("--alert", action="store_true",
                        help="check for overdue jobs and push a summary to "
                             "FPL_ALERT_WEBHOOK, printing nothing else. What "
                             "the 05:00 cron line runs. Deliberately last in "
                             "the night: run any earlier and it judges jobs "
                             "that haven't had their turn yet.")
    status.add_argument("--test-alert", action="store_true",
                        help="send a test message to FPL_ALERT_WEBHOOK and "
                             "report whether it went. The only way to confirm "
                             "alerting is wired up without waiting for "
                             "something to break.")

    args = parser.parse_args(argv)

    # The heartbeat wraps the whole dispatch, so a job records that it ran
    # whichever way it exits - including the traceback path below, which is the
    # one that matters. Only the scheduled commands are tracked: `status` and
    # `init-db` are things a person types, and recording them as job runs would
    # make a hand-run command look like a healthy cron.
    tracked = args.command in ops.JOB_INTERVALS
    run_id = ops.record_start(args.command) if tracked else None

    try:
        rc = _dispatch(args)
    except Exception:
        traceback.print_exc()
        if tracked:
            ops.record_finish(run_id, "failed", "unhandled exception")
            # The push happens here rather than inside the except in _dispatch,
            # so every command gets it without each one remembering to.
            ops.alert(f"{args.command} failed with an unhandled exception. "
                      "Check the job log on the host.")
        return 1

    if tracked:
        ops.record_finish(run_id, "ok" if rc == 0 else "failed",
                          None if rc == 0 else f"exit code {rc}")
        if rc != 0:
            ops.alert(f"{args.command} exited {rc}.")
    return rc


def _dispatch(args):
    """Run the chosen command.

    Split out of main() so the heartbeat and the alerting wrap every branch
    rather than being repeated in each - and, more to the point, so an
    exception raised in ANY of them reaches one place that records the failure
    and pushes it. This deliberately does not catch anything: main() does, and
    a second handler here would swallow the traceback before the alert could
    describe it."""
    if args.command == "deadline-watch":
        return deadline_watch(budget=args.budget, force_gameweek=args.gameweek)
    if args.command == "daily-refresh":
        return daily_refresh(skip_stats=args.skip_stats)
    if args.command == "refresh-stats":
        refresh_season_stats()
        return 0
    if args.command == "init-db":
        log(f"Initialised {db.init_db()}")
        return 0
    if args.command == "gameweek-report":
        # "final" by hand means freeze it: the stage and the frozen flag are
        # the same statement, and setting one without the other would give a
        # page that says it will never change and a job that keeps rewriting it.
        if args.stage == "final":
            rc = build_gameweek_report(force_gameweek=args.gameweek)
            gw = args.gameweek or gw_clock.next_gameweek()
            if gw is not None:
                freeze_gameweek_report(gw)
            return rc
        return nightly(force_gameweek=args.gameweek, stage=args.stage)
    if args.command == "gameweek-roundup":
        return build_gameweek_roundup(force_gameweek=args.gameweek,
                                      replace=args.replace)
    if args.command == "player-spotlight":
        return build_player_spotlight(force_date=args.date,
                                      replace=args.replace)
    if args.command == "purge":
        db.init_db()
        log("Retention sweep" + (" (dry run)" if args.dry_run else ""))
        for line in retention.sweep(dry_run=args.dry_run)["log"]:
            log(line)
        return 0
    if args.command == "status":
        if args.test_alert:
            return send_test_alert()
        if args.alert:
            db.init_db()
            return check_staleness()
        return print_status()
    return 0


# Every key in ops.CHANNELS needs a line here. A channel missing from this map
# still gets tested, but its test message reads "This channel will carry: ."
# with nothing in it - which is exactly the message you least want in the one
# place you go to confirm the wiring is right. `kofi` and `questions` were both
# absent and both did that.
WHAT_EACH_CHANNEL_CARRIES = {
    "alerts": "scheduled jobs that failed or stopped running",
    "drafts": "the nightly player write-up, the briefing, the roundup",
    "gameweek": "deadline reminders, and what the AI Manager did",
    "seo": "the weekly Search Console digest",
    "kofi": "Ko-fi donations, relayed from the webhook Ko-fi posts to",
    # Worth spelling out that this one is the storage, because it is the only
    # channel where a missed message is a lost message rather than a lost copy.
    "questions": ("questions typed into the box on /faq - nothing is stored, "
                  "so this channel is the only record"),
}


def send_test_alert():
    """Send a test message to every configured channel.

    Worth its own flag because every other path to this code needs something to
    have gone wrong first, and "set the variable and hope" is how notifications
    end up configured wrong and discovered during the incident they were meant
    to catch.

    Sends to each channel separately rather than once, because the thing most
    likely to be wrong is not the URL - it is a webhook pointing at the wrong
    CHANNEL, which no amount of checking the variable can reveal. Four messages
    arriving in four channels is the only way to see that.
    """
    configured = ops.configured_channels()
    if not any(state != "off" for state in configured.values()):
        log("No webhooks are configured - nothing to test.")
        log("")
        log("  Set FPL_ALERT_WEBHOOK in .env next to docker-compose.yml on the")
        log("  server, then `docker compose up -d` to recreate the container.")
        log("")
        log("  The compose file has to NAME the variable too - one that .env")
        log("  sets and compose doesn't list never reaches the container, and")
        log("  the failure is silent.")
        return 1

    failures = 0
    for channel, state in configured.items():
        carries = WHAT_EACH_CHANNEL_CARRIES.get(channel, "")
        if state == "off":
            log(f"  {channel:<9} not configured")
            continue
        via = "own webhook" if state == "own" else "falling back to FPL_ALERT_WEBHOOK"
        url = ops.webhook_url(channel)
        # The URL is a credential - anyone holding it can post to the channel -
        # so only the tail is printed. The tail rather than the head, because
        # every Discord webhook begins with the same 33 characters: showing the
        # start distinguishes nothing.
        shown = "…" + url[-12:] if len(url) > 12 else url
        ok, detail = ops.send(channel,
                              f"✅ **Test message — `{channel}`**\n"
                              f"This channel will carry: {carries}.")
        if ok:
            log(f"  {channel:<9} sent   ({via}, {shown})")
        else:
            log(f"  {channel:<9} FAILED ({via}, {shown})")
            log(f"            {detail}")
            failures += 1

    log("")
    if failures:
        log(f"{failures} channel(s) failed. Check the URL, and that the")
        log("container has outbound network access.")
        return 1
    log("Check each channel. If two messages landed in the same place, two")
    log("variables are pointing at the same webhook.")
    return 0


def print_status():
    """What ran, what didn't, and what is overdue.

    The command to type when the site looks stale. It answers the question the
    log files can't: not "did anything happen last night" but "has each job
    succeeded recently enough that the pages should be current".
    """
    db.init_db()
    health = ops.health()

    log("Scheduled jobs")
    log("=" * 46)
    runs = {r["job"]: r for r in health["last_runs"]}
    for job, hours in sorted(ops.JOB_INTERVALS.items()):
        row = runs.get(job)
        success = ops.last_success(job)
        if row is None:
            log(f"  {job:<18} never run          (expected every {hours}h)")
            continue
        log(f"  {job:<18} last run {row['started_at'][:16]} "
            f"[{row['status']}]  last success "
            f"{success[:16] if success else 'never'}")

    if health["stale"]:
        log("")
        log("OVERDUE")
        for row in health["stale"]:
            log(f"  {row['job']}: "
                + ("has never completed successfully" if row["never"]
                   else f"last succeeded {row['overdue_hours']}h ago"))
    else:
        log("")
        log("  All jobs are up to date.")

    # Reported next to the jobs rather than buried, because a green
    # `gameweek-report` says the job ran and says nothing about whether it
    # wrote anything. Those two came apart for a fortnight and this line is
    # what would have shown it.
    post = health.get("player_post") or {}
    log("")
    if post.get("post_date"):
        age = post.get("days_ago")
        log(f"Player write-ups: newest {post['post_date']} "
            f"(GW{post.get('gameweek')}, {post.get('angle')})"
            + (f", {age}d ago" if age else ", today"))
    else:
        log("Player write-ups: none written yet")

    backups = health["backups"]
    log("")
    log(f"Backups: {backups['count']} on disk"
        + (f", newest {backups['latest']}" if backups["latest"] else ""))

    log("")
    log("Discord channels")
    for channel, state in health.get("channels", {}).items():
        label = {"own": "own webhook",
                 "fallback": "via FPL_ALERT_WEBHOOK",
                 "off": "not configured"}.get(state, state)
        log(f"  {channel:<9} {label:<24} "
            f"{WHAT_EACH_CHANNEL_CARRIES.get(channel, '')}")
    log("")
    log("  Test them with: jobs.py status --test-alert")

    # Non-zero when something is wrong, so this is usable from a shell script
    # or another monitor without parsing the text above.
    return 0 if health["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
