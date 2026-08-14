"""The AI Manager: a persistent bot that plays the real game, week to week.

Where AI Best XI re-picks from scratch every gameweek with no consequences,
this one carries a squad forward and lives with its decisions - it has a bank,
a free-transfer count, point hits, and one set of chips for the season. It's
stored as a manager with the reserved sentinel `fpl_id 0`, reusing
manager_team/manager_team_picks rather than a parallel schema.

Two things drive the policy, both of which follow from the fact that a transfer
is permanent while a fixture is not:

  * Players are valued over a HORIZON, not on the next gameweek. Buying the
    best one-week captain and selling him a fortnight later burns 4 points and
    a free transfer. `horizon_value()` weights the coming gameweeks with a
    decay, so a player with three good fixtures beats one with a single great
    one and a wall afterwards.

  * Chips are planned against the whole remaining fixture list and re-planned
    every week. Double gameweeks (a team playing twice) and blank gameweeks (a
    team not playing) are what actually make chips valuable, and the fixture
    list shows both long before they arrive.

Nothing here writes to the real FPL game - there's no authenticated API. It's a
simulation whose picks are recorded so its record can be compared against the
Best XI and against real managers.
"""

from db import AI_MANAGER_FPL_ID, connect, utcnow
from squad_optimiser import (DEFAULT_BUDGET, MAX_PER_CLUB, OptimisationError,
                             SQUAD_QUOTA, availability, optimise_squad,
                             team_rating, verify)

# How far ahead a transfer decision looks, and how quickly later gameweeks stop
# mattering. 0.75 means gameweek n+2 counts for about half of the next one -
# far enough to avoid chasing a single fixture, short enough that the model's
# accuracy (which decays fast) still means something.
HORIZON = 4
HORIZON_DECAY = 0.75

# Buying a player is a season-long commitment, so the SQUAD is chosen over a
# longer view than a transfer is judged on, with a slower decay. This is what
# separates the AI Manager from Best XI: pick the same 15 on next-gameweek
# points and you get Best XI's team, which is the right answer only if you
# intend to rebuild from scratch every week.
SQUAD_HORIZON = 8
SQUAD_DECAY = 0.9
# How hard the long view pulls against this week's XI when choosing who to own.
SQUAD_WEIGHT = 1.0
# Owned players who must have a fixture in each gameweek of the horizon, so a
# blank week can't leave the squad unable to field eleven.
MIN_COVERAGE = 11

# A transfer beyond the free allowance costs 4 points. Only make one if the
# horizon gain clears the hit with room to spare - marginal hits are how FPL
# seasons get quietly destroyed.
HIT_COST = 4.0
HIT_MARGIN = 2.0
# Even a free transfer should do something worthwhile rather than churn.
MIN_FREE_TRANSFER_GAIN = 0.8
MAX_TRANSFERS_PER_WEEK = 2

# Chip thresholds, all in predicted points.
BENCH_BOOST_MIN = 18.0      # bench must be genuinely worth playing
TRIPLE_CAPTAIN_MIN = 9.0    # captain's own projection before tripling
WILDCARD_MIN_GAIN = 12.0    # squad overhaul must beat the current squad by this
FREE_HIT_MIN_GAIN = 15.0    # one-week rental must be worth burning the chip


def horizon_value(player, from_gameweek, horizon=HORIZON, decay=HORIZON_DECAY):
    """Decayed sum of a player's predicted points over the next few gameweeks.

    This is the number transfers are judged on. A double gameweek shows up
    naturally: two fixtures in the same event both contribute, so a player with
    a DGW outscores an equivalent one without ever needing a special case.
    Risk-adjusted, so a doubtful player isn't valued as if he's certain to play.
    """
    avail = availability(player)
    total, weight = 0.0, 1.0
    for gw in range(from_gameweek, from_gameweek + horizon):
        pts = 0.0
        for entry in player.get("next_gameweeks") or []:
            if entry.get("event") == gw and entry.get("points") is not None:
                pts += float(entry["points"])          # += covers double gameweeks
        total += pts * weight
        weight *= decay
    return total * avail


