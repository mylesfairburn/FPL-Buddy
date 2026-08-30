"""AI Best XI: generate, freeze and read back the per-gameweek optimum squad.

The tab is stateless by design - each gameweek gets a fresh optimum with no
carry-over, no bank and no transfers. That's what separates it from the AI
Manager (which plays by real rules and reuses manager_team instead).

Snapshots freeze `predicted_points` and `cost` at generation time and are never
recomputed. If the model improves mid-season and we re-derived old snapshots
from it, every "AI predicted X, actually scored Y" comparison in the history
would retroactively change - which would make the tab worthless as a record.
"""

import autosubs
from db import connect, utcnow
from gameweek import (element_index, event_minutes, finished_teams,
                      get_event_live, gameweek_is_finished, started_teams)
from squad_optimiser import (DEFAULT_BUDGET, OptimisationError, optimise_squad,
                             verify)


def build_best_xi(players, gameweek, budget=DEFAULT_BUDGET):
    """Solve for the gameweek's optimum squad and sanity-check it.

    The independent verify() pass is deliberate: a mis-stated constraint would
    otherwise produce an illegal squad that still looks plausible on a pitch."""
    result = optimise_squad(players, gameweek, budget=budget)
    problems = verify(result, budget=budget)
    if problems:
        raise OptimisationError(
            "Optimiser returned an illegal squad: " + "; ".join(problems))
    return result


def save_snapshot(result):
    """Persist a built squad. Re-running for the same gameweek replaces it -
    the watcher is idempotent, and a manual regeneration before kickoff should
    win rather than collide."""
    gw = int(result["gameweek"])
    with connect() as conn:
        conn.execute("DELETE FROM ai_team_snapshot WHERE gameweek = ?", (gw,))
        cur = conn.execute(
            """INSERT INTO ai_team_snapshot
                   (gameweek, formation, budget, squad_cost, predicted_points, generated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (gw, result["formation"], result["budget"], result["squad_cost"],
             result["predicted_points"], utcnow()))
        snapshot_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO ai_team_snapshot_picks
                   (snapshot_id, element_id, position, is_captain, is_vice_captain,
                    cost, predicted_points)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(snapshot_id, p["element_id"], p["position"], int(p["is_captain"]),
              int(p["is_vice_captain"]), p["cost"], p["predicted"])
             for p in result["squad"]])
    return snapshot_id


def _enrich(picks, pool_by_id):
    """Join stored element_ids against the live player pool for display names,
    shirts and fixtures. Cost and predicted points come from the SNAPSHOT, not
    the pool - they're the frozen record of what was true at the deadline."""
    out = []
    for row in picks:
        p = pool_by_id.get(row["element_id"], {})
        out.append({
            "id": row["element_id"],
            "element_id": row["element_id"],
            # The stable cross-season player code, which is what the profile
            # pages are keyed on. Carried so the card can offer a link to one.
            "code": p.get("code"),
            "web_name": p.get("web_name") or f"#{row['element_id']}",
            "pos": p.get("pos"),
            "team": p.get("team"),
            "team_code": p.get("team_code"),
            "team_name": p.get("team_name"),
            "next_gameweeks": p.get("next_gameweeks") or [],
            "cost": row["cost"],
            "predicted": row["predicted_points"],
            "actual_points": row["actual_points"],
            "position": row["position"],
            "starting": row["position"] <= 11,
            "is_captain": bool(row["is_captain"]),
            "is_vice_captain": bool(row["is_vice_captain"]),
            **settled_flags(row),
        })
    return out


def settled_flags(row):
    """The substitution marks a scored pick carries, from its stored multiplier.

    Same three keys live_overlay produces mid-round, so the front end has one
    vocabulary for "this player came on" whether the gameweek is still being
    played or was settled back in August.

    Derived rather than given its own column because the multiplier already says
    it: a starter multiplied by nothing was substituted off, a bench player
    multiplied by anything came on, and whoever was multiplied by two or three
    wore the armband. NULL means the round has not been scored, and every flag
    is False - the squad as picked, which is all there is to show.
    """
    mult = row["effective_multiplier"] if "effective_multiplier" in row.keys() else None
    if mult is None:
        return {"auto_sub_in": False, "auto_sub_out": False, "wore_armband": False}
    started = (row["position"] or 99) <= 11
    return {
        "auto_sub_in": not started and mult > 0,
        "auto_sub_out": started and mult == 0,
        "wore_armband": mult >= 2,
    }


def get_snapshot(gameweek, pool=None):
    """One stored snapshot, display-ready. Returns None if that gameweek was
    never snapshotted (e.g. the app wasn't running when the deadline passed)."""
    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM ai_team_snapshot WHERE gameweek = ?", (int(gameweek),)).fetchone()
        if head is None:
            return None
        picks = conn.execute(
            "SELECT * FROM ai_team_snapshot_picks WHERE snapshot_id = ? ORDER BY position",
            (head["id"],)).fetchall()

    pool_by_id = {p["id"]: p for p in (pool or [])}
    return {
        "gameweek": head["gameweek"],
        "formation": head["formation"],
        "budget": head["budget"],
        "squad_cost": head["squad_cost"],
        "predicted_points": head["predicted_points"],
        "actual_points": head["actual_points"],
        "generated_at": head["generated_at"],
        "squad": _enrich(picks, pool_by_id),
        "stored": True,
        # Deliberately absent. Ratings are recomputed nightly, so deriving one
        # here would describe these players TODAY against a squad picked weeks
        # ago. Unlike cost and predicted points, it isn't frozen in the
        # snapshot, so there is no honest figure to state.
        "team_rating": None,
    }


def list_snapshots():
    """Every stored gameweek, newest first - powers the predicted-vs-actual
    history table."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT gameweek, formation, budget, squad_cost, predicted_points,
                      actual_points, generated_at
               FROM ai_team_snapshot ORDER BY gameweek DESC""")]


