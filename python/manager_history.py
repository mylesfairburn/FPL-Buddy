"""Per-gameweek history for real managers.

Populated only for FPL ids someone has actually looked up (see
db.known_manager) - there are ~11M FPL entries and no reason to hold any but
the handful this instance serves.

FPL's own API already exposes a manager's picks for past gameweeks, so this
isn't the only copy of that data. What it adds is the frozen `predicted_points`
per pick: the model's view at the time, which FPL obviously doesn't store and
which can't be reconstructed later once ratings move on. That's what makes
"what we predicted vs what happened" answerable.
"""

from db import AI_MANAGER_FPL_ID, connect, utcnow
from gameweek import get_event_live, gameweek_is_finished


def save_manager_gameweek(fpl_id, gameweek, squad, gw_meta=None):
    """Store one manager's squad for one gameweek.

    `squad` is the same shape team_service.get_team_view returns: dicts with
    id/position/starting/is_captain/is_vice_captain/multiplier/cost/predicted.
    Re-running replaces the row, so a mid-gameweek re-capture (picks can't
    change after the deadline, but points can) just updates in place."""
    gw_meta = gw_meta or {}
    with connect() as conn:
        conn.execute("DELETE FROM manager_team WHERE fpl_id = ? AND gameweek = ?",
                     (int(fpl_id), int(gameweek)))
        cur = conn.execute(
            """INSERT INTO manager_team
                   (fpl_id, gameweek, points, predicted_points, bank, value,
                    active_chip, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(fpl_id), int(gameweek), gw_meta.get("points"),
             gw_meta.get("predicted_points"), gw_meta.get("bank"),
             gw_meta.get("value"), gw_meta.get("active_chip"), utcnow()))
        team_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO manager_team_picks
                   (manager_team_id, element_id, position, is_captain,
                    is_vice_captain, multiplier, cost, predicted_points)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(team_id, p["id"], p.get("position"), int(bool(p.get("is_captain"))),
              int(bool(p.get("is_vice_captain"))), p.get("multiplier", 1),
              p.get("cost"), p.get("predicted"))
             for p in squad])
    return team_id



def manager_history(fpl_id):
    """Every captured gameweek for one manager, with the AI Best XI's numbers
    for the same gameweek alongside - which is the comparison the whole
    snapshot exercise exists to support."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.gameweek, m.points, m.predicted_points, m.bank, m.value,
                      m.active_chip, m.captured_at,
                      a.predicted_points AS ai_predicted,
                      a.actual_points    AS ai_actual
               FROM manager_team m
               LEFT JOIN ai_team_snapshot a ON a.gameweek = m.gameweek
               WHERE m.fpl_id = ?
               ORDER BY m.gameweek DESC""", (int(fpl_id),)).fetchall()
    return [dict(r) for r in rows]


def _ai_hits(conn, gameweek):
    """What the bot paid in points for that gameweek's transfers."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_hit), 0) FROM ai_transfer_log WHERE gameweek = ?",
        (int(gameweek),)).fetchone()
    return int(row[0] or 0)


def _team_points_from_picks(conn, manager_team_id, hits=0):
    """A gameweek score summed from the picks that have just been backfilled.

    Starting eleven only, each pick multiplied by its own multiplier - which is
    what carries the captain's double, the triple captain, and a bench boost
    (where the bench players are stored with a multiplier of 1 rather than 0).

    Returns None when no pick has an actual score yet, so an un-backfilled
    gameweek stays visibly pending rather than being written down as a zero.
    """
    rows = conn.execute(
        """SELECT actual_points, multiplier FROM manager_team_picks
           WHERE manager_team_id = ? AND position <= 11""",
        (int(manager_team_id),)).fetchall()
    scored = [r for r in rows if r["actual_points"] is not None]
    if not scored:
        return None
    return sum(int(r["actual_points"]) * int(r["multiplier"] or 1)
               for r in scored) - int(hits or 0)