def squad_selection_values(pool, gameweek):
    """{player_id: season-long value} for the ownership decision."""
    return {p["id"]: horizon_value(p, gameweek, horizon=SQUAD_HORIZON, decay=SQUAD_DECAY)
            for p in pool}


def coverage_requirement(gameweek, horizon=SQUAD_HORIZON, minimum=MIN_COVERAGE):
    """Minimum players with a fixture, per gameweek across the horizon."""
    return {gw: minimum for gw in range(gameweek, gameweek + horizon)}


def build_squad(pool, gameweek, budget):
    """Draft a squad to KEEP - long-horizon value, fixture coverage enforced.

    Same solver as Best XI, different objective. Best XI asks "who scores most
    this Saturday"; this asks "who is worth owning for the next couple of
    months", which is the question a manager with two transfers a week actually
    faces."""
    return optimise_squad(
        pool, gameweek, budget=budget,
        squad_values=squad_selection_values(pool, gameweek),
        squad_weight=SQUAD_WEIGHT,
        coverage=coverage_requirement(gameweek))


def fixture_outlook(pool, gameweeks):
    """Per-gameweek shape of the fixture list: how many teams play, and how many
    play twice.

    Derived from the players' own next_gameweeks rather than a separate fixture
    pull, so it always agrees with the numbers the optimiser is using. A team
    appearing twice in one event is a double gameweek; a team missing entirely
    from an event everyone else appears in is a blank.
    """
    outlook = {}
    for gw in gameweeks:
        teams_playing, teams_doubling = set(), set()
        for p in pool:
            team = p.get("team")
            if team is None:
                continue
            n = sum(1 for e in (p.get("next_gameweeks") or []) if e.get("event") == gw)
            if n >= 1:
                teams_playing.add(team)
            if n >= 2:
                teams_doubling.add(team)
        outlook[gw] = {
            "gameweek": gw,
            "teams_playing": len(teams_playing),
            "teams_doubling": len(teams_doubling),
            "is_double": len(teams_doubling) >= 4,
            "is_blank": 0 < len(teams_playing) <= 14,
        }
    return outlook


def _squad_from_rows(rows, pool_by_id):
    """Hydrate stored picks into full player dicts using the live pool."""
    squad = []
    for r in rows:
        p = pool_by_id.get(r["element_id"])
        if p is None:
            continue
        squad.append({**p,
                      "position": r["position"],
                      "starting": r["position"] <= 11,
                      "is_captain": bool(r["is_captain"]),
                      "is_vice_captain": bool(r["is_vice_captain"])})
    return squad


def load_state(pool):
    """The bot's current squad, bank and chip state, or None on its first run."""
    pool_by_id = {p["id"]: p for p in pool}
    with connect() as conn:
        head = conn.execute(
            """SELECT * FROM manager_team WHERE fpl_id = ?
               ORDER BY gameweek DESC LIMIT 1""", (AI_MANAGER_FPL_ID,)).fetchone()
        if head is None:
            return None
        picks = conn.execute(
            "SELECT * FROM manager_team_picks WHERE manager_team_id = ? ORDER BY position",
            (head["id"],)).fetchall()
        used = [r["chip"] for r in conn.execute(
            "SELECT chip FROM ai_transfer_log WHERE kind = 'chip' AND chip IS NOT NULL",
            ).fetchall()]
    squad = _squad_from_rows(picks, pool_by_id)
    return {
        "gameweek": head["gameweek"],
        "bank": head["bank"] if head["bank"] is not None else 0.0,
        "squad": squad,
        "chips_used": used,
        "incomplete": len(squad) != 15,   # a player left the league between runs
    }


def free_transfers(previous_free, transfers_made, wildcard_or_freehit=False):
    """FPL's rule: +1 per gameweek, capped at 5, and a wildcard/free hit week
    doesn't consume any."""
    if wildcard_or_freehit:
        return min(5, previous_free + 1)
    return max(1, min(5, previous_free - transfers_made + 1))


