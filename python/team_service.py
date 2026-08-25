"""
Team view service for FPL Companion.

Everything here uses the PUBLIC FPL API (no login), so it can READ a manager's
team, points, bank, leagues and history - but it CANNOT write changes back to
FPL (transfers/lineup changes need an authenticated session). The optimise /
captain / transfer endpoints therefore return recommendations only; applying
them to the real team is done by the user in the FPL app.

Free-transfer count and "chips available" are not exposed by the public API, so
both are DERIVED here (walk the history / infer from used chips) and flagged as
estimates in the response.
"""

from datetime import datetime, timezone

import pandas as pd
import requests

import chip_model
import fixture_structure
import seasons
from ai_manager import free_transfers as step_free_transfers
from fetch_data import get_bootstrap_data
from squad_optimiser import (SELECTABLE_STATUS, predicted_for_gameweek,
                             team_rating)

BASE = "https://fantasy.premierleague.com/api"

# Position ids -> short labels, matching FPL's element_type.
POS_SHORT = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# Formation legality for a starting XI (1 GK is always fixed separately).
MIN_OUTFIELD = {"DEF": 3, "MID": 2, "FWD": 1}
MAX_OUTFIELD = {"DEF": 5, "MID": 5, "FWD": 3}

# One standard set of chips per half of the season - FPL hands the whole set
# back after GW19, so "used" is always a question about one half. Names come
# from chip_model so there is a single definition of what a chip is called.
ALL_CHIPS = dict(chip_model.CHIP_NAMES)


def _get(url):
    """GET returning parsed JSON, or None on any HTTP/connection error."""
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return None


