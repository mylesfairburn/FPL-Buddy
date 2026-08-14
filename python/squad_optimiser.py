"""Budget-constrained squad selection, as an integer linear program.

This is not an extension of team_service._optimise(). That takes a squad you
already own and orders it into a legal XI plus bench; this picks WHICH 15
players to own in the first place, under a budget and a per-club cap.

Why an ILP and not a greedy pass: the constraints interact. "Best points per
million, position by position" can spend the budget early and then be unable to
fill the last forward slot, or hit the 3-per-club cap on a team it already took
three cheap defenders from and be forced into a strictly worse fix. A solver
handles the interaction directly and returns a provably optimal squad, which
also means the number this tab reports is meaningful rather than "whatever the
heuristic happened to find".

Squad picking and lineup picking are solved TOGETHER in one model rather than
"pick 15, then pick the best 11 from them". Those give different answers: the
best 15 by total value is not the 15 whose best 11 scores highest, because only
the starters (and a doubled captain) actually score.

PuLP ships its own CBC binary, so there's no system solver to install.
"""

import pulp

# FPL squad rules. Structure, not preference - these are the actual game rules.
SQUAD_QUOTA = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_SIZE = 15
XI_SIZE = 11
MAX_PER_CLUB = 3
DEFAULT_BUDGET = 100.0

# Bench players don't score. Weighting them at all would spend budget on players
# who sit there, so the default is 0: every spare pound belongs in the XI, and
# the solver naturally lands on the cheapest bench that still lets it field the
# strongest eleven. Cover for doubtful starters is handled by a constraint
# instead (see below), not by paying for a good bench across the board.
BENCH_WEIGHT = 0.0
# With BENCH_WEIGHT at 0 the objective is genuinely indifferent between two
# benches once the XI is maximal, so the solver can hand back an expensive one
# for no reason. This tips ties toward the cheaper bench. Kept tiny on purpose:
# across a whole bench it's worth well under a tenth of a point, so it can never
# outrank a real improvement to the eleven - it only decides between equals.
BENCH_COST_TIEBREAK = 0.001

# FPL flags availability as a status letter plus a 0-100 percentage.
#   'a' available, 'd' doubtful, 'i' injured, 's' suspended, 'u' unavailable
# Injured/suspended/unavailable are never selectable. Doubtful players ARE -
# a 75%-fit premium can still be the right pick - but their predicted points
# are scaled by that probability so the optimiser trades them off honestly
# rather than treating "probably plays" as "plays".
SELECTABLE_STATUS = {"a", "d"}
DEFAULT_DOUBTFUL_CHANCE = 0.5      # 'd' with no percentage given
# At or below this, a starter is risky enough to want a real replacement behind
# them rather than the usual £4.0m bench filler.
COVER_NEEDED_AT = 0.9
# A bench player only counts as cover if they're themselves dependable.
RELIABLE_AT = 0.9


class OptimisationError(RuntimeError):
    """Raised when no legal squad exists for the given pool/budget."""


def predicted_for_gameweek(player, gameweek):
    """The player's predicted points for one specific gameweek.

    Deliberately NOT `next_gameweeks[0]`. That list is built from fixtures that
    haven't finished yet, so its first entry rolls forward to the next round the
    moment a gameweek's matches complete - reading [0] would silently attribute
    some later gameweek's numbers to the one being snapshotted. Matching on
    `event` means a stale pool produces None (and a clear failure) rather than a
    quietly wrong squad.
    """
    for gw in player.get("next_gameweeks") or []:
        if gw.get("event") == gameweek:
            pts = gw.get("points")
            return float(pts) if pts is not None else None
    return None


def availability(player):
    """Probability this player features, in 0..1.

    FPL's percentage where it gives one, otherwise inferred from the status
    letter. Used to scale predicted points, so a 75%-fit player is compared on
    75% of his output rather than all of it."""
    status = (player.get("status") or "a").lower()
    if status not in SELECTABLE_STATUS:
        return 0.0
    chance = player.get("chance_of_playing_next_round")
    if chance is not None:
        try:
            return max(0.0, min(1.0, float(chance) / 100.0))
        except (TypeError, ValueError):
            pass
    return 1.0 if status == "a" else DEFAULT_DOUBTFUL_CHANCE


def eligible_pool(players, gameweek, include_unavailable=False):
    """Players that can legally be picked and have a prediction for this
    gameweek.

    Injured, suspended and unavailable players are dropped outright - an
    optimiser that doesn't know they can't play will happily build a squad
    around them. Doubtful players are kept, with their points risk-adjusted.
    """
    pool = []
    for p in players:
        if p.get("cost") is None or p.get("pos") not in SQUAD_QUOTA:
            continue
        avail = availability(p)
        if not include_unavailable and avail <= 0.0:
            continue
        pts = predicted_for_gameweek(p, gameweek)
        if pts is None:
            continue
        pool.append({**p, "_raw_predicted": pts,
                     "_availability": avail,
                     "_predicted": pts * avail})
    return pool