def free_transfers_for(gameweek):
    """How many free transfers the bot has going into `gameweek`.

    Replayed from its own decision log rather than stored as a column: the log
    already records every transfer and chip it made, so walking it forward is
    the single source of truth and can't drift out of sync with what actually
    happened. Cheap - one small query and at most 38 iterations.
    """
    with connect() as conn:
        # Every gameweek the bot actually played, not just ones it transferred
        # in. A quiet week still banks a free transfer, so walking only the log
        # would under-count the allowance permanently.
        played = [r["gameweek"] for r in conn.execute(
            """SELECT gameweek FROM manager_team
               WHERE fpl_id = ? AND gameweek < ? ORDER BY gameweek""",
            (AI_MANAGER_FPL_ID, int(gameweek)))]
        rows = conn.execute(
            """SELECT gameweek, kind, chip FROM ai_transfer_log
               WHERE gameweek < ? ORDER BY gameweek""", (int(gameweek),)).fetchall()

    per_gw = {gw: {"transfers": 0, "free_chip": False} for gw in played}
    for r in rows:
        slot = per_gw.setdefault(r["gameweek"], {"transfers": 0, "free_chip": False})
        if r["kind"] == "transfer":
            slot["transfers"] += 1
        elif r["chip"] in ("wildcard", "freehit"):
            slot["free_chip"] = True

    ft = 1
    for gw in sorted(per_gw):
        info = per_gw[gw]
        ft = free_transfers(ft, info["transfers"], info["free_chip"])
    return ft


def best_lineup(squad, gameweek):
    """Pick the strongest legal XI and captain from a fixed 15.

    Reuses the squad optimiser with the squad itself as the pool and a budget
    high enough not to bind - the selection is already made, only the lineup is
    in question."""
    try:
        return optimise_squad(squad, gameweek, budget=10_000.0,
                              max_per_club=15, include_unavailable=True)
    except OptimisationError:
        return None


def evaluate_transfers(squad, pool, bank, gameweek, free, max_transfers=MAX_TRANSFERS_PER_WEEK):
    """Greedy best-first transfers, judged on horizon value.

    Greedy rather than a full multi-transfer ILP on purpose: with at most two
    transfers a week the search is tiny, and a greedy pass with an explicit
    hit threshold is far easier to explain in the log - which matters, because
    the tab's whole job is showing WHY the bot did something.
    """
    owned = {p["id"] for p in squad}
    club_counts = {}
    for p in squad:
        club_counts[p.get("team")] = club_counts.get(p.get("team"), 0) + 1

    values = {p["id"]: horizon_value(p, gameweek) for p in squad}
    candidates = [p for p in pool if p["id"] not in owned and availability(p) > 0]
    cand_values = {p["id"]: horizon_value(p, gameweek) for p in candidates}

    moves, budget, made = [], bank, 0
    working = list(squad)

    while made < max_transfers:
        best = None
        for out in working:
            out_val = values.get(out["id"], 0.0)
            affordable = (out.get("cost") or 0) + budget
            for cand in candidates:
                if cand["id"] in owned or cand["pos"] != out["pos"]:
                    continue
                if (cand.get("cost") or 0) > affordable:
                    continue
                # 3-per-club, counting the outgoing player leaving.
                n_club = club_counts.get(cand.get("team"), 0) - (
                    1 if cand.get("team") == out.get("team") else 0)
                if n_club >= MAX_PER_CLUB:
                    continue
                gain = cand_values[cand["id"]] - out_val
                if best is None or gain > best["gain"]:
                    best = {"out": out, "in": cand, "gain": gain}
        if best is None:
            break

        is_free = made < free
        needed = MIN_FREE_TRANSFER_GAIN if is_free else HIT_COST + HIT_MARGIN
        if best["gain"] < needed:
            break

        out_p, in_p = best["out"], best["in"]
        budget += (out_p.get("cost") or 0) - (in_p.get("cost") or 0)
        owned.discard(out_p["id"])
        owned.add(in_p["id"])
        club_counts[out_p.get("team")] = club_counts.get(out_p.get("team"), 1) - 1
        club_counts[in_p.get("team")] = club_counts.get(in_p.get("team"), 0) + 1
        working = [in_p if p["id"] == out_p["id"] else p for p in working]
        values[in_p["id"]] = cand_values[in_p["id"]]
        made += 1
        moves.append({
            "out": out_p, "in": in_p,
            "gain": round(best["gain"], 2),
            "free": is_free,
            "hit": 0 if is_free else HIT_COST,
            "rationale": (
                f"{in_p['web_name']} projects {round(best['gain'], 1)} more points than "
                f"{out_p['web_name']} over the next {HORIZON} gameweeks"
                + ("" if is_free else f" - worth a -{int(HIT_COST)} hit")),
        })

    return {"squad": working, "moves": moves, "bank": round(budget, 1)}


