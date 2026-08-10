"""Scheduled jobs, run from the Proxmox host's cron (see deploy/README).

    python jobs.py deadline-watch    # hourly
    python jobs.py daily-refresh     # ~03:00 UK

Two jobs rather than one, because they answer to different clocks:

  deadline-watch has to be timing-accurate. FPL deadlines land at irregular
  times and days (midweek rounds, 11:00 Saturday starts, blank/double weeks),
  so there's no daily slot that reliably lands just after one. Hourly polling of
  `deadline_time` catches them all, and the work is cheap enough to do 24x/day.

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
import os
import sys
import traceback

import requests

import ai_manager
import ai_team
import db
import drafts
import gameweek as gw_clock
import manager_history
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
    try:
        position_dfs = get_rated_position_dfs(data["position_dfs"], mode=mode)
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
            view = get_team_view(fpl_id, gameweek, position_dfs)
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

    if not already_xv:
        try:
            r = ai_team.generate_and_store(pool, gameweek, budget=budget)
            log(f"  AI Best-XV: {r['formation']}, £{r['squad_cost']}m, "
                f"{r['predicted_points']} predicted pts")
            detail.append(f"best_xv={r['predicted_points']}")
        except OptimisationError as e:
            log(f"  AI Best-XV FAILED: {e}")
            detail.append(f"best_xv_failed={e}")

    if not already_mgr:
        try:
            m = ai_manager.run_gameweek(pool, gameweek, budget=budget)
            log(f"  AI Manager: {m['formation']}, {len(m['transfers'])} transfer(s), "
                f"chip={m['chip'] or 'none'}, {m['predicted_points']} predicted pts")
            for t in m["transfers"]:
                log(f"    {t['out']} -> {t['in']} (+{t['gain']}"
                    f"{'' if t['free'] else ', -4 hit'})")
            detail.append(f"manager={m['predicted_points']}")
        except OptimisationError as e:
            log(f"  AI Manager FAILED: {e}")
            detail.append(f"manager_failed={e}")

    if late:
        detail.append("committed_after_deadline")
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
    return pool


def deadline_watch(budget=DEFAULT_BUDGET, force_gameweek=None):
    """Hourly. Two phases:

      1. Just BEFORE a deadline - commit the AI squads, as late as the poll
         interval allows, so they reflect the latest injury news.
      2. Just AFTER one - capture real managers' picks, replace their drafts
         with the official team, and backfill anything the pre-commit missed
         (e.g. the box was down when the window passed).
    """
    db.init_db()
    events = gw_clock.get_events()
    if not events:
        log("No events from the FPL API (offline?) - nothing to do.")
        return 1

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
    to inseason ones. Nothing used to call it, and the writer/reader disagreed
    about the path, so 'inseason' was unreachable however deep into the season
    you got. Slow - roughly one API call per player - which is why it lives in
    the nightly job.
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
            log(f"GW{gw}: AI Best-XV scored {res['actual_points']} "
                f"(predicted {res['predicted_points']})")
            m = manager_history.backfill_manager_actuals(gw, events)
            log(f"GW{gw}: backfilled {m['updated']} manager picks")
        else:
            log(f"GW{gw}: not backfilled - {res.get('reason')}")

    ping_refresh()
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

    args = parser.parse_args(argv)
    try:
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
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
