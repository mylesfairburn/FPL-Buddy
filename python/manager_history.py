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

from db import connect, utcnow
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


def backfill_manager_actuals(gameweek, events=None):
    """Write real per-player scores onto stored manager picks once the
    gameweek's stats are final. Same data_checked gate as the AI snapshot."""
    gw = int(gameweek)
    if not gameweek_is_finished(gw, events):
        return {"updated": 0, "reason": "gameweek not finished / stats not yet checked"}
    live = get_event_live(gw)
    if not live:
        return {"updated": 0, "reason": "no live data available"}

    updated = 0
    with connect() as conn:
        teams = conn.execute("SELECT id FROM manager_team WHERE gameweek = ?", (gw,)).fetchall()
        for t in teams:
            picks = conn.execute(
                "SELECT id, element_id FROM manager_team_picks WHERE manager_team_id = ?",
                (t["id"],)).fetchall()
            rows = [(live[p["element_id"]], p["id"]) for p in picks
                    if p["element_id"] in live]
            conn.executemany(
                "UPDATE manager_team_picks SET actual_points = ? WHERE id = ?", rows)
            updated += len(rows)
    return {"updated": updated, "gameweek": gw, "teams": len(teams)}