def plan_chips(squad, pool, gameweek, chips_used, outlook, lineup):
    """Decide whether to play a chip this week, and flag what's coming.

    Re-run every gameweek rather than fixed at the start of the season: fixture
    lists move, doubles get created by postponements, and a plan made in August
    is worthless by March. Returns the chip to play now (or None) plus the
    reasoning for each, so the tab can show what it's waiting for.
    """
    available = [c for c in ("wildcard", "freehit", "bboost", "3xc") if c not in chips_used]
    notes, play = [], None

    here = outlook.get(gameweek, {})
    bench = [p for p in (lineup or {}).get("squad", []) if not p["starting"]]
    bench_pts = round(sum(p.get("predicted") or 0 for p in bench), 1)
    captain = next((p for p in (lineup or {}).get("squad", []) if p.get("is_captain")), None)
    captain_pts = round((captain or {}).get("predicted") or 0, 1)

    if "bboost" in available:
        ok = bench_pts >= BENCH_BOOST_MIN
        notes.append({
            "chip": "bboost", "ready": ok,
            "detail": f"Bench projects {bench_pts} pts (needs {BENCH_BOOST_MIN})"
                      + (" - double gameweek" if here.get("is_double") else "")})
        if ok and play is None:
            play = "bboost"

    if "3xc" in available and captain:
        ok = captain_pts >= TRIPLE_CAPTAIN_MIN
        notes.append({
            "chip": "3xc", "ready": ok,
            "detail": f"{captain['web_name']} projects {captain_pts} pts "
                      f"(needs {TRIPLE_CAPTAIN_MIN})"
                      + (" - double gameweek" if here.get("is_double") else "")})
        if ok and play is None:
            play = "3xc"

    # Free Hit earns its place in a BLANK week - when a chunk of your squad
    # simply isn't playing, a one-week rental of players who are beats
    # transferring in people you don't want to keep.
    if "freehit" in available:
        blanking = sum(1 for p in squad
                       if not any(e.get("event") == gameweek
                                  for e in (p.get("next_gameweeks") or [])))
        ok = here.get("is_blank") and blanking >= 5
        notes.append({
            "chip": "freehit", "ready": bool(ok),
            "detail": f"{blanking} of 15 players blank this gameweek"
                      + (" - blank gameweek" if here.get("is_blank") else "")})
        if ok and play is None:
            play = "freehit"

    # Wildcard is the fallback when the squad is simply far from optimal -
    # measured against what an unconstrained rebuild would score, so it fires on
    # genuine drift rather than on a bad week.
    if "wildcard" in available and play is None:
        try:
            ideal = optimise_squad(pool, gameweek, budget=DEFAULT_BUDGET)
            current = (lineup or {}).get("predicted_points") or 0
            gain = round(ideal["predicted_points"] - current, 1)
            ok = gain >= WILDCARD_MIN_GAIN
            notes.append({"chip": "wildcard", "ready": ok,
                          "detail": f"A rebuilt squad projects {gain} more pts "
                                    f"(needs {WILDCARD_MIN_GAIN})"})
            if ok:
                play = "wildcard"
        except OptimisationError as e:
            notes.append({"chip": "wildcard", "ready": False, "detail": str(e)})

    upcoming = [o for gw, o in sorted(outlook.items())
                if gw > gameweek and (o["is_double"] or o["is_blank"])]
    return {"play": play, "notes": notes, "upcoming": upcoming[:4],
            "available": available, "used": chips_used}