def team_rating(squad):
    """A squad's rating out of 100: the mean of its starting XI's ratings.

    **This is not a points projection, and a higher one does not mean a higher
    score.** A player's `rating` is his percentile among the players in his own
    position (rating_model.predict_ratings: predicted points, ranked within
    position, scaled to 100). Top of your position is 100 whichever position
    that is, so an elite goalkeeper and an elite forward both read 100 and are
    worth very different numbers of points. Averaging eleven of those measures
    how good the eleven are relative to their peers - a squad-quality figure -
    and says nothing about the total they will return on Saturday. The
    Predicted chip beside it is the number that answers that.

    Two further reasons the two can move apart: this reads `rating` from the
    pool as it stands today, not as it stood for any particular gameweek, and
    it takes no account of captaincy, of that week's fixtures, or of a starter
    who is a doubt to play at all.

    Starters only, and deliberately. Averaging all fifteen would drag every team
    towards the middle through the two cheap bench fillers nobody intends to
    play - and it would rate a £100m squad and a £83m squad about the same,
    because the difference between them is mostly in the eleven. Comparing
    elevens is the comparison a manager is actually making.

    Returns None rather than a number for a partial squad, since an average
    over seven players isn't the same measurement and shouldn't be printed as
    though it were.

    Note this reads `rating` from the pool the squad was built from, so it is
    always a statement about the players as rated TODAY. Callers holding a
    stored squad from an earlier gameweek should not present it as that
    gameweek's rating - see ai_team.get_snapshot.
    """
    starters = [p for p in squad or [] if p.get("starting")]
    if len(starters) != XI_SIZE:
        return None
    ratings = [float(p["rating"]) for p in starters
               if p.get("rating") is not None and p.get("rating") == p.get("rating")]
    if len(ratings) != XI_SIZE:
        return None
    return round(sum(ratings) / len(ratings))


def _formation(starters):
    counts = {pos: sum(1 for p in starters if p["pos"] == pos) for pos in SQUAD_QUOTA}
    return f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}"


