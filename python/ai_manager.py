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

import chip_model
import fixture_structure
from db import AI_MANAGER_FPL_ID, connect, utcnow
from fixture_structure import chip_half, half_deadline
from squad_optimiser import (DEFAULT_BUDGET, MAX_PER_CLUB, OptimisationError,
                             availability, optimise_squad, team_rating)
from valuation import (HORIZON, SQUAD_HORIZON, SQUAD_WEIGHT,
                       coverage_requirement, horizon_value,
                       squad_selection_values)

# A transfer beyond the free allowance costs 4 points. Only make one if the
# horizon gain clears the hit with room to spare - marginal hits are how FPL
# seasons get quietly destroyed.
HIT_COST = 4.0
HIT_MARGIN = 2.0
# Even a free transfer should do something worthwhile rather than churn.
MIN_FREE_TRANSFER_GAIN = 0.8
MAX_TRANSFERS_PER_WEEK = 2

# How much the solver is told to care about bench points in the gameweeks
# leading up to a scheduled Bench Boost. Normally it is told not to care at all
# (squad_optimiser.BENCH_WEIGHT is 0.0), which is right all season except for
# the two weeks before the chip: a squad built to ignore its bench will field a
# £17m bench into the one week the bench scores.
BENCH_BOOST_BUILD_WEIGHT = 0.6
BENCH_BOOST_BUILD_LEAD = 2


def build_squad(pool, gameweek, budget, bench_weight=None):
    """Draft a squad to KEEP - long-horizon value, fixture coverage enforced.

    Same solver as Best XI, different objective. Best XI asks "who scores most
    this Saturday"; this asks "who is worth owning for the next couple of
    months", which is the question a manager with two transfers a week actually
    faces.

    `bench_weight` is normally left alone - see BENCH_BOOST_BUILD_WEIGHT for the
    one case where the squad is deliberately built to have a bench worth
    playing."""
    kwargs = {} if bench_weight is None else {"bench_weight": bench_weight}
    return optimise_squad(
        pool, gameweek, budget=budget,
        squad_values=squad_selection_values(pool, gameweek),
        squad_weight=SQUAD_WEIGHT,
        coverage=coverage_requirement(gameweek), **kwargs)


def fixture_outlook(pool, gameweeks=None, season=None):
    """Per-gameweek shape of the fixture list: how many teams play, and how many
    play twice.

    Read from the season's fixture list rather than the rated pool, because the
    pool's next_gameweeks only run eight deep and a chip planner working to a
    GW19 deadline has to see past that. A team appearing twice in one event is a
    double gameweek; a team missing entirely from an event everyone else appears
    in is a blank. See fixture_structure for why the pool is only a fallback.
    """
    return fixture_structure.combined_outlook(pool, gameweeks, season=season)


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
        # The squad comes from the last week that WASN'T a free hit. A free hit
        # is a one-week rental: those picks are what played and what gets
        # scored, so they belong in manager_team_picks, but the squad carried
        # into next week is the one the rental was borrowed instead of. Reading
        # the latest row flat would hand the bot a team it never bought.
        owned = conn.execute(
            """SELECT * FROM manager_team
               WHERE fpl_id = ? AND COALESCE(active_chip, '') != 'freehit'
               ORDER BY gameweek DESC LIMIT 1""", (AI_MANAGER_FPL_ID,)).fetchone()
        picks = conn.execute(
            "SELECT * FROM manager_team_picks WHERE manager_team_id = ? ORDER BY position",
            ((owned or head)["id"],)).fetchall()
        # Every chip ever played, with the gameweek it was played in. The
        # gameweek matters: FPL hands the whole set back after GW19, so a
        # wildcard played in GW6 must stop counting as used once GW20 arrives.
        # Filtering here would lose that, so the filtering is left to the
        # caller, which knows which gameweek it is planning for.
        played = [(r["gameweek"], r["chip"]) for r in conn.execute(
            """SELECT gameweek, chip FROM ai_transfer_log
               WHERE kind = 'chip' AND chip IS NOT NULL ORDER BY gameweek""").fetchall()]
    squad = _squad_from_rows(picks, pool_by_id)
    return {
        "gameweek": head["gameweek"],
        "bank": head["bank"] if head["bank"] is not None else 0.0,
        "squad": squad,
        "chips_played": played,
        "incomplete": len(squad) != 15,   # a player left the league between runs
    }


def chips_used_in_half(played, gameweek):
    """Which chips are already spent from the set `gameweek` can draw on.

    FPL returns every chip after GW19, so "used" is only ever a question about
    one half of the season. Without this the bot plays four chips all year
    instead of eight, and the second set is never touched.
    """
    start, end = chip_half(gameweek)
    return [chip for gw, chip in played if chip and start <= int(gw) <= end]


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

    # The bot's FIRST gameweek is skipped, not stepped. That week it drafted a
    # squad from nothing under the same unlimited-transfer rule a human gets
    # before the first deadline - it neither spent an allowance nor banked one,
    # so it goes into gameweek two with exactly one free transfer. Stepping it
    # like an ordinary round left the bot believing it had two, and spending the
    # phantom one is a -4 it never accounted for.
    ft = 1
    for gw in sorted(per_gw)[1:]:
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