def _fpl_gameweek_points(fpl_id, gameweek):
    """A real manager's own score for one gameweek, from FPL.

    Preferred over summing picks for anyone who has an actual FPL entry: it is
    the number on their own team page, and it already accounts for automatic
    substitutions - which the stored picks cannot show, because a sub happens
    after the deadline the picks were captured at.

    The hit is subtracted rather than assumed either way. FPL reports `points`
    and `event_transfers_cost` separately and it is not obvious from one
    gameweek which of the two `points` already includes, so the two candidate
    totals are checked against the cumulative `total_points` FPL also reports
    and whichever reconciles is used. Returns None if neither does, or if the
    entry can't be read - the caller then falls back to the picks.
    """
    from team_service import _get, BASE

    data = _get(f"{BASE}/entry/{int(fpl_id)}/history/")
    rounds = (data or {}).get("current") or []
    if not rounds:
        return None

    row = next((r for r in rounds if r.get("event") == int(gameweek)), None)
    if row is None or row.get("points") is None:
        return None

    gross = int(row["points"])
    hit = int(row.get("event_transfers_cost") or 0)
    if not hit:
        return gross

    # Which reading reconciles with FPL's own running total?
    upto = [r for r in rounds if (r.get("event") or 0) <= int(gameweek)]
    stated = upto[-1].get("total_points")
    if stated is not None:
        net_sum = sum(int(r.get("points") or 0) - int(r.get("event_transfers_cost") or 0)
                      for r in upto)
        gross_sum = sum(int(r.get("points") or 0) for r in upto)
        if net_sum == int(stated):
            return gross - hit
        if gross_sum == int(stated):
            return gross
    return None


def gameweeks_awaiting_actuals():
    """Gameweeks with a stored team whose total is still missing.

    Asked of the manager tables rather than inferred from the Best XI's
    snapshots, so this backfill's work is defined by its own data. Oldest
    first: an older gap is more likely to be a permanent one worth logging.
    """
    with connect() as conn:
        return [int(r["gameweek"]) for r in conn.execute(
            """SELECT DISTINCT gameweek FROM manager_team
               WHERE points IS NULL ORDER BY gameweek""")]


def backfill_manager_actuals(gameweek, events=None):
    """Write real scores onto stored manager picks AND onto the team totals.

    The team total is the half that was missing, and its absence was invisible
    in the obvious place to look: the per-pick scores were being written
    correctly, so the squad view showed every player's real return while the
    track record beside it said "pending" forever. `manager_team.points` is
    what the history API serves, what the AI Manager's cumulative total is
    summed from, and nothing wrote it after capture - at which point the round
    had not been played and it was null by definition.

    Same data_checked gate as the AI snapshot.
    """
    gw = int(gameweek)
    if not gameweek_is_finished(gw, events):
        return {"updated": 0, "reason": "gameweek not finished / stats not yet checked"}
    live = get_event_live(gw)
    if not live:
        return {"updated": 0, "reason": "no live data available"}

    updated = totals = 0
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, fpl_id FROM manager_team WHERE gameweek = ?", (gw,)).fetchall()
        for t in teams:
            picks = conn.execute(
                "SELECT id, element_id FROM manager_team_picks WHERE manager_team_id = ?",
                (t["id"],)).fetchall()
            rows = [(live[p["element_id"]], p["id"]) for p in picks
                    if p["element_id"] in live]
            conn.executemany(
                "UPDATE manager_team_picks SET actual_points = ? WHERE id = ?", rows)
            updated += len(rows)

            # The bot has no FPL entry to ask - fpl_id 0 is synthetic - so its
            # score is its own picks plus the hits it recorded taking. A real
            # manager's own entry is the better source; the picks are the
            # fallback when it can't be reached.
            points = None
            if t["fpl_id"] != AI_MANAGER_FPL_ID:
                try:
                    points = _fpl_gameweek_points(t["fpl_id"], gw)
                except Exception:
                    points = None
            if points is None:
                hits = _ai_hits(conn, gw) if t["fpl_id"] == AI_MANAGER_FPL_ID else 0
                points = _team_points_from_picks(conn, t["id"], hits)

            if points is not None:
                conn.execute("UPDATE manager_team SET points = ? WHERE id = ?",
                             (int(points), t["id"]))
                totals += 1

    return {"updated": updated, "totals": totals, "gameweek": gw,
            "teams": len(teams)}