def _num(v):
    """Parse a possibly-string numeric field (e.g. FPL 'form') to float or None.

    NaN counts as missing and comes back as None. A missing value arrives as
    NaN far more often than as None here - it is what pandas puts in an empty
    cell, and FPL leaves whole columns empty for most of the pool - and NaN is
    the worse of the two to hand back, because it survives every check a caller
    writes for an absent value. `x is not None` passes, `if x` passes, and every
    comparison against a threshold quietly returns False, so the value doesn't
    read as missing anywhere: it reads as a number that fails every test.

    That is not hypothetical. `chance_of_playing_next_round` is null for every
    fit player (498 of 581 rows), and returning NaN for it made
    gw_report._is_available() answer "not available" for the entire pool - the
    briefing's three recommendation sections were drawn from the dozen players
    FPL had explicitly stamped 100%."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


# detect_mode() lives in gameweek.py now - it's part of the season clock, which
# the scheduled jobs also depend on, and keeping one events[] reader avoids two
# modules disagreeing about what gameweek it is.

_TEAM_SHORT = None


def _team_short_map():
    """team id -> short name (e.g. 1 -> 'ARS'), fetched once and cached."""
    global _TEAM_SHORT
    if _TEAM_SHORT is None:
        data = _get(f"{BASE}/bootstrap-static/")
        _TEAM_SHORT = {t["id"]: t["short_name"] for t in (data or {}).get("teams", [])}
    return _TEAM_SHORT or {}


_TEAM_NAMES = None


def team_name_map():
    """team id -> full club name (e.g. 1 -> 'Arsenal').

    Goes through get_bootstrap_data() rather than the API directly, so it falls
    back to the on-disk cache during an outage: the short names are enough for a
    pitch tile, but a page that says "a their club midfielder" is not something
    to serve to a reader."""
    global _TEAM_NAMES
    if _TEAM_NAMES is None:
        data = get_bootstrap_data() or {}
        _TEAM_NAMES = {t["id"]: t["name"] for t in data.get("teams", [])}
    return _TEAM_NAMES or {}


def _build_element_index(position_dfs):
    """element_id -> everything the front end needs about that player, pulled
    from the already-rated position DataFrames (so ratings/predictions come for
    free without another model run)."""
    index = {}
    short = _team_short_map()
    for pos_name, df in position_dfs.items():
        for _, row in df.iterrows():
            next_gws = row.get("next_gameweeks")
            next_pts = None
            if isinstance(next_gws, list) and next_gws:
                next_pts = next_gws[0].get("points")

            predicted = next_pts
            if predicted is None:
                pp = row.get("predicted_points")
                predicted = float(pp) if pp is not None and pp == pp else 0.0

            # `id` is reassigned by FPL every summer, so it can't be part of a
            # URL that's meant to survive - last season's /player/427 would come
            # back in August as a different footballer. `code` is the same
            # number for a player for as long as they're in the game, which is
            # what a permanent page has to be keyed on.
            code = row.get("code")
            index[int(row["id"])] = {
                "id": int(row["id"]),
                "code": int(code) if code is not None and code == code else None,
                "web_name": row.get("web_name", ""),
                "first_name": row.get("first_name", "") or "",
                "second_name": row.get("second_name", "") or "",
                "pos": POS_SHORT.get(int(row["element_type"]), "?"),
                "element_type": int(row["element_type"]),
                "team_code": int(row["team_code"]) if row.get("team_code") == row.get("team_code") else None,
                "team": int(row["team"]) if row.get("team") == row.get("team") else None,
                "team_name": short.get(int(row["team"])) if row.get("team") == row.get("team") else None,
                "form": _num(row.get("form")),
                # Percentage of FPL managers who own him. FPL publishes it as a
                # string ("26.8"), hence _num. Displayed on the Players tab, and
                # the one number on that table the model has no part in - it is
                # what everyone else has done, not what anyone should do.
                "owned": _num(row.get("selected_by_percent")),
                "cost": round(float(row["now_cost"]) / 10, 1) if row.get("now_cost") is not None else None,
                "rating": round(float(row["rating"]), 1) if row.get("rating") == row.get("rating") else 0.0,
                "predicted": round(float(predicted), 1),
                "status": row.get("status", "a"),
                "news": row.get("news", "") or "",
                # Drives the optimiser's risk adjustment and its bench cover.
                "chance_of_playing_next_round": (
                    float(row["chance_of_playing_next_round"])
                    if row.get("chance_of_playing_next_round") == row.get("chance_of_playing_next_round")
                       and row.get("chance_of_playing_next_round") is not None else None),
                # NOT truncated to 3: the AI Manager plans over a longer
                # horizon, and the front end slices to 3 itself for display.
                "next_gameweeks": (next_gws if isinstance(next_gws, list) else []),
            }
    return index


def _estimate_free_transfers(history):
    """Free transfers going into the gameweek AFTER the last one played.

    The rule itself is not restated here - it is `ai_manager.free_transfers`,
    which the bot has always used and which is the same rule for a human. There
    were two copies of it and they disagreed, which is what produced "3" in a
    week where the answer is 1. One copy now.

    What IS this function's own is where the walk starts. GW1 has unlimited
    transfers, so it neither consumes an allowance nor banks one: a manager who
    has played only GW1 has exactly one free transfer for GW2, however many
    changes they made before the first deadline. The old walk counted GW1 like
    any other round AND added a further transfer on the way out, so it ran two
    ahead of FPL from the first week of the season.
    """
    rounds = sorted(((history or {}).get("current") or []),
                    key=lambda r: r.get("event") or 0)
    if not rounds:
        return 1

    # A wildcard or free hit week costs no transfer however many are made in it.
    # Same list `_chips_state` reads, so the two cannot disagree about which
    # weeks those were.
    free_rounds = {c.get("event") for c in ((history or {}).get("chips") or [])
                   if c.get("name") in ("wildcard", "freehit")}

    ft = 1                       # what GW1 leaves you with, before any of GW2 on
    for row in rounds[1:]:
        on_chip = row.get("event") in free_rounds
        made = 0 if on_chip else (row.get("event_transfers") or 0)
        ft = step_free_transfers(ft, made, wildcard_or_freehit=on_chip)
    return ft


def _total_points_at(history, event):
    """Cumulative points AS AT `event`, not the season total.

    `entry.summary_overall_points` - what the page header carries - is today's
    running total and nothing else. Shown above a squad the user has navigated
    back to, it states a number that was not true in that gameweek: step back to
    GW3 and the total sat there is still the one from GW38.

    FPL already publishes the honest figure per round in the history the caller
    has fetched anyway, so this is a lookup rather than a sum - which matters,
    because adding up `points` would quietly diverge from FPL's own total by the
    transfer hits it already has deducted.

    None when the round isn't in the history: a squad can be viewed before its
    round has been scored, and no number is better than last week's."""
    for row in (history or {}).get("current") or []:
        if row.get("event") == event:
            return row.get("total_points")
    return None


def _chips_state(history, event=None):
    """Which chips this manager has spent, and which are still in hand.

    Scoped to the half of the season `event` falls in. FPL returns the whole set
    after GW19, so a wildcard played in GW6 is spent for the first half and back
    in hand for the second; reading the history flat says the manager has no
    wildcard for the rest of the year, which is wrong from GW20 onwards. Each
    entry in history["chips"] carries the event it was played in, so the split
    is available rather than assumed.
    """
    played = [(c.get("event"), c.get("name")) for c in
              ((history or {}).get("chips") or []) if c.get("name")]
    if event is None:
        used = [name for _gw, name in played]
    else:
        start, end = fixture_structure.chip_half(event)
        used = [name for gw, name in played
                if gw is not None and start <= int(gw) <= end]
    available = [label for key, label in ALL_CHIPS.items() if key not in used]
    used_labels = [ALL_CHIPS.get(name, name) for name in used]
    return used_labels, available