def snapshots_awaiting_actuals():
    """Snapshot gameweeks the backfill still has work to do on.

    The Best XI half of manager_history.gameweeks_awaiting_actuals, and it
    exists for the same reason: the nightly loop skipped any snapshot that
    already had a total, so a snapshot scored by a build that predates the
    substitution rules could never be revisited. `effective_multiplier` is the
    marker - written by the same pass that writes the total, never NULL
    afterwards - so this heals a gameweek exactly once and then leaves it alone.
    """
    with connect() as conn:
        return [int(r["gameweek"]) for r in conn.execute(
            """SELECT DISTINCT s.gameweek
               FROM ai_team_snapshot s
               LEFT JOIN ai_team_snapshot_picks p ON p.snapshot_id = s.id
               WHERE s.actual_points IS NULL OR p.effective_multiplier IS NULL
               ORDER BY s.gameweek""")]


def clear_settled_scoring(gameweek=None):
    """Forget that a gameweek was ever scored, so the next backfill redoes it.

    The manual counterpart to the two `*_awaiting_actuals` queries above, for
    the case they deliberately don't cover: a scoring change that lands AFTER
    every settled round already carries an `effective_multiplier`. The marker
    has done its job by then and those rounds are quiet, which is what it is
    for - so re-running them has to be something a person asks for, not
    something the schedule decides.

    Only nulls the derived figures. Picks, costs and the frozen predictions are
    untouched, so nothing that makes the track record a record is at risk.
    """
    where, args = ("", [])
    if gameweek is not None:
        where, args = " WHERE gameweek = ?", [int(gameweek)]
    with connect() as conn:
        conn.execute(f"UPDATE ai_team_snapshot SET actual_points = NULL{where}", args)
        conn.execute(
            "UPDATE ai_team_snapshot_picks SET effective_multiplier = NULL"
            + (" WHERE snapshot_id IN (SELECT id FROM ai_team_snapshot WHERE gameweek = ?)"
               if gameweek is not None else ""), args)
        conn.execute(f"UPDATE manager_team SET points = NULL{where}", args)
        conn.execute(
            "UPDATE manager_team_picks SET effective_multiplier = NULL"
            + (" WHERE manager_team_id IN (SELECT id FROM manager_team WHERE gameweek = ?)"
               if gameweek is not None else ""), args)
    return {"cleared": True, "gameweek": gameweek}


def _scoring_squad(picks, index=None):
    """The minimum a stored pick row needs to be run through the sub rules.

    Position and club aren't in either picks table - they were never needed to
    add up an eleven that always played - so they come from bootstrap-static
    rather than from the rated pool. See gameweek.element_index for why: this
    runs on the hourly watcher, which has no pool and shouldn't build one.
    """
    index = index if index is not None else element_index()
    out = []
    for row in picks:
        meta = index.get(row["element_id"], {})
        out.append({
            "id": row["element_id"],
            "pos": meta.get("pos"),
            "team": meta.get("team"),
            "position": row["position"],
            "starting": row["position"] <= 11,
            "is_captain": bool(row["is_captain"]),
            "is_vice_captain": bool(row["is_vice_captain"]),
        })
    return out