def optimise_squad(players, gameweek, budget=DEFAULT_BUDGET,
                   max_per_club=MAX_PER_CLUB, bench_weight=BENCH_WEIGHT,
                   include_unavailable=False, squad_values=None,
                   squad_weight=1.0, coverage=None):
    """Pick the highest-scoring legal 15, its starting XI, and its captain.

    Returns a dict with `squad` (15 dicts carrying position 1-15, starting,
    is_captain, is_vice_captain, cost and the frozen predicted points),
    `formation`, `squad_cost`, `predicted_points` and `bench_predicted`.

    `predicted_points` counts starters only, with the captain doubled - the same
    definition the My Team tab uses, so the two numbers are comparable.

    Two hooks exist so a season-long squad can be built with the same solver as
    a one-week one, rather than duplicating the model:

    `squad_values` - {player_id: value} used for the OWNERSHIP decision, while
    the lineup and captaincy still go on this gameweek's points. Without it the
    best 15 for one gameweek is also the best 15 to own, which is only true if
    you never keep a player past Saturday. The AI Manager passes multi-gameweek
    values here; Best XI leaves it None and is unaffected.

    `coverage` - {gameweek: minimum owned players with a fixture}. Blank
    gameweeks are where a squad picked purely on points falls over: it can be
    excellent on paper and unable to field eleven bodies. Constraining coverage
    across the horizon is what makes a squad rotation-proof.
    """
    pool = eligible_pool(players, gameweek, include_unavailable)
    if not pool:
        # The rating model only projects a few gameweeks ahead, so asking for a
        # distant one isn't an error so much as out of range - say that rather
        # than reporting "0 players", which reads like a data failure.
        horizon = sorted({g.get("event") for p in players
                          for g in (p.get("next_gameweeks") or [])
                          if g.get("event") is not None})
        window = f"GW{horizon[0]}-{horizon[-1]}" if horizon else "none"
        raise OptimisationError(
            f"No predictions exist for GW{gameweek}. The model currently "
            f"projects {window}.")
    if len(pool) < SQUAD_SIZE:
        raise OptimisationError(
            f"Only {len(pool)} players have a prediction for GW{gameweek} "
            f"(need at least {SQUAD_SIZE}).")

    for pos, need in SQUAD_QUOTA.items():
        have = sum(1 for p in pool if p["pos"] == pos)
        if have < need:
            raise OptimisationError(
                f"Pool has {have} available {pos} but a squad needs {need}.")

    idx = range(len(pool))
    cost = [float(p["cost"]) for p in pool]
    pts = [float(p["_predicted"]) for p in pool]

    prob = pulp.LpProblem("fpl_best_xi", pulp.LpMaximize)
    pick = pulp.LpVariable.dicts("pick", idx, cat="Binary")    # in the 15
    start = pulp.LpVariable.dicts("start", idx, cat="Binary")  # in the XI
    capt = pulp.LpVariable.dicts("capt", idx, cat="Binary")    # wears the armband

    # Only starters score, and the captain scores twice - so captaincy is part
    # of the optimisation, not a post-hoc "pick the highest starter". When
    # squad_values is given, owning a player is scored separately from starting
    # him, which is what lets one solver serve both a one-week XI and a squad
    # meant to last.
    own_value = ([float(squad_values.get(pool[i]["id"], 0.0)) for i in idx]
                 if squad_values else None)
    prob += (
        pulp.lpSum(pts[i] * start[i] for i in idx)
        + pulp.lpSum(pts[i] * capt[i] for i in idx)
        + (squad_weight * pulp.lpSum(own_value[i] * pick[i] for i in idx)
           if own_value else 0)
        + bench_weight * pulp.lpSum(pts[i] * (pick[i] - start[i]) for i in idx)
        - BENCH_COST_TIEBREAK * pulp.lpSum(cost[i] * (pick[i] - start[i]) for i in idx)
    )

    prob += pulp.lpSum(pick[i] for i in idx) == SQUAD_SIZE
    prob += pulp.lpSum(cost[i] * pick[i] for i in idx) <= budget
    prob += pulp.lpSum(start[i] for i in idx) == XI_SIZE
    prob += pulp.lpSum(capt[i] for i in idx) == 1

    for pos, need in SQUAD_QUOTA.items():
        members = [i for i in idx if pool[i]["pos"] == pos]
        prob += pulp.lpSum(pick[i] for i in members) == need
        prob += pulp.lpSum(start[i] for i in members) >= XI_MIN[pos]
        prob += pulp.lpSum(start[i] for i in members) <= XI_MAX[pos]

    by_club = {}
    for i in idx:
        by_club.setdefault(pool[i].get("team"), []).append(i)
    for team, members in by_club.items():
        if team is not None:
            prob += pulp.lpSum(pick[i] for i in members) <= max_per_club

    for i in idx:
        prob += start[i] <= pick[i]   # can't start someone you don't own
        prob += capt[i] <= start[i]   # can't captain a benched player

    # Cover for doubtful starters. Per position: at least as many DEPENDABLE
    # bench players as doubtful starters. Without this, a bench chosen purely
    # on cheapness leaves a 75%-fit striker with £4.0m of non-playing filler
    # behind him, so the auto-sub brings on nobody useful when he's ruled out.
    # Expressed on (pick - start), which is exactly "is benched".
    doubtful = [1 if pool[i]["_availability"] <= COVER_NEEDED_AT else 0 for i in idx]
    reliable = [1 if pool[i]["_availability"] >= RELIABLE_AT else 0 for i in idx]
    for pos in SQUAD_QUOTA:
        members = [i for i in idx if pool[i]["pos"] == pos]
        prob += (pulp.lpSum(reliable[i] * (pick[i] - start[i]) for i in members)
                 >= pulp.lpSum(doubtful[i] * start[i] for i in members))

    # Fixture coverage across the horizon: enough owned players actually have a
    # match in each of these gameweeks. Only binds in blank weeks, which is
    # exactly when a points-only squad would be caught short.
    for cov_gw, minimum in (coverage or {}).items():
        playing = [i for i in idx
                   if any(e.get("event") == cov_gw
                          for e in (pool[i].get("next_gameweeks") or []))]
        if len(playing) >= minimum:      # infeasible asks are ignored, not fatal
            prob += pulp.lpSum(pick[i] for i in playing) >= minimum

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise OptimisationError(
            f"No legal squad found for GW{gameweek} within £{budget}m "
            f"(solver status: {pulp.LpStatus[status]}).")

    chosen = [i for i in idx if pick[i].value() and round(pick[i].value()) == 1]
    starters = [i for i in chosen if start[i].value() and round(start[i].value()) == 1]
    bench = [i for i in chosen if i not in set(starters)]
    captain = next((i for i in chosen if capt[i].value() and round(capt[i].value()) == 1), None)

    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    starters.sort(key=lambda i: (order[pool[i]["pos"]], -pts[i]))

    # Bench order decides who the auto-sub brings on first, so it isn't just
    # cosmetic. Bench GK takes slot 1 (FPL substitutes keepers only for the
    # keeper). Among outfielders, anyone covering a position where a doubtful
    # player is starting goes first - that's the whole point of carrying them -
    # and the rest fall in on predicted points.
    doubtful_positions = {pool[i]["pos"] for i in starters
                          if pool[i]["_availability"] <= COVER_NEEDED_AT}
    bench.sort(key=lambda i: (
        pool[i]["pos"] != "GK",                                   # bench GK first
        not (pool[i]["pos"] in doubtful_positions                 # then cover...
             and pool[i]["_availability"] >= RELIABLE_AT),
        -pts[i]))                                                 # then best first

    # Vice is the highest-scoring starter who isn't the captain. Note `starters`
    # is sorted by POSITION for the pitch layout, so taking the first non-captain
    # from it hands the armband to whichever goalkeeper happens to lead the list.
    vice = max((i for i in starters if i != captain), key=lambda i: pts[i], default=None)

    squad = []
    for slot, i in enumerate(starters + bench, start=1):
        p = pool[i]
        squad.append({
            "element_id": p["id"],
            "id": p["id"],
            "web_name": p.get("web_name"),
            "pos": p["pos"],
            "team": p.get("team"),
            "team_code": p.get("team_code"),
            "team_name": p.get("team_name"),
            "cost": round(float(p["cost"]), 1),
            "rating": p.get("rating"),
            "predicted": round(pts[i], 2),                     # risk-adjusted
            "raw_predicted": round(float(p["_raw_predicted"]), 2),
            "availability": round(float(p["_availability"]), 2),
            "status": p.get("status", "a"),
            # Carried through so the front end can show the same availability
            # band as the My Team pitch, rather than re-deriving a percentage
            # from the rounded `availability` figure above.
            "chance_of_playing_next_round": p.get("chance_of_playing_next_round"),
            "news": p.get("news", "") or "",
            "is_cover": (slot > XI_SIZE and p["pos"] in doubtful_positions
                         and p["_availability"] >= RELIABLE_AT),
            "next_gameweeks": p.get("next_gameweeks") or [],
            "position": slot,
            "starting": slot <= XI_SIZE,
            "is_captain": i == captain,
            "is_vice_captain": i == vice,
        })

    starter_pts = sum(pts[i] for i in starters) + (pts[captain] if captain is not None else 0)
    return {
        "gameweek": gameweek,
        "squad": squad,
        "formation": _formation([pool[i] for i in starters]),
        "budget": round(float(budget), 1),
        "squad_cost": round(sum(cost[i] for i in chosen), 1),
        "team_rating": team_rating(squad),
        "predicted_points": round(starter_pts, 2),
        "bench_predicted": round(sum(pts[i] for i in bench), 2),
        "pool_size": len(pool),
    }


