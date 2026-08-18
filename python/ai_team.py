"""AI Best XI: generate, freeze and read back the per-gameweek optimum squad.

The tab is stateless by design - each gameweek gets a fresh optimum with no
carry-over, no bank and no transfers. That's what separates it from the AI
Manager (which plays by real rules and reuses manager_team instead).

Snapshots freeze `predicted_points` and `cost` at generation time and are never
recomputed. If the model improves mid-season and we re-derived old snapshots
from it, every "AI predicted X, actually scored Y" comparison in the history
would retroactively change - which would make the tab worthless as a record.
"""

from db import connect, utcnow
from gameweek import get_event_live, gameweek_is_finished
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
        })
    return out


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


def backfill_actuals(gameweek, events=None):
    """Write real scores onto a frozen snapshot once the gameweek is settled.

    Gated on `data_checked`, not just `finished`: FPL's bonus points aren't
    final until that flag flips, so backfilling earlier records provisional
    totals that later change. Captain's double counts, and only starters score,
    matching how the predicted figure was built."""
    gw = int(gameweek)
    if not gameweek_is_finished(gw, events):
        return {"updated": False, "reason": "gameweek not finished / stats not yet checked"}

    live = get_event_live(gw)
    if not live:
        return {"updated": False, "reason": "no live data available"}

    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM ai_team_snapshot WHERE gameweek = ?", (gw,)).fetchone()
        if head is None:
            return {"updated": False, "reason": f"no snapshot stored for GW{gw}"}
        picks = conn.execute(
            "SELECT * FROM ai_team_snapshot_picks WHERE snapshot_id = ?",
            (head["id"],)).fetchall()

        total, updates = 0, []
        for row in picks:
            pts = live.get(row["element_id"])
            if pts is None:
                continue
            updates.append((pts, row["id"]))
            if row["position"] <= 11:
                total += pts * (2 if row["is_captain"] else 1)

        conn.executemany(
            "UPDATE ai_team_snapshot_picks SET actual_points = ? WHERE id = ?", updates)
        conn.execute("UPDATE ai_team_snapshot SET actual_points = ? WHERE id = ?",
                     (total, head["id"]))

    return {"updated": True, "gameweek": gw, "actual_points": total,
            "predicted_points": head["predicted_points"], "players_scored": len(updates)}


def generate_and_store(players, gameweek, budget=DEFAULT_BUDGET):
    """Build + persist in one step - what the deadline watcher calls."""
    result = build_best_xi(players, gameweek, budget=budget)
    save_snapshot(result)
    return result