def _persist(gameweek, squad, bank, lineup, chip, moves, predicted):
    """Write the bot's squad for a gameweek plus its decision log."""
    order = {p["id"]: p for p in (lineup or {}).get("squad", [])}
    with connect() as conn:
        conn.execute("DELETE FROM manager_team WHERE fpl_id = ? AND gameweek = ?",
                     (AI_MANAGER_FPL_ID, int(gameweek)))
        cur = conn.execute(
            """INSERT INTO manager_team (fpl_id, gameweek, predicted_points, bank,
                                         value, active_chip, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (AI_MANAGER_FPL_ID, int(gameweek), predicted, bank,
             round(sum(p.get("cost") or 0 for p in squad), 1), chip, utcnow()))
        team_id = cur.lastrowid
        rows = []
        for p in squad:
            lp = order.get(p["id"], {})
            rows.append((team_id, p["id"], lp.get("position", 15),
                         int(bool(lp.get("is_captain"))), int(bool(lp.get("is_vice_captain"))),
                         2 if lp.get("is_captain") else 1,
                         p.get("cost"), lp.get("predicted", p.get("predicted"))))
        conn.executemany(
            """INSERT INTO manager_team_picks
                   (manager_team_id, element_id, position, is_captain, is_vice_captain,
                    multiplier, cost, predicted_points)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)

        conn.execute("DELETE FROM ai_transfer_log WHERE gameweek = ?", (int(gameweek),))
        for m in moves:
            conn.execute(
                """INSERT INTO ai_transfer_log (gameweek, kind, out_element_id,
                       in_element_id, cost_hit, rationale, created_at)
                   VALUES (?, 'transfer', ?, ?, ?, ?, ?)""",
                (int(gameweek), m["out"]["id"], m["in"]["id"], int(m["hit"]),
                 m["rationale"], utcnow()))
        if chip:
            conn.execute(
                """INSERT INTO ai_transfer_log (gameweek, kind, chip, rationale, created_at)
                   VALUES (?, 'chip', ?, ?, ?)""",
                (int(gameweek), chip, f"Played {chip} in GW{gameweek}", utcnow()))
    return team_id


def run_gameweek(pool, gameweek, budget=DEFAULT_BUDGET, persist=True):
    """The bot's weekly decision: transfers, lineup, captain, chip.

    First run of a season has no squad to carry forward, so it drafts one with
    the same optimiser the Best XI uses - which is why that had to exist first.
    """
    state = load_state(pool)
    gameweeks = sorted({e.get("event") for p in pool
                        for e in (p.get("next_gameweeks") or []) if e.get("event")})
    outlook = fixture_outlook(pool, gameweeks)

    if state is None or state.get("incomplete") or not state["squad"]:
        initial = build_squad(pool, gameweek, budget)
        squad = [{**p, "id": p["element_id"]} for p in initial["squad"]]
        bank = round(budget - initial["squad_cost"], 1)
        moves, chips_used = [], []
        first_run = True
    else:
        squad, bank, chips_used, first_run = (
            state["squad"], state["bank"], state["chips_used"], False)

    prev_free = 1 if first_run else free_transfers_for(gameweek)
    lineup = best_lineup(squad, gameweek)
    chips = plan_chips(squad, pool, gameweek, chips_used, outlook, lineup)
    chip = chips["play"]

    moves = []
    if not first_run:
        # A wildcard means the squad is rebuilt outright rather than nudged.
        if chip == "wildcard":
            rebuilt = build_squad(pool, gameweek, budget + bank)
            squad = [{**p, "id": p["element_id"]} for p in rebuilt["squad"]]
            bank = round(budget + bank - rebuilt["squad_cost"], 1)
        else:
            result = evaluate_transfers(squad, pool, bank, gameweek, prev_free)
            squad, moves, bank = result["squad"], result["moves"], result["bank"]
        lineup = best_lineup(squad, gameweek)

    predicted = (lineup or {}).get("predicted_points", 0.0)
    if chip == "bboost" and lineup:
        predicted += sum(p.get("predicted") or 0 for p in lineup["squad"] if not p["starting"])
    if chip == "3xc" and lineup:
        cap = next((p for p in lineup["squad"] if p.get("is_captain")), None)
        if cap:
            predicted += cap.get("predicted") or 0     # 2x -> 3x
    predicted = round(predicted - sum(m["hit"] for m in moves), 2)

    if persist:
        _persist(gameweek, squad, bank, lineup, chip, moves, predicted)

    return {
        "gameweek": gameweek, "first_run": first_run, "bank": bank,
        "total_points": total_points_to(gameweek - 1),
        "squad": (lineup or {}).get("squad", squad),
        "formation": (lineup or {}).get("formation"),
        "squad_cost": round(sum(p.get("cost") or 0 for p in squad), 1),
        # Built from the pool passed in, so this is a live figure - which is
        # what the caller wants for the gameweek being picked. get_gameweek()
        # returns None for it on a stored week; see the note there.
        "team_rating": team_rating((lineup or {}).get("squad", squad)),
        "predicted_points": predicted,
        "transfers": [{"out": m["out"]["web_name"], "in": m["in"]["web_name"],
                       "gain": m["gain"], "hit": m["hit"], "free": m["free"],
                       "rationale": m["rationale"]} for m in moves],
        "hits": sum(m["hit"] for m in moves),
        "chip": chip, "chip_plan": chips, "outlook": outlook,
        "horizon": HORIZON,
    }