def verify(result, budget=DEFAULT_BUDGET, max_per_club=MAX_PER_CLUB):
    """Re-check a result against the FPL rules independently of the solver.

    Cheap insurance: a wrong constraint would otherwise surface as a squad that
    merely looks plausible. Returns a list of violations (empty means legal)."""
    squad = result["squad"]
    problems = []
    if len(squad) != SQUAD_SIZE:
        problems.append(f"squad has {len(squad)} players, expected {SQUAD_SIZE}")

    for pos, need in SQUAD_QUOTA.items():
        have = sum(1 for p in squad if p["pos"] == pos)
        if have != need:
            problems.append(f"{have} {pos} in squad, expected {need}")

    starters = [p for p in squad if p["starting"]]
    if len(starters) != XI_SIZE:
        problems.append(f"{len(starters)} starters, expected {XI_SIZE}")
    for pos in SQUAD_QUOTA:
        n = sum(1 for p in starters if p["pos"] == pos)
        if not (XI_MIN[pos] <= n <= XI_MAX[pos]):
            problems.append(f"{n} {pos} starting, legal range {XI_MIN[pos]}-{XI_MAX[pos]}")

    spend = round(sum(p["cost"] for p in squad), 1)
    if spend > budget + 1e-6:
        problems.append(f"squad costs £{spend}m, over the £{budget}m budget")

    clubs = {}
    for p in squad:
        clubs[p["team"]] = clubs.get(p["team"], 0) + 1
    for team, n in clubs.items():
        if team is not None and n > max_per_club:
            problems.append(f"{n} players from club {team}, max is {max_per_club}")

    if sum(1 for p in squad if p["is_captain"]) != 1:
        problems.append("expected exactly one captain")
    if any(p["is_captain"] and not p["starting"] for p in squad):
        problems.append("captain is on the bench")
    if len({p["element_id"] for p in squad}) != len(squad):
        problems.append("duplicate player in squad")
    return problems
