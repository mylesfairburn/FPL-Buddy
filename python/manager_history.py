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

import autosubs
from db import AI_MANAGER_FPL_ID, connect, utcnow
from gameweek import (element_index, event_minutes, get_event_live,
                      gameweek_is_finished)


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


def _team_points_from_picks(conn, manager_team_id, hits=0, chip=None,
                            minutes=None, index=None):
    """A gameweek score summed from the picks that have just been backfilled.

    Scored by the rules a real entry is scored by rather than by the stored
    multipliers, because the stored multipliers cannot express either of the two
    things that actually decide the total.

    The first is the bench. This used to read `WHERE position <= 11` and lean on
    a comment saying a Bench Boost worked because its bench was stored with a
    multiplier of 1 - which it is, and which that WHERE clause then threw away
    before the multiplier was ever looked at. Every Bench Boost the bot played
    was scored as an ordinary week, the chip spent for nothing.

    The second is automatic substitutions. A starter who did not play is
    replaced by the first bench player who did, and the armband moves to the
    vice-captain if the captain was among them - none of which is knowable from
    a row written before the round was played. See autosubs.

    Returns None when no pick has an actual score yet, so an un-backfilled
    gameweek stays visibly pending rather than being written down as a zero.
    """
    rows = conn.execute(
        "SELECT * FROM manager_team_picks WHERE manager_team_id = ?",
        (int(manager_team_id),)).fetchall()
    scored = {r["element_id"]: int(r["actual_points"]) for r in rows
              if r["actual_points"] is not None}
    if not scored:
        return None

    squad = _pick_squad(rows, index)

    result = autosubs.apply(squad, minutes or {}, chip=chip)
    return autosubs.score(squad, scored, result, chip=chip) - int(hits or 0)


def _record_effective_multipliers(conn, manager_team_id, rows, chip, minutes, index):
    """Store which of the fifteen counted, and what the armband was worth.

    Written for EVERY stored team, not just the ones scored from their picks. A
    real manager's total comes from their own FPL entry - the better source, and
    the one that already includes their substitutions - but the total is not the
    only thing the round has to say. The squad view still has to show which
    eleven produced it, and that is knowable here whichever number won.
    """
    squad = _pick_squad(rows, index)
    result = autosubs.apply(squad, minutes or {}, chip=chip)
    mults = autosubs.multipliers(squad, result, chip=chip)
    conn.executemany(
        "UPDATE manager_team_picks SET effective_multiplier = ? "
        "WHERE manager_team_id = ? AND element_id = ?",
        [(m, int(manager_team_id), pid) for pid, m in mults.items()])
    return result


def _pick_squad(rows, index):
    """Stored pick rows in the shape the substitution rules read.

    Position and club come from bootstrap-static rather than the rated pool -
    see gameweek.element_index. An index that could not be fetched leaves both
    None, which fails the formation check and so substitutes nobody: the old
    behaviour, which is the safe direction to fail in.
    """
    return [{
        "id": r["element_id"],
        "pos": (index or {}).get(r["element_id"], {}).get("pos"),
        "team": (index or {}).get(r["element_id"], {}).get("team"),
        "position": r["position"],
        "starting": (r["position"] or 99) <= 11,
        "is_captain": bool(r["is_captain"]),
        "is_vice_captain": bool(r["is_vice_captain"]),
    } for r in rows]


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
    """Gameweeks with a stored team the backfill still has work to do on.

    Two conditions, not one, and the second is what stops a scoring FIX from
    being unreachable. A gameweek with no total has obviously never been
    scored. But a gameweek that WAS scored under rules that have since been
    corrected also needs revisiting, and "points IS NULL" can never say so - it
    was the only condition here, so the round that most needed rescoring was
    precisely the one this would never look at again.

    `effective_multiplier` is the marker. It is written by the same pass that
    writes the total, so a row missing it was scored by a build that predates
    the substitution rules - and once written it is never NULL again, which is
    what keeps this from re-fetching every settled round of the season on every
    hourly run. Self-healing exactly once per gameweek.

    Oldest first: an older gap is more likely to be a permanent one worth
    logging.
    """
    with connect() as conn:
        return [int(r["gameweek"]) for r in conn.execute(
            """SELECT DISTINCT m.gameweek
               FROM manager_team m
               LEFT JOIN manager_team_picks p ON p.manager_team_id = m.id
               WHERE m.points IS NULL OR p.effective_multiplier IS NULL
               ORDER BY m.gameweek""")]


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

    # One call each, fetched once for the whole sweep rather than per manager.
    # Needed by every team, not just the ones scored from their picks: the
    # substitutions are recorded for all of them - see
    # _record_effective_multipliers.
    minutes = event_minutes(gw)
    index = element_index()

    updated = totals = 0
    mismatches = []
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, fpl_id, active_chip FROM manager_team WHERE gameweek = ?",
            (gw,)).fetchall()
        for t in teams:
            picks = conn.execute(
                "SELECT * FROM manager_team_picks WHERE manager_team_id = ?",
                (t["id"],)).fetchall()
            rows = [(live[p["element_id"]], p["id"]) for p in picks
                    if p["element_id"] in live]
            conn.executemany(
                "UPDATE manager_team_picks SET actual_points = ? WHERE id = ?", rows)
            updated += len(rows)

            _record_effective_multipliers(conn, t["id"], picks, t["active_chip"],
                                          minutes, index)

            # The bot has no FPL entry to ask - fpl_id 0 is synthetic - so its
            # score is its own picks plus the hits it recorded taking. A real
            # manager's own entry is the better source and is always preferred:
            # it is the number on their own team page, and where the two can
            # disagree the API is right by definition.
            reported = None
            if t["fpl_id"] != AI_MANAGER_FPL_ID:
                try:
                    reported = _fpl_gameweek_points(t["fpl_id"], gw)
                except Exception:
                    reported = None

            hits = _ai_hits(conn, gw) if t["fpl_id"] == AI_MANAGER_FPL_ID else 0
            derived = _team_points_from_picks(
                conn, t["id"], hits, chip=t["active_chip"],
                minutes=minutes, index=index)

            # Both computed, so the two can be compared rather than only one of
            # them existing. Our sum is a reconstruction - it re-derives the
            # substitutions and the chip from stored picks - and a
            # reconstruction that disagrees with the source is worth surfacing
            # even though the source is the one we keep. Silently taking the
            # API number would hide a scoring bug for the whole season.
            if reported is not None and derived is not None and reported != derived:
                mismatches.append({"fpl_id": t["fpl_id"], "gameweek": gw,
                                   "fpl": reported, "ours": derived})
            points = reported if reported is not None else derived

            if points is not None:
                conn.execute("UPDATE manager_team SET points = ? WHERE id = ?",
                             (int(points), t["id"]))
                totals += 1

    return {"updated": updated, "totals": totals, "gameweek": gw,
            "teams": len(teams), "mismatches": mismatches}