def _chip_advice(squad, event, chips_available, outlook=None):
    """What each chip the manager still holds is worth to THIS squad, now.

    The same measurements the AI Manager's planner uses, minus the solver. Free
    Hit and Wildcard need an integer program each, and this runs on a page load
    rather than once a week in a cron job - a My Team request is not the place
    to spend two seconds in CBC. So those two are described by the things that
    actually drive them (how much of the squad blanks, how much of it is
    injured or contributing nothing) rather than by a rebuilt squad, and the
    bot remains the only one that gets the full treatment.

    Every figure is stated against what the same chip returned in the 2025-26
    simulation, because a bench projecting 14 points means nothing on its own.
    """
    priors = chip_model.load_priors()
    starters = [p for p in squad if p.get("starting")]
    bench = [p for p in squad if not p.get("starting")]
    captain = next((p for p in starters if p.get("is_captain")), None)
    lineup = {"squad": squad}
    context = fixture_structure.context((outlook or {}).get(event))
    blanking = [p for p in squad if not any(
        e.get("event") == event for e in (p.get("next_gameweeks") or []))]

    codes = {label: code for code, label in ALL_CHIPS.items()}
    available = {codes.get(label, label) for label in (chips_available or [])}

    out = []
    for chip in chip_model.CHIP_CODES:
        if chip not in available:
            continue
        factors, detail = {}, ""
        if chip == "bboost":
            gain = chip_model.bench_boost_gain(lineup, event)
            detail = f"Your bench projects {round(gain, 1)} pts"
        elif chip == "3xc":
            gain = chip_model.triple_captain_gain(lineup, event)
            who = (captain or {}).get("web_name", "your captain")
            detail = f"{who} projects {round(gain, 1)} extra pts on top of the double"
        elif chip == "freehit":
            gain = sum(p.get("predicted") or 0.0 for p in blanking)
            detail = (f"{len(blanking)} of your 15 have no fixture this gameweek"
                      if blanking else "Every one of your 15 has a fixture")
        else:
            factors = chip_model.wildcard_factors(squad, event)
            gain = float(factors["injured"] + factors["deadweight"])
            detail = (f"{factors['injured']} injured, {factors['deadweight']} "
                      f"contributing nothing over the next 4 gameweeks")

        entry = {
            "chip": chip,
            "name": chip_model.chip_name(chip),
            "gain": round(float(gain), 1),
            "context": context,
            "detail": detail,
            "factors": factors,
        }
        # Only Bench Boost and Triple Captain are measured here in the units
        # the floors are in, so only they get a verdict against one. The other
        # two are described rather than thresholded - and neither gets a "last
        # season returned N" line, because their simulated figure is a
        # squad-over-eight-gameweeks delta, not points on a Saturday. Printing
        # it beside a bench total would invite exactly the wrong comparison.
        if chip in ("bboost", "3xc"):
            floor = chip_model.floor(chip, priors)
            entry.update({
                "floor": round(floor, 1),
                "percentile": chip_model.percentile_of(chip, gain, priors),
                "realised_median": chip_model.realised_median(chip, priors),
                "verdict": "play" if gain >= floor else "hold",
            })
        else:
            entry.update({"floor": None, "percentile": None,
                          "realised_median": None,
                          "verdict": "consider" if gain else "hold"})
        out.append(entry)
    return out


def _optimise(squad):
    """Pick the best legal starting XI by predicted points, then order the bench.

    Rules enforced: exactly 1 GK starts, min 3 DEF / 2 MID / 1 FWD, 11 total.
    Bench = the other GK (always bench slot 1) + the 3 weakest remaining
    outfielders, ordered best-first so auto-subs bring the strongest on."""
    gks = sorted([p for p in squad if p["pos"] == "GK"], key=lambda p: -p["predicted"])
    outs = {pos: sorted([p for p in squad if p["pos"] == pos], key=lambda p: -p["predicted"])
            for pos in ("DEF", "MID", "FWD")}

    if len(gks) < 2 or sum(len(v) for v in outs.values()) < 10:
        return None  # not a full 15-man squad; skip optimisation

    start_gk, bench_gk = gks[0], gks[1]

    # Lock in the minimum at each outfield position, then fill the 4 open slots
    # with the highest predicted players left, respecting the max per position.
    starters, counts = [], {"DEF": 0, "MID": 0, "FWD": 0}
    for pos, need in MIN_OUTFIELD.items():
        starters += outs[pos][:need]
        counts[pos] = need

    pool = []
    for pos in outs:
        pool += outs[pos][MIN_OUTFIELD[pos]:]
    pool.sort(key=lambda p: -p["predicted"])

    for p in pool:
        if len(starters) >= 10:
            break
        if counts[p["pos"]] < MAX_OUTFIELD[p["pos"]]:
            starters.append(p)
            counts[p["pos"]] += 1

    starter_ids = {p["id"] for p in starters} | {start_gk["id"]}
    bench_out = sorted([p for p in squad if p["id"] not in starter_ids and p["pos"] != "GK"],
                       key=lambda p: -p["predicted"])

    # Present starters grouped GK->DEF->MID->FWD for a natural pitch layout.
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    starting = sorted([start_gk] + starters, key=lambda p: (order[p["pos"]], -p["predicted"]))
    return {
        "starting": [p["id"] for p in starting],
        "bench": [bench_gk["id"]] + [p["id"] for p in bench_out],
    }