def _chip_detail(chip, gameweek, target, gain, computed, factors, lineup, priors):
    """One line explaining what the planner thinks of a chip, for the tab.

    Says what the chip is worth and, if it isn't being played, which gameweek it
    is being kept for - "waiting" with no destination is the thing the old
    version couldn't tell you.
    """
    bar = chip_model.floor(chip, priors)
    if chip == "bboost":
        head = f"Bench projects {round(gain, 1)} pts"
    elif chip == "3xc":
        captain = next((p for p in (lineup or {}).get("squad", [])
                        if p.get("is_captain")), None)
        who = (captain or {}).get("web_name", "the captain")
        head = f"{who} projects {round(gain, 1)} extra pts"
    elif chip == "freehit":
        one_week = factors.get("one_week_gain")
        head = (f"A one-week rental projects {one_week} more pts"
                if one_week is not None else
                f"A one-week rental projects {round(gain, 1)} more pts")
    else:
        head = f"A rebuilt squad projects {round(gain, 1)} more pts over {SQUAD_HORIZON} GWs"
        reasons = []
        if factors.get("injured"):
            reasons.append(f"{factors['injured']} injured")
        if factors.get("deadweight"):
            reasons.append(f"{factors['deadweight']} not contributing")
        if factors.get("blanking"):
            reasons.append(f"{factors['blanking']} without a fixture")
        if reasons:
            head += " - " + ", ".join(reasons)

    if target == gameweek:
        return head + " - playing it"
    if target:
        return head + f" (worth {round(bar, 1)}+) - held for GW{target}"
    if computed is not None and computed < bar:
        return head + f" - below the {round(bar, 1)} pts a chip is worth burning"
    return head + " - held"


def plan_chips(squad, pool, gameweek, chips_used, outlook, lineup, bank=0.0):
    """Decide whether to play a chip this week, and say what the rest are for.

    Re-run every gameweek rather than fixed at the start of the season: fixture
    lists move, doubles get created by postponements, and a plan made in August
    is worthless by March.

    The decision itself is an assignment of the remaining chips to the remaining
    gameweeks of this half - see chip_model. Only the chip that lands on THIS
    gameweek is played; the rest of the schedule is a forecast, which is what
    lets the tab say "holding the Bench Boost for GW14" rather than "not yet".
    """
    priors = chip_model.load_priors()
    available = [c for c in chip_model.CHIP_CODES if c not in chips_used]
    deadline = half_deadline(gameweek)
    gameweeks = [gw for gw in sorted(outlook) if gameweek <= gw <= deadline]
    if not gameweeks:
        gameweeks = [gameweek]

    matrix = chip_model.gain_matrix(available, gameweek, gameweeks, squad, pool,
                                    outlook, bank=bank, lineup=lineup, priors=priors)
    schedule = chip_model.schedule_chips(available, gameweek, deadline, matrix, priors)
    targets = {chip: gw for gw, chip in schedule.items()}
    play = schedule.get(gameweek)

    notes = []
    for chip in available:
        effective, computed, factors = matrix[chip][gameweek]
        notes.append({
            "chip": chip,
            "ready": chip == play,
            "target": targets.get(chip),
            "gain": round(computed if computed is not None else effective, 1),
            "floor": round(chip_model.floor(chip, priors), 1),
            "factors": factors,
            "detail": _chip_detail(chip, gameweek, targets.get(chip),
                                   computed if computed is not None else effective,
                                   computed, factors, lineup, priors),
        })

    upcoming = [o for gw, o in sorted(outlook.items())
                if gw > gameweek and (o["is_double"] or o["is_blank"])]
    return {
        "play": play, "notes": notes, "upcoming": upcoming[:4],
        "available": available, "used": chips_used,
        # The forecast, oldest first, so the tab can show the whole plan and the
        # stored version can later be scored against what actually happened.
        "schedule": [{"gameweek": gw, "chip": chip,
                      "expected_gain": round(matrix[chip][gw][0], 1)}
                     for gw, chip in sorted(schedule.items())],
        "deadline": deadline,
    }


def bench_build_weight(chips, gameweek):
    """How much the solver should care about the bench when rebuilding.

    Normally nothing: squad_optimiser deliberately buys the cheapest legal bench
    because bench players don't score, and every pound spent there is a pound
    missing from the eleven that do. The exception is the run-up to a Bench
    Boost, which is the one week they do score. Without this the planner
    schedules a Bench Boost and then fields a £17.0m bench into it - the chip
    policy and the squad policy quietly working against each other.
    """
    for entry in (chips or {}).get("schedule") or []:
        if entry["chip"] == "bboost" and 0 <= entry["gameweek"] - gameweek <= BENCH_BOOST_BUILD_LEAD:
            return BENCH_BOOST_BUILD_WEIGHT
    return None


def free_hit_squad(pool, gameweek, budget):
    """The best team money can buy for one gameweek, ignoring who's owned.

    No squad_values and no coverage requirement, unlike build_squad: a Free Hit
    lasts one week, so there is no next month to stay balanced for and no blank
    to keep bodies back for.
    """
    try:
        return optimise_squad(pool, gameweek, budget=budget)
    except OptimisationError:
        return None