def total_points_to(gameweek=None):
    """The bot's cumulative real points up to and including `gameweek`.

    Summed from the stored rows rather than carried in a column: points are
    backfilled per gameweek once FPL settles them, so deriving the total means
    it can never disagree with the weeks it's made of."""
    sql = ("SELECT COALESCE(SUM(points), 0) FROM manager_team "
           "WHERE fpl_id = ? AND points IS NOT NULL")
    args = [AI_MANAGER_FPL_ID]
    if gameweek is not None:
        sql += " AND gameweek <= ?"
        args.append(int(gameweek))
    with connect() as conn:
        return int(conn.execute(sql, args).fetchone()[0] or 0)


def get_gameweek(gameweek, pool=None):
    """A stored AI Manager gameweek, with its transfer log."""
    pool_by_id = {p["id"]: p for p in (pool or [])}
    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM manager_team WHERE fpl_id = ? AND gameweek = ?",
            (AI_MANAGER_FPL_ID, int(gameweek))).fetchone()
        if head is None:
            return None
        picks = conn.execute(
            "SELECT * FROM manager_team_picks WHERE manager_team_id = ? ORDER BY position",
            (head["id"],)).fetchall()
        log = conn.execute(
            "SELECT * FROM ai_transfer_log WHERE gameweek = ? ORDER BY id", (int(gameweek),)
        ).fetchall()

    squad = []
    for r in picks:
        p = pool_by_id.get(r["element_id"], {})
        squad.append({
            "id": r["element_id"], "web_name": p.get("web_name") or f"#{r['element_id']}",
            "pos": p.get("pos"), "team_code": p.get("team_code"),
            "team_name": p.get("team_name"), "cost": r["cost"],
            "predicted": r["predicted_points"], "actual_points": r["actual_points"],
            "position": r["position"], "starting": (r["position"] or 99) <= 11,
            "is_captain": bool(r["is_captain"]), "is_vice_captain": bool(r["is_vice_captain"]),
        })
    transfers = [{
        "kind": r["kind"], "chip": r["chip"], "hit": r["cost_hit"],
        "out": (pool_by_id.get(r["out_element_id"], {}) or {}).get("web_name"),
        "in": (pool_by_id.get(r["in_element_id"], {}) or {}).get("web_name"),
        "rationale": r["rationale"],
    } for r in log]
    return {
        "gameweek": head["gameweek"], "bank": head["bank"], "value": head["value"],
        "predicted_points": head["predicted_points"], "points": head["points"],
        "total_points": total_points_to(head["gameweek"]),
        "active_chip": head["active_chip"], "captured_at": head["captured_at"],
        "squad": squad, "transfers": transfers,
        # None for the same reason as ai_team.get_snapshot: ratings move every
        # night, so one derived now would describe today's players rather than
        # the squad as it stood when this gameweek was committed.
        "team_rating": None,
    }


def history():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT gameweek, points, predicted_points, bank, value, active_chip
               FROM manager_team WHERE fpl_id = ? ORDER BY gameweek DESC""",
            (AI_MANAGER_FPL_ID,))]