def _transfer_recs(squad, index, bank, free_transfers, max_recs=3):
    """Swap the weakest-rated players for the best affordable upgrade in the
    same position that isn't already owned. Budget-aware (bank + funds freed by
    selling). NB: uses now_cost as a proxy for selling price, and does not check
    the 3-per-club limit - treat as suggestions to sanity-check in the app."""
    owned = {p["id"] for p in squad}
    by_pos = {}
    for p in index.values():
        # Candidates are ranked on `rating`, which is a percentile of the
        # model's output and knows nothing about fitness - so without this
        # check the panel would cheerfully tell you to buy a player who is out
        # injured, and rank him highly for it. Doubtful players stay in: a
        # 75%-fit premium can still be the right move, and that's a judgement
        # for the reader. Same letters the optimiser treats as selectable.
        if str(p.get("status") or "a").lower() not in SELECTABLE_STATUS:
            continue
        by_pos.setdefault(p["pos"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: -p["rating"])

    budget = bank
    recs = []
    for weak in sorted(squad, key=lambda p: p["rating"]):
        if len(recs) >= max_recs:
            break
        affordable = weak["cost"] + budget
        for cand in by_pos.get(weak["pos"], []):
            if cand["id"] in owned:
                continue
            if cand["rating"] <= weak["rating"]:
                break  # list is sorted, nothing better remains
            if cand["cost"] <= affordable:
                recs.append({
                    "out": weak, "in": cand,
                    "rating_gain": round(cand["rating"] - weak["rating"], 1),
                    "cost_change": round(cand["cost"] - weak["cost"], 1),
                    # True for a freshly loaded page and nothing more. The
                    # allowance is spent client-side as the reader makes
                    # transfers, so app.js recomputes the free/-4 tag from what
                    # is LEFT every time it draws the list - see
                    # renderTransfers. Kept here because it is the right answer
                    # at render time and the only one a non-JS reader gets.
                    "free": len(recs) < free_transfers,
                })
                owned.add(cand["id"])
                budget -= (cand["cost"] - weak["cost"])
                break
    return recs


# Name fragments that mark a public league as broadcaster-run rather than a
# core FPL global league. Heuristic - the API has no explicit flag - so easy to
# extend once you see the real names your account is in.
BROADCASTER_HINTS = ("sky", "espn", "the sun", "talksport", "bbc", "itv",
                     "tnt", "bt sport", "guardian", "telegraph", "daily mail",
                     "mirror", "premier league")


def _categorise_leagues(entry):
    """Split classic leagues into personal / general / broadcaster.
    In the FPL API, league_type 's' = system/auto public leagues (Overall,
    Gameweek, country...) => general; anything else is an invitational league
    you joined => personal. Broadcaster leagues are matched by name first."""
    groups = {"personal": [], "general": [], "broadcaster": []}
    for l in entry.get("leagues", {}).get("classic", []):
        item = {"id": l["id"], "name": l["name"], "rank": l.get("entry_rank")}
        name = (l.get("name") or "").lower()
        if any(h in name for h in BROADCASTER_HINTS):
            groups["broadcaster"].append(item)
        elif l.get("league_type") == "s":
            groups["general"].append(item)
        else:
            groups["personal"].append(item)
    return groups


def get_all_players(position_dfs):
    """Full rated player pool for the team builder / player search
    (id, cost, rating, club name, upcoming fixtures)."""
    if position_dfs is None:
        return {"players": []}
    idx = _build_element_index(position_dfs)
    # `status` is included because the squad optimiser must not build a team
    # around an injured or suspended player ('a' = available).
    players = [{"id": p["id"], "code": p.get("code"),
                "web_name": p["web_name"], "pos": p["pos"],
                "team_code": p["team_code"], "team": p.get("team"),
                "team_name": p.get("team_name"), "form": p.get("form"),
                "owned": p.get("owned"),
                "cost": p["cost"], "rating": p["rating"], "predicted": p["predicted"],
                "status": p.get("status", "a"), "news": p.get("news", ""),
                "chance_of_playing_next_round": p.get("chance_of_playing_next_round"),
                "next_gameweeks": p.get("next_gameweeks", [])}
               for p in idx.values()]
    return {"players": players}


_PREV_SEASON_UNDERPERF_STATS = None
# The five the performance-gap table needs, plus the ones a player page prints
# beside this season's. Widened rather than aggregated a second time elsewhere:
# the file is read once and cached, so the extra columns are free, and two
# separate summaries of the same CSV are two things to keep in step.
_PREV_STAT_COLS = ["expected_goals", "goals_scored", "expected_goals_conceded",
                   "goals_conceded", "minutes", "total_points", "assists",
                   "clean_sheets", "bonus", "starts", "expected_assists",
                   "yellow_cards", "red_cards"]

# What counts as having played last season at all. Two full matches - below
# that the totals are a cameo and printing them beside a full season invites a
# comparison the numbers cannot support.
PREV_SEASON_MIN_MINUTES = 180


def _prev_season_stats_by_code():
    """code -> {expected_goals, goals_scored, expected_goals_conceded,
    goals_conceded, minutes} summed across last season's full totals - the
    fallback source for underperforming-players before the current season
    has enough minutes played for anyone to judge (preseason, or the first
    few GWs). the previous season's gameweek_stats.csv is PER-GAMEWEEK (one row per player
    per match), so it has to be grouped and summed to get season totals -
    keeping only the last row per player would just be that player's most
    recent single match."""
    global _PREV_SEASON_UNDERPERF_STATS
    if _PREV_SEASON_UNDERPERF_STATS is None:
        try:
            prev = seasons.previous_season() or seasons.FIRST_TRAINING_SEASON
            stats = pd.read_csv(seasons.gameweek_stats_path(prev))
            players = pd.read_csv(seasons.players_path(prev))
        except (FileNotFoundError, OSError):
            _PREV_SEASON_UNDERPERF_STATS = {}
            return _PREV_SEASON_UNDERPERF_STATS
        id_to_code = dict(zip(players["id"], players["code"]))
        stats = stats.copy()
        stats["code"] = stats["player_id"].map(id_to_code)
        stats = stats.dropna(subset=["code"])
        for c in _PREV_STAT_COLS:
            stats[c] = pd.to_numeric(stats[c], errors="coerce")
        totals = stats.groupby("code")[_PREV_STAT_COLS].sum()

        # Appearances, so points-per-game can be shown on the same basis FPL
        # uses. Counted from rows with minutes rather than from row count: an
        # unused substitute has a gameweek row and did not appear in the match.
        played = stats[stats["minutes"] > 0].groupby("code").size()

        _PREV_SEASON_UNDERPERF_STATS = {}
        for code, row in totals.iterrows():
            rec = {c: row[c] for c in _PREV_STAT_COLS}
            games = int(played.get(code, 0))
            rec["appearances"] = games
            rec["points_per_game"] = (round(float(row["total_points"]) / games, 1)
                                      if games else None)
            _PREV_SEASON_UNDERPERF_STATS[int(code)] = rec
    return _PREV_SEASON_UNDERPERF_STATS


# One substantial appearance. 180 (two full matches) was the old floor and it
# is the right number for a settled season, but it cannot be met by anybody
# until the third gameweek - so for the opening fortnight the table silently
# described last season instead. 60 is a start or a long substitute appearance:
# thin evidence, which is what the first weeks of a season are, and the table
# prints the minutes beside every row so a reader can weigh it.
MIN_MINUTES_GAP = 60
MIN_MINUTES_GAP_PREV = 900


def get_performance_gap_players(position_dfs, direction="under", top_n=20,
                                min_minutes=MIN_MINUTES_GAP,
                                min_minutes_prev=MIN_MINUTES_GAP_PREV):
    """Players whose actual returns and underlying numbers disagree.

    `direction="under"` - doing worse than the underlying play deserves:
      - Midfielders/forwards: goals scored below expected goals (xG) -
        finishing worse than the chances they're getting deserve.
      - Goalkeepers/defenders: goals conceded above expected goals conceded
        (xGC) - the defence/keeper shipping more than the play suggests.

    `direction="over"` - the mirror image: goals ABOVE xG, goals conceded BELOW
    xGC. Only the sign of `diff` flips, which is why this takes a parameter
    rather than being copied.

    They are read differently, though, and the UI says so. An underperformer may
    be due a correction upward. An overperformer is NOT that claim in reverse
    with a buy signal attached - it is as often a finisher on a hot run as a
    sell signal, so the honest framing is "this may not hold", not "this will
    revert".

    Both are FPL's own underlying-stat fields, straight from bootstrap-static.

    THIS season's numbers, once this season has any. It used to fall back to
    last season per player for anyone under the minutes floor, which sounds
    like a gentle degradation and was not: for the first two gameweeks nobody
    could clear the floor, so every row came from last season while the page
    around it talked about the current one. The only signal that a table headed
    "underperforming" was describing a season that had finished was a small
    "last season" label on each row.

    So the fallback is now all-or-nothing and keyed on whether the season has
    started at all. Before the opener there is nothing else to show and last
    season is the honest answer; after it, a thin sample of the season the
    reader is actually playing beats a complete sample of one they are not.

    top_n is applied PER group (attackers, defenders) rather than to a single
    combined-then-sorted list - otherwise one group's generally larger diffs
    (e.g. keepers/defenders often show bigger xGC gaps than attackers show xG
    gaps) could crowd the other group out of the top_n entirely."""
    if position_dfs is None:
        return {"results": []}
    over = direction == "over"
    short = _team_short_map()

    # Has anyone kicked a ball this season? Asked of the pool rather than of
    # the season clock, so this needs no network call and cannot disagree with
    # the very numbers it is about to read.
    season_started = any(
        (_num(r.get("minutes")) or 0) > 0
        for df in position_dfs.values() for _, r in df.iterrows())

    prev_stats = {} if season_started else _prev_season_stats_by_code()
    attacking_rows, defensive_rows = [], []
    for pos_name, df in position_dfs.items():
        attacking = pos_name in ("Midfielder", "Forward")
        rows = attacking_rows if attacking else defensive_rows
        for _, r in df.iterrows():
            minutes = _num(r.get("minutes")) or 0
            source, used_fallback = r, False
            if minutes < min_minutes:
                if season_started:
                    continue        # too little of THIS season to say anything
                code = int(r["code"]) if r.get("code") == r.get("code") else None
                fb = prev_stats.get(code) if code is not None else None
                fb_minutes = (fb or {}).get("minutes") or 0
                if not fb or fb_minutes < min_minutes_prev:
                    continue
                source, used_fallback, minutes = fb, True, fb_minutes
            if attacking:
                expected = _num(source.get("expected_goals"))
                actual = _num(source.get("goals_scored"))
                metric = "Goals vs xG"
            else:
                expected = _num(source.get("expected_goals_conceded"))
                actual = _num(source.get("goals_conceded"))
                metric = "Conceded vs xGC"
            if expected is None or actual is None:
                continue
            # Signed so that a POSITIVE diff always means "more of the thing
            # this table is about", whichever table it is. Under: goals missing
            # against xG, or goals shipped above xGC. Over: the same two
            # subtractions the other way round.
            diff = (expected - actual) if attacking else (actual - expected)
            if over:
                diff = -diff
            if diff <= 0:
                continue  # on the other side of expectation - the other table's row
            next_gws = r.get("next_gameweeks")
            rows.append({
                "id": int(r["id"]), "web_name": r.get("web_name", ""),
                "pos": POS_SHORT.get(int(r["element_type"]), "?"),
                "team_code": int(r["team_code"]) if r.get("team_code") == r.get("team_code") else None,
                "team_name": short.get(int(r["team"])) if r.get("team") == r.get("team") else None,
                "cost": round(float(r["now_cost"]) / 10, 1) if r.get("now_cost") is not None else None,
                "metric": metric,
                "expected": round(expected, 2),
                "actual": round(actual, 2),
                "diff": round(diff, 2),
                "minutes": int(minutes),
                "season": "last season" if used_fallback else "this season",
                "next_gameweeks": next_gws[:3] if isinstance(next_gws, list) else [],
            })
    attacking_rows.sort(key=lambda x: -x["diff"])
    defensive_rows.sort(key=lambda x: -x["diff"])
    return {"results": attacking_rows[:top_n] + defensive_rows[:top_n],
            "direction": "over" if over else "under",
            # Which season the whole table is about. Per-row `season` labels
            # stay, because they are what a reader sees; this is for the header
            # above them, which previously could not say.
            "season": "this season" if season_started else "last season",
            "min_minutes": min_minutes if season_started else min_minutes_prev}


def get_underperforming_players(position_dfs, **kwargs):
    """Kept as its own name because it is what the rest of the app asks for,
    and because "underperforming" is the word on the page."""
    return get_performance_gap_players(position_dfs, direction="under", **kwargs)


def get_overperforming_players(position_dfs, **kwargs):
    """Returns ahead of the underlying play - see get_performance_gap_players
    for why this is not simply a buy signal inverted."""
    return get_performance_gap_players(position_dfs, direction="over", **kwargs)


def get_player_summary(player_id, n=6):
    """Recent per-gameweek performance for one player (plus last few seasons),
    for the player pop-up. Current-season history is empty until the season
    starts, in which case history_past carries the story."""
    from fetch_data import get_player_history
    data = get_player_history(player_id)
    if not data or "history" not in data:
        return {"available": False, "history": [], "history_past": []}
    short = _team_short_map()
    hist = data.get("history", [])
    rows = [{"event": h.get("round"), "opponent": short.get(h.get("opponent_team"), "?"),
             "was_home": h.get("was_home"), "points": h.get("total_points"),
             "minutes": h.get("minutes")} for h in hist[-n:]]
    past = [{"season": p.get("season_name"), "points": p.get("total_points")}
            for p in data.get("history_past", [])[-3:]]
    return {"available": True, "history": rows, "history_past": past}


def get_team_view(team_id, event, position_dfs, next_event=None,
                  carry_forward=True):
    """Assemble the whole Team tab payload for a manager id + gameweek.

    `next_event` is the gameweek being picked for, from the season clock. It is
    the default target rather than `current_event`, because the round you can
    still change is the one you came here to change - see the carry-forward note
    further down for why that view has to be assembled rather than fetched.

    `carry_forward` is the switch on that behaviour, and callers that are
    RECORDING rather than displaying must turn it off. The deadline watcher is
    the one that matters: it snapshots official picks moments after a deadline,
    when FPL has been known to still report the previous `current_event` while
    already serving the new round's picks. If that fetch hiccupped, a
    carried-forward squad would be written down as somebody's official team for
    a gameweek they picked differently - a wrong answer that looks exactly like
    a right one. Displaying last week's squad as a starting point is helpful;
    filing it as fact is not.
    """
    if position_dfs is None:
        return {"available": False, "detail": "Ratings not loaded yet."}

    entry = _get(f"{BASE}/entry/{team_id}/")
    if entry is None:
        return {"available": False, "detail": f"Couldn't find manager {team_id}. Check the ID."}

    current_event = entry.get("current_event")
    event = event or next_event or current_event
    max_event = next_event or current_event
    index = _build_element_index(position_dfs)

    # Leagues + header basics are available even in preseason.
    leagues = _categorise_leagues(entry)
    header = {
        "name": entry.get("name", ""),
        "manager": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
        "total_points": entry.get("summary_overall_points"),
        "bank": round((entry.get("last_deadline_bank") or 0) / 10, 1),
        "value": round((entry.get("last_deadline_value") or 0) / 10, 1),
    }

    if not event:
        return {"available": False, "detail": "No gameweek has started yet (preseason).",
                "header": header, "leagues": leagues,
                "current_event": current_event, "min_event": 1,
                "max_event": max_event}

    # FPL does not publish a manager's picks for a gameweek until its deadline
    # has passed - /entry/{id}/event/{next}/picks/ is a flat 404 - so the round
    # you can actually still change is the one round the API cannot hand over.
    # Refusing to show it left the tab able to display only locked gameweeks,
    # which is the wrong half of the season to be able to edit.
    #
    # So it is carried forward: the last squad FPL does have, re-priced against
    # the target gameweek's own projections. That is what the manager owns going
    # into it, and it is the honest starting point for planning - overwritten by
    # their real picks the moment the deadline passes and FPL starts answering.
    carried_from = None
    picks_data = _get(f"{BASE}/entry/{team_id}/event/{event}/picks/")
    if (picks_data is None or "picks" not in picks_data)             and carry_forward and current_event and event > current_event:
        picks_data = _get(f"{BASE}/entry/{team_id}/event/{current_event}/picks/")
        if picks_data is not None and "picks" in picks_data:
            carried_from = current_event
    if picks_data is None or "picks" not in picks_data:
        return {"available": False,
                "detail": f"Team for GW{event} isn't available yet (picks appear once the gameweek is live).",
                "header": header, "leagues": leagues,
                "current_event": current_event, "min_event": 1,
                "max_event": max_event}

    history = _get(f"{BASE}/entry/{team_id}/history/")
    free_transfers = _estimate_free_transfers(history)
    chips_used, chips_available = _chips_state(history, event)

    eh = picks_data.get("entry_history", {})
    bank = round((eh.get("bank") or 0) / 10, 1)

    # Merge each pick with its rated player info.
    #
    # `predicted` is re-read for the gameweek actually on screen rather than
    # taken from the index, where it is `next_gameweeks[0]`. That list is built
    # from unfinished fixtures and rolls forward per TEAM as matches complete,
    # so mid-round it holds this gameweek for the clubs still to play and the
    # next one for the clubs that have finished - eleven players projected
    # against two different gameweeks at once. Matching on the event fixes them
    # all to the same round. None means the pool has nothing for that gameweek
    # (a past round; its fixtures are long gone), and the index value is the
    # best available answer there.
    squad = []
    for pk in picks_data["picks"]:
        info = index.get(pk["element"])
        if info is None:
            continue
        for_gw = predicted_for_gameweek(info, event)
        squad.append({**info,
                      "predicted": round(for_gw, 1) if for_gw is not None
                                   else info.get("predicted"),
                      "position": pk["position"],           # 1-11 start, 12-15 bench
                      "starting": pk["position"] <= 11,
                      "is_captain": pk["is_captain"],
                      "is_vice_captain": pk["is_vice_captain"],
                      "multiplier": pk["multiplier"]})

    # Captain / vice recommendation: highest predicted points in the squad.
    ranked = sorted(squad, key=lambda p: -p["predicted"])
    recommended = {"captain": ranked[0]["id"] if ranked else None,
                   "vice": ranked[1]["id"] if len(ranked) > 1 else None}

    # Predicted GW points = sum of starters' predictions, current captain doubled.
    predicted_gw = 0.0
    for p in squad:
        if p["starting"]:
            predicted_gw += p["predicted"] * (2 if p["is_captain"] else 1)

    return {
        "available": True,
        "header": header,
        "gw": {
            "event": event,
            # Everything FPL reports per round belongs to the round the picks
            # came FROM. On a carried-forward view that is a different gameweek
            # to the one on screen, and stating last week's score, transfers and
            # chip under next week's heading would be wrong on every count -
            # so they are what they truly are for a gameweek not yet played:
            # nothing has happened in it. The cumulative total still stands,
            # because that is a season-to-date figure and it is up to date.
            "points": None if carried_from else eh.get("points"),
            # Cumulative as at THIS round, so stepping back through the season
            # shows what the total actually was then. See _total_points_at.
            "total_points": _total_points_at(history, carried_from or event),
            "predicted_points": round(predicted_gw, 1),
            "bank": bank,
            "value": round((eh.get("value") or 0) / 10, 1),
            "transfers_made": 0 if carried_from else eh.get("event_transfers"),
            "transfers_cost": 0 if carried_from else eh.get("event_transfers_cost"),
            "free_transfers_est": free_transfers,
            "active_chip": None if carried_from else picks_data.get("active_chip"),
            "chips_used": chips_used,
            "chips_available": chips_available,
            # What each held chip is worth to this squad this week, measured
            # against what the same chip returned in the 2025-26 simulation.
            "chip_advice": _chip_advice(squad, event, chips_available,
                                        fixture_structure.season_outlook()),
            # Mean rating of the eleven that start. Only stated for the round
            # being picked and the one in play: ratings are rebuilt nightly, so
            # scoring someone's GW3 side with today's numbers would rate a team
            # on information nobody had when it was picked.
            "team_rating": (team_rating(squad)
                            if current_event is None or event >= current_event else None),
        },
        "squad": squad,
        "recommended": recommended,
        "optimised": _optimise(squad),
        "transfer_recs": _transfer_recs(squad, index, bank, free_transfers),
        "leagues": leagues,
        "current_event": current_event,
        "min_event": 1,
        # How far the forward arrow may go. The client used to cap itself at
        # current_event, which is precisely the round it cannot change.
        "max_event": max_event,
        # Set when these picks are last week's, shown for a gameweek that has
        # not had its deadline yet. The banner that says so is the only thing
        # standing between a carried-forward squad and being read as a
        # confirmed team - so it is stated in the payload, not inferred.
        "carried_from": carried_from,
    }


def get_news_feed(limit=20):
    """Most recent injury/transfer blurbs, straight from FPL's own per-player
    'news' field (the same text the FPL site shows next to a flagged player).
    No separate news source needed - every flagged player already carries one.

    Always hits the live API rather than the cached players_full.csv, so the
    feed is as fresh as FPL's own site. Note that FPL only stamps news_added
    when a player's flag CHANGES, so a quiet week genuinely produces no new
    items - the feed looking static isn't necessarily a bug.

    'added' is the full ISO timestamp (news_added) so the front end can show a
    time as well as a date; 'date' is kept as the plain YYYY-MM-DD."""
    data = _get(f"{BASE}/bootstrap-static/")
    if data is None:
        return {"available": False, "stories": []}
    short = _team_short_map()
    items = [e for e in data.get("elements", []) if (e.get("news") or "").strip()]
    items.sort(key=lambda e: e.get("news_added") or "", reverse=True)
    stories = [{
        "player": e.get("web_name", ""),
        "team": short.get(e.get("team"), ""),
        "team_code": e.get("team_code"),
        "headline": e.get("news", ""),
        "added": e.get("news_added"),
        "date": (e.get("news_added") or "")[:10],
        "status": e.get("status"),
    } for e in items[:limit]]
    return {"available": True, "stories": stories,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def get_league_standings(league_id, page=1):
    """Top of a classic league's table (paged, 50 per page from FPL)."""
    data = _get(f"{BASE}/leagues-classic/{league_id}/standings/?page_standings={page}")
    if data is None:
        return {"available": False, "detail": "Couldn't load that league."}
    results = data.get("standings", {}).get("results", [])
    return {
        "available": True,
        "name": data.get("league", {}).get("name", ""),
        "standings": [{
            "rank": r["rank"], "entry_name": r["entry_name"],
            "manager": r["player_name"], "total": r["total"],
            "event_total": r.get("event_total"),
        } for r in results[:25]],
    }