def save_chip_plan(gameweek, chips):
    """Freeze this gameweek's chip forecast so it can be scored later."""
    rows = [(int(gameweek), e["chip"], int(e["gameweek"]), float(e["expected_gain"]),
             utcnow()) for e in (chips or {}).get("schedule") or []]
    with connect() as conn:
        conn.execute("DELETE FROM ai_chip_plan WHERE gameweek = ?", (int(gameweek),))
        conn.executemany(
            """INSERT INTO ai_chip_plan (gameweek, chip, target_gw, expected_gain, created_at)
               VALUES (?, ?, ?, ?, ?)""", rows)
    return len(rows)



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
    # Runs to the end of the season, not to the end of the rated pool's eight
    # gameweeks: the chip scheduler has to count the weeks left before the
    # GW19 reset, and it cannot do that from a horizon shorter than the
    # deadline it is working to.
    outlook = fixture_outlook(pool)

    if state is None or state.get("incomplete") or not state["squad"]:
        initial = build_squad(pool, gameweek, budget)
        squad = [{**p, "id": p["element_id"]} for p in initial["squad"]]
        bank = round(budget - initial["squad_cost"], 1)
        moves, chips_played = [], []
        first_run = True
    else:
        squad, bank, chips_played, first_run = (
            state["squad"], state["bank"], state["chips_played"], False)

    prev_free = 1 if first_run else free_transfers_for(gameweek)
    lineup = best_lineup(squad, gameweek)
    chips_used = chips_used_in_half(chips_played, gameweek)
    chips = plan_chips(squad, pool, gameweek, chips_used, outlook, lineup, bank=bank)
    chip = chips["play"]

    moves = []
    fielded = squad                       # what actually plays, which a free hit changes
    if not first_run:
        # A wildcard means the squad is rebuilt outright rather than nudged.
        if chip == "wildcard":
            rebuilt = build_squad(pool, gameweek, budget + bank,
                                  bench_weight=bench_build_weight(chips, gameweek))
            squad = [{**p, "id": p["element_id"]} for p in rebuilt["squad"]]
            bank = round(budget + bank - rebuilt["squad_cost"], 1)
        elif chip == "freehit":
            # A Free Hit is a rental: the best team money can buy for one week,
            # reverting to the real squad afterwards. So the squad carried
            # forward is deliberately NOT updated - only what gets fielded is.
            pass
        else:
            result = evaluate_transfers(squad, pool, bank, gameweek, prev_free)
            squad, moves, bank = result["squad"], result["moves"], result["bank"]
        lineup = best_lineup(squad, gameweek)
        fielded = squad

    if chip == "freehit":
        rental = free_hit_squad(pool, gameweek, budget + bank)
        if rental is not None:
            lineup, fielded = rental, [{**p, "id": p["element_id"]}
                                       for p in rental["squad"]]

    predicted = (lineup or {}).get("predicted_points", 0.0)
    if chip == "bboost" and lineup:
        predicted += sum(p.get("predicted") or 0 for p in lineup["squad"] if not p["starting"])
    if chip == "3xc" and lineup:
        cap = next((p for p in lineup["squad"] if p.get("is_captain")), None)
        if cap:
            predicted += cap.get("predicted") or 0     # 2x -> 3x
    predicted = round(predicted - sum(m["hit"] for m in moves), 2)

    if persist:
        # `fielded` is what played and is what gets scored; `squad` is what the
        # bot still owns going into next week. They differ only on a Free Hit,
        # which is exactly the week the distinction matters.
        _persist(gameweek, fielded, bank, lineup, chip, moves, predicted)
        save_chip_plan(gameweek, chips)

    return {
        "gameweek": gameweek, "first_run": first_run, "bank": bank,
        "total_points": total_points_to(gameweek - 1),
        "free_transfers": prev_free,
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
            # The club id, not just its code. The code draws the shirt; the id
            # is what says whether that club has kicked off yet, which is what
            # decides between showing a score and showing a fixture.
            "team": p.get("team"),
            # And the fixture itself, which this has never sent. The card falls
            # back to showing the opponent when there is no score yet, so
            # without this the fallback had nothing to fall back TO - a blank
            # tile with a projection floating in it. ai_team's snapshot reader
            # has always passed these through; this one simply did not.
            "next_gameweeks": p.get("next_gameweeks") or [],
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
        "squad_cost": head["value"],
        "predicted_points": head["predicted_points"], "points": head["points"],
        "total_points": total_points_to(head["gameweek"]),
        "active_chip": head["active_chip"], "captured_at": head["captured_at"],
        # What the week cost and what it had to spend. Both are already in the
        # rows above - the hits in the transfer log, the allowance replayable
        # from the decision history - they simply were not being handed over,
        # so the tab could describe the bot's squad but not the rules it was
        # playing under.
        "hits": sum(r["cost_hit"] or 0 for r in log),
        "free_transfers": free_transfers_for(head["gameweek"]),
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