def backfill_actuals(gameweek, events=None):
    """Write real scores onto a frozen snapshot once the gameweek is settled.

    Gated on `data_checked`, not just `finished`: FPL's bonus points aren't
    final until that flag flips, so backfilling earlier records provisional
    totals that later change.

    Scored the way a real entry is: automatic substitutions first, then the
    captain's double on whoever ended up wearing the armband. The round is
    settled by the time this runs, so every fixture is decided and no
    substitution here is a guess."""
    gw = int(gameweek)
    if not gameweek_is_finished(gw, events):
        return {"updated": False, "reason": "gameweek not finished / stats not yet checked"}

    live = get_event_live(gw)
    if not live:
        return {"updated": False, "reason": "no live data available"}

    # Both HTTP calls, both made before the connection is opened rather than
    # inside it - there is no reason to hold a write connection open across two
    # network round trips.
    minutes, index = event_minutes(gw), element_index()

    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM ai_team_snapshot WHERE gameweek = ?", (gw,)).fetchone()
        if head is None:
            return {"updated": False, "reason": f"no snapshot stored for GW{gw}"}
        picks = conn.execute(
            "SELECT * FROM ai_team_snapshot_picks WHERE snapshot_id = ?",
            (head["id"],)).fetchall()

        squad = _scoring_squad(picks, index)
        result = autosubs.apply(squad, minutes)
        total = autosubs.score(squad, live, result)
        mults = autosubs.multipliers(squad, result)
        # The multiplier is written for every pick, the score only for the ones
        # FPL reported. A player absent from the live feed still has a place in
        # the lineup that was scored, and stating it is what lets the pitch
        # explain the total; inventing a nought for him would not.
        updates = [(live.get(row["element_id"]), mults.get(row["element_id"]),
                    row["id"]) for row in picks]

        conn.executemany(
            "UPDATE ai_team_snapshot_picks SET actual_points = COALESCE(?, actual_points), "
            "effective_multiplier = ? WHERE id = ?", updates)
        conn.execute("UPDATE ai_team_snapshot SET actual_points = ? WHERE id = ?",
                     (total, head["id"]))

    return {"updated": True, "gameweek": gw, "actual_points": total,
            "predicted_points": head["predicted_points"],
            "players_scored": sum(1 for row in picks if row["element_id"] in live),
            "auto_subs": len(result["subs"])}


def live_overlay(squad, gameweek, events=None, chip=None):
    """Live scores for a squad whose gameweek is still being played.

    The deliberate opposite number to backfill_actuals above, and the contrast
    between them is the point. That one WRITES, and is gated on `data_checked`
    because bonus points are not final before it - a stored figure has to be the
    settled one forever, or the published predicted-vs-actual record stops
    meaning anything. This one never writes. It exists because a squad sitting
    on a page mid-Saturday showed nothing at all until the following night's
    cron, which is the one afternoon anybody wants to look at it.

    So: the pitch gets provisional numbers and says they are provisional, and
    the track-record tables keep reading the settled ones. Nothing here touches
    the database.

    Returns None when there is nothing to overlay - the gameweek is settled (the
    stored numbers are already the real ones), or FPL couldn't be reached.
    Otherwise {"squad": [...], "points": int, "provisional": True}.

    Only players whose match has actually kicked off are scored. Everyone else
    keeps `actual_points: None`, which is what the front end reads as "show me
    his fixture, not a nought".

    Scoring matches backfill_actuals: automatic substitutions, then the
    captain's double on whoever the armband ended up with. Chips extend that
    where the caller passes one - the AI Manager plays them, the Best XI does
    not.

    Substitutions are only applied for clubs whose fixtures have FINISHED, which
    is the difference between modelling them and guessing at them. Nought
    minutes at half time is a substitute who may yet come on; nought minutes at
    full time is a player who did not play. Waiting for the whistle means the
    eleven on screen changes once, when the reason for the change is visible,
    rather than shuffling itself all afternoon.
    """
    if gameweek_is_finished(gameweek, events):
        return None
    started = started_teams(gameweek)
    if started is None:
        return None
    live = get_event_live(gameweek)
    if not live:
        return None

    result = autosubs.apply(squad, event_minutes(gameweek),
                            decided_teams=finished_teams(gameweek), chip=chip)
    total = 0
    subbed_in = {autosubs.player_id(s["in"]) for s in result["subs"]}
    subbed_out = {autosubs.player_id(s["out"]) for s in result["subs"]}

    out = []
    for p in squad:
        pid = p.get("id", p.get("element_id"))
        team = p.get("team")
        kicked_off = team is None or int(team) in started
        pts = live.get(pid) if kicked_off else None
        out.append({**p,
                    "actual_points": pts if pts is not None
                                     else p.get("actual_points"),
                    # Flagged rather than reordered. Moving a substitute onto
                    # the pitch would redraw the formation under the reader
                    # mid-round; saying which two swapped leaves the squad they
                    # picked recognisable and still explains the total.
                    "auto_sub_in": pid in subbed_in,
                    "auto_sub_out": pid in subbed_out,
                    "wore_armband": pid == result["captain_id"]})
        if pts is None:
            continue
        counts = chip == "bboost" or pid in {autosubs.player_id(s)
                                             for s in result["starters"]}
        if not counts:
            continue
        multiplier = 1
        if pid == result["captain_id"]:
            multiplier = 3 if chip == "3xc" else 2
        total += pts * multiplier

    return {"squad": out, "points": total, "provisional": True,
            "auto_subs": [{"out": s["out"].get("web_name"),
                           "in": s["in"].get("web_name")} for s in result["subs"]]}


def generate_and_store(players, gameweek, budget=DEFAULT_BUDGET):
    """Build + persist in one step - what the deadline watcher calls."""
    result = build_best_xi(players, gameweek, budget=budget)
    save_snapshot(result)
    return result
