"""What each chip was actually worth last season, by replaying it.

The chip planner needs two numbers the live game cannot give it: the least a
chip is worth spending (a floor), and what a chip typically returns in a week of
a given kind (a prior for gameweeks too far out to evaluate directly). FPL's API
exposes a manager's own chip history but nothing aggregate, and the leaderboard
only reaches back to the current season - so the question can only be answered
backwards, by simulating.

What makes that possible is `selected`, the per-gameweek ownership count. The
popular squad of any past gameweek can be reconstructed from it, and a chip
played into that squad scored against what those players really did. Not the top
10k, but a real squad real managers held.

Two populations are simulated for every gameweek of 2025-26:

  template   the most-owned legal 15. One squad per gameweek.
  plausible  ownership-weighted random legal 15s. A distribution built from one
             squad a week would describe that squad, not the decision.

There is deliberately no hindsight-optimal population: a floor set at what
perfect foresight returns is a floor no chip ever clears.

Two measurements come out of this, and keeping them apart is the whole design.

REALISED value is what a chip was worth last season - four bench players' actual
points, over all 38 gameweeks. It needs no forecast, so it uses every gameweek,
and it is the number worth showing a reader.

PLANNED value is what the planner's own arithmetic says a chip is worth: the
calculation chip_model runs live, replayed on the model's holdout rounds. The
floors are built from this, because a floor is only meaningful in the units of
what it is compared against, and the planner compares predicted points. The
model shrinks, so a floor set in actual points sits too high to ever clear.

The distinction matters most for Free Hit and Wildcard. Scoring a Free Hit as
"the best possible team that week minus what you had" measures hindsight, not a
chip - it comes out around 90 points a week. Both are measured only where a
genuine forecast exists: the last 8 rounds, which train_model.py holds out and
the saved bundles were never fitted on. The alternative squad is chosen on
predictions and scored on what happened, which is what playing the chip is.

Returns are split by week kind - double, blank, ordinary - because they are not
the same distribution. Every double and blank in 2025-26 landed after GW26, so a
planner valuing patience at the pooled 90th percentile would hold out all
through the first half for a week the fixture list did not contain.

Run it directly. Writes data/reference/chip_priors.json; prints the report.
"""

import json
import os
import random
import sys

import numpy as np
import pandas as pd

import fixture_structure
import seasons
import train_model
from squad_optimiser import (MAX_PER_CLUB, OptimisationError, SQUAD_QUOTA,
                             XI_MAX, XI_MIN, optimise_squad)

SIM_SEASON = "2025-26"
BUDGET = 100.0
SQUAD_SIZE = 15
XI_SIZE = 11

# Random legal squads per gameweek. Enough for a stable 90th percentile without
# making the run take minutes - 38 gameweeks x 12 is 456 squads per chip.
PLAUSIBLE_PER_ROUND = 12
# Ownership-weighted sampling draws from this many candidates per position.
# Wide enough that squads differ, narrow enough that they stay recognisable as
# teams somebody might really have owned.
SAMPLE_DEPTH = 30
RANDOM_SEED = 0

# The wildcard question is "is this squad worth keeping", so it is scored over
# the same horizon the AI Manager owns players on.
WILDCARD_HORIZON = 8
WILDCARD_DECAY = 0.9

# Distinct GAMEWEEKS a context needs before its ratio is believed - not sample
# rows, which is the trap: thirteen squads all drawn from the same single double
# gameweek look like thirteen observations and are really one. 2025-26 had
# exactly one double and one blank, so on this data neither qualifies, and the
# ratios are correctly dropped rather than published. That costs the planner
# nothing: a double gameweek already shows up in the computed gain, because the
# doubling players carry two fixtures and their points are summed. The prior is
# only consulted for gameweeks past the prediction horizon, and FPL has not
# scheduled those doubles yet either.
MIN_CONTEXT_GAMEWEEKS = 2

QUANTILES = {"p25": 0.25, "p50": 0.50, "p75": 0.75, "p90": 0.90}

POS_SHORT = {"Goalkeeper": "GK", "Defender": "DEF",
             "Midfielder": "MID", "Forward": "FWD"}


# ---- Season table ---------------------------------------------------------

def load_rounds(season=SIM_SEASON):
    """Per-round rows with everything a squad needs: points, price, ownership."""
    df = train_model.load_season(season)
    if df is None or df.empty:
        raise RuntimeError(f"No usable gameweek stats for {season} at "
                           f"{seasons.gameweek_stats_path(season)}")
    df = train_model.attach_position(df)
    df["pos"] = df["position"].map(POS_SHORT)
    df = df[df["pos"].notna()].copy()

    # FPL prices are tenths of a million; `selected` is a raw owner count.
    df["cost"] = pd.to_numeric(df["value"], errors="coerce") / 10.0
    df["owned"] = pd.to_numeric(df.get("selected"), errors="coerce").fillna(0.0)
    df["points"] = pd.to_numeric(df["total_points"], errors="coerce").fillna(0.0)
    df["club"] = df["team"].astype(str)
    df = df[df["cost"].notna() & (df["cost"] > 0)]
    return df


def round_players(rows, score_col="points"):
    """One gameweek's rows as the dicts squad_optimiser expects."""
    gw = int(rows["round"].iloc[0])
    return [{
        "id": int(r.player_id),
        "web_name": str(r.name),
        "pos": r.pos,
        "team": r.club,
        "cost": float(r.cost),
        "status": "a",
        "owned": float(r.owned),
        "points": float(r.points),
        "next_gameweeks": [{"event": gw, "points": float(getattr(r, score_col))}],
    } for r in rows.itertuples()]


# ---- Squad construction ---------------------------------------------------

def greedy_squad(players, key, budget=BUDGET):
    """A legal 15 picked best-first on `key`, never overspending.

    Not an ILP: this runs hundreds of times and only has to produce a squad a
    human might plausibly own, not an optimal one. But best-first on ownership
    alone picks fifteen premiums and blows the budget by miles, and a squad
    repaired afterwards from that state is neither optimal nor plausible. So
    affordability is checked as it goes: a player is only taken if the cheapest
    legal way to fill the slots still empty leaves enough money to do it. That
    is the constraint that actually shapes a real squad - you buy Haaland by
    deciding which four players become £4.5m, not by buying him and hoping.
    """
    by_key = {}
    by_cost = {}
    for p in players:
        by_key.setdefault(p["pos"], []).append(p)
    for pos, group in by_key.items():
        group.sort(key=key, reverse=True)
        by_cost[pos] = sorted(group, key=lambda p: p["cost"])

    def cheapest_fill(need, used):
        """Least it can cost to fill `need` slots per position, ignoring nobody
        already taken. Ignores the club cap - a lower bound, which is all the
        feasibility check needs."""
        total = 0.0
        for pos, count in need.items():
            if count <= 0:
                continue
            picked = [p["cost"] for p in by_cost.get(pos, []) if p["id"] not in used][:count]
            if len(picked) < count:
                return None
            total += sum(picked)
        return total

    squad, clubs, used, spent = [], {}, set(), 0.0
    need = dict(SQUAD_QUOTA)
    for pos in ("GK", "DEF", "MID", "FWD"):
        for _ in range(SQUAD_QUOTA[pos]):
            need[pos] -= 1
            chosen = None
            for p in by_key.get(pos, []):
                if p["id"] in used or clubs.get(p["team"], 0) >= MAX_PER_CLUB:
                    continue
                rest = cheapest_fill(need, used | {p["id"]})
                if rest is None:
                    continue
                if spent + p["cost"] + rest <= budget:
                    chosen = p
                    break
            if chosen is None:
                return None
            squad.append(chosen)
            used.add(chosen["id"])
            clubs[chosen["team"]] = clubs.get(chosen["team"], 0) + 1
            spent += chosen["cost"]
    return squad


def template_squad(players):
    """The most-owned legal 15 - the closest thing to "what everyone had"."""
    return greedy_squad(players, key=lambda p: p["owned"])


def plausible_squad(players, rng):
    """An ownership-weighted random legal 15.

    Weighted rather than uniform because a squad drawn uniformly from 500
    players is not a squad anybody owned, and the point of the exercise is to
    describe decisions real managers faced.
    """
    jitter = {p["id"]: rng.random() for p in players}
    return greedy_squad(players, key=lambda p: p["owned"] * (0.4 + jitter[p["id"]]))


def pick_xi(squad, key):
    """Split a 15 into a legal XI and a bench, best-first on `key`.

    The same shape as team_service._optimise, on whatever measure the caller
    is simulating - ownership for a template squad (who people actually
    started), actual points for the hindsight ceiling.
    """
    gks = sorted([p for p in squad if p["pos"] == "GK"], key=key, reverse=True)
    outs = {pos: sorted([p for p in squad if p["pos"] == pos], key=key, reverse=True)
            for pos in ("DEF", "MID", "FWD")}
    if len(gks) < 2 or sum(len(v) for v in outs.values()) < 10:
        return None, None

    starters, counts = [gks[0]], {"DEF": 0, "MID": 0, "FWD": 0}
    for pos in ("DEF", "MID", "FWD"):
        starters += outs[pos][:XI_MIN[pos]]
        counts[pos] = XI_MIN[pos]

    rest = sorted((p for pos in outs for p in outs[pos][XI_MIN[pos]:]),
                  key=key, reverse=True)
    for p in rest:
        if len(starters) >= XI_SIZE:
            break
        if counts[p["pos"]] < XI_MAX[p["pos"]]:
            starters.append(p)
            counts[p["pos"]] += 1

    ids = {id(p) for p in starters}
    bench = [p for p in squad if id(p) not in ids]
    return starters, bench


# ---- Chip returns ---------------------------------------------------------

def horizon_value(squad, start_round, table_by_round):
    """Decayed points of a whole squad over the next few gameweeks.

    `table_by_round` is {round: {player_id: points}}, so the same function
    serves both measurements - pass actual points for what happened, model
    predictions for what the planner thought would happen.
    """
    total, weight = 0.0, 1.0
    for gw in range(start_round, start_round + WILDCARD_HORIZON):
        table = table_by_round.get(gw, {})
        total += sum(table.get(p["id"], 0.0) for p in squad) * weight
        weight *= WILDCARD_DECAY
    return total


def held_squads(players, rng):
    """The squads a manager might plausibly have been holding this week."""
    out = [("template", template_squad(players))]
    out += [("plausible", plausible_squad(players, rng))
            for _ in range(PLAUSIBLE_PER_ROUND)]
    return [(label, squad) for label, squad in out
            if squad and len(squad) == SQUAD_SIZE]


# ---- Realised value: what the chips were actually worth --------------------

def simulate_realised(season=SIM_SEASON, verbose=True):
    """Bench Boost and Triple Captain returns, over every gameweek.

    Only these two, because only these two can be measured without a forecast:
    both are questions about a squad the manager already holds. Free Hit and
    Wildcard are about a squad he would have to choose, and choosing it with
    hindsight measures hindsight.
    """
    df = load_rounds(season)
    outlook = fixture_structure.season_outlook(season) or {}
    rng = random.Random(RANDOM_SEED)

    rows = []
    for gw, group in df.groupby("round"):
        gw = int(gw)
        players = round_players(group)
        if len(players) < 200:
            continue
        context = fixture_structure.context(outlook.get(gw))
        for label, squad in held_squads(players, rng):
            # A manager picks his XI on what he expects, not on what happened.
            # Ownership is the honest proxy; using actual points would be
            # scoring the decision with the answer already in hand.
            starters, bench = pick_xi(squad, lambda p: p["owned"])
            if not starters or len(bench) != 4:
                continue
            captain = max(starters, key=lambda p: p["owned"])
            for chip, value in (("bboost", sum(p["points"] for p in bench)),
                                ("3xc", captain["points"])):
                rows.append({"gameweek": gw, "context": context, "chip": chip,
                             "population": label, "value": float(value)})
        if verbose and gw % 10 == 0:
            print(f"  ... realised, gameweek {gw}", flush=True)
    return pd.DataFrame(rows)


# ---- Planned value: what the planner's own arithmetic produces -------------

def simulate_planned(season=SIM_SEASON, verbose=True):
    """Every chip's gain as chip_model computes it, replayed on the holdout.

    This is the measurement the floors are built from. The squad the chip would
    move you to is chosen on model predictions, exactly as the live planner
    chooses it, so the resulting numbers are in the units the planner compares.
    The realised column records what that choice then actually returned.
    """
    preds, cutoff = holdout_predictions(season)
    if not preds:
        return pd.DataFrame(), None

    df = load_rounds(season)
    df["pred"] = [preds.get(int(r), {}).get(int(p))
                  for r, p in zip(df["round"], df["player_id"])]
    outlook = fixture_structure.season_outlook(season) or {}
    pred_by_round = {gw: dict(zip(g["player_id"], g["pred"].fillna(0.0)))
                     for gw, g in df.groupby("round")}
    actual_by_round = {gw: dict(zip(g["player_id"], g["points"]))
                       for gw, g in df.groupby("round")}
    rng = random.Random(RANDOM_SEED)

    rows = []
    for gw, group in df.groupby("round"):
        gw = int(gw)
        if gw <= cutoff:
            continue
        group = group[group["pred"].notna()]
        if len(group) < 200:
            continue
        context = fixture_structure.context(outlook.get(gw))

        # Two views of the same players, and they must not be crossed: what the
        # model expected of them, which is all a selection can be made on, and
        # what they went on to do, which is all a selection can be scored on.
        # `round_players` carries the chosen column into next_gameweeks, which
        # is what optimise_squad selects on; `expected` and `points` are set
        # explicitly here so neither can quietly become the other.
        forecast = round_players(group, "pred")
        expected = dict(zip(group["player_id"], group["pred"]))
        actual = dict(zip(group["player_id"], group["points"]))
        for p in forecast:
            p["expected"] = float(expected.get(p["id"]) or 0.0)
            p["points"] = float(actual.get(p["id"]) or 0.0)

        try:
            rented = optimise_squad(forecast, gw, budget=BUDGET)
            rebuilt = optimise_squad(
                forecast, gw, budget=BUDGET,
                squad_values={p["id"]: horizon_value([p], gw, pred_by_round)
                              for p in forecast})
        except OptimisationError:
            continue
        rented_expected = rented["predicted_points"]
        rented_ids = {s["element_id"] for s in rented["squad"] if s["starting"]}
        rented_actual = sum(p["points"] for p in forecast if p["id"] in rented_ids)
        rebuilt_expected = horizon_value(rebuilt["squad"], gw, pred_by_round)
        rebuilt_actual = horizon_value(rebuilt["squad"], gw, actual_by_round)

        for label, squad in held_squads(forecast, rng):
            # The held squad's own XI, chosen the way the planner chooses it -
            # on the model's numbers, not on ownership, because by this point a
            # manager using this site is picking on the model.
            starters, bench = pick_xi(squad, lambda p: p["expected"])
            if not starters or len(bench) != 4:
                continue
            captain = max(starters, key=lambda p: p["expected"])
            held_expected = (sum(p["expected"] for p in starters)
                             + captain["expected"])
            held_actual = sum(p["points"] for p in starters) + captain["points"]

            planned = {
                "bboost": sum(p["expected"] for p in bench),
                "3xc": captain["expected"],
                "freehit": max(rented_expected - held_expected, 0.0),
                "wildcard": max(rebuilt_expected
                                - horizon_value(squad, gw, pred_by_round), 0.0),
            }
            realised = {
                "bboost": sum(p["points"] for p in bench),
                "3xc": captain["points"],
                "freehit": max(rented_actual - held_actual, 0.0),
                "wildcard": max(rebuilt_actual
                                - horizon_value(squad, gw, actual_by_round), 0.0),
            }
            for chip, value in planned.items():
                rows.append({"gameweek": gw, "context": context, "chip": chip,
                             "population": label, "value": float(value),
                             "realised": float(realised[chip])})
        if verbose:
            print(f"  ... planned, gameweek {gw}", flush=True)
    return pd.DataFrame(rows), cutoff


# ---- Aggregation ----------------------------------------------------------

def _quantiles(values):
    if len(values) == 0:
        return None
    return {name: round(float(np.quantile(values, q)), 2)
            for name, q in QUANTILES.items()}


def context_ratios(realised):
    """How much better a chip does in a double or a blank than an ordinary week.

    Taken from the realised numbers rather than the planned ones because they
    have thirty-eight gameweeks behind them instead of eight. Only Bench Boost
    and Triple Captain are measured this way, so the other two inherit Bench
    Boost's shape - both are worth more when more teams play, for the same
    reason, and one weak assumption stated out loud beats a made-up number.
    """
    ratios = {}
    for chip, group in realised.groupby("chip"):
        base = group.loc[group["context"] == "normal", "value"].median()
        if not base or base <= 0:
            continue
        ratios[str(chip)] = {
            str(ctx): round(float(sub["value"].median() / base), 3)
            for ctx, sub in group.groupby("context")
            if sub["gameweek"].nunique() >= MIN_CONTEXT_GAMEWEEKS
        }
    return ratios


def build_priors(planned, realised, cutoff):
    """Turn the two simulations into the file the planner reads.

    Floors and spreads come from the PLANNED numbers, because those are the
    units the planner compares against. The realised numbers ride along for the
    site to quote and for anyone reading the file to sanity-check it against.
    """
    floors, quantiles, conditional = {}, {}, {}
    ratios = context_ratios(realised)

    for chip, group in planned.groupby("chip"):
        chip = str(chip)
        values = group["value"].to_numpy()
        quantiles[chip] = _quantiles(values)
        floors[chip] = quantiles[chip]["p50"]

        # Ordinary weeks are the only context with enough planned samples to
        # measure directly; the others are that shape scaled by how much better
        # they were in reality. Scaling the whole distribution rather than
        # shifting it keeps the spread proportional, which is what the planner's
        # hold-quantile reads off.
        normal = _quantiles(group.loc[group["context"] == "normal", "value"].to_numpy()) \
            or quantiles[chip]
        by_context = {"normal": normal}
        chip_ratios = ratios.get(chip) or ratios.get("bboost") or {}
        for ctx in ("double", "blank"):
            factor = chip_ratios.get(ctx)
            if factor:
                by_context[ctx] = {k: round(v * factor, 2) for k, v in normal.items()}
        conditional[chip] = by_context

    realised_summary = {}
    for chip, group in realised.groupby("chip"):
        realised_summary[str(chip)] = _quantiles(group["value"].to_numpy())
    for chip, group in planned.groupby("chip"):
        realised_summary.setdefault(str(chip),
                                    _quantiles(group["realised"].to_numpy()))

    return {
        "provenance": "simulated",
        "season": SIM_SEASON,
        "planned_from_rounds": f">{int(cutoff)}" if cutoff else None,
        "samples": {"planned": int(len(planned)), "realised": int(len(realised))},
        "floor": floors,
        "quantiles": quantiles,
        "conditional": conditional,
        "context_ratios": ratios,
        # In real points, for the site to quote. NOT what the floors are in.
        "realised": realised_summary,
    }


# ---- Predicted-vs-actual scale --------------------------------------------

def holdout_predictions(season=SIM_SEASON):
    """Model predictions for the rounds train_model.py holds out.

    Out of sample by construction: these are the last 8 rounds, which the saved
    bundles were never fitted on. Returns {round: {player_id: predicted}}, or
    None if the bundles aren't on disk.
    """
    import joblib

    df = train_model.load_all_seasons([season])
    df = train_model.build_rolling_features(df)
    df = train_model.add_fixture_context(df)
    df = train_model.attach_position(df)
    _train, test, _latest, cutoff = train_model.split_train_test(df)

    out = {}
    for position, group in test.groupby("position"):
        path = os.path.join(seasons.MODELS_DIR,
                            f"{str(position).lower()}_model.pkl")
        if not os.path.exists(path):
            return None, None
        bundle = joblib.load(path)
        expected, _if_starts, _p_start = train_model.predict_bundle(bundle, group)
        for pid, rnd, value in zip(group["player_id"], group["round"], expected):
            out.setdefault(int(rnd), {})[int(pid)] = float(value)
    return out, int(cutoff)


# ---- Report ---------------------------------------------------------------

def _row(label, values, width=10):
    if len(values) == 0:
        return f"{label:<10}{'-':>7}"
    return (f"{label:<10}{len(values):>7}{np.quantile(values, .25):>8.1f}"
            f"{np.median(values):>9.1f}{np.quantile(values, .75):>8.1f}"
            f"{np.quantile(values, .90):>8.1f}")


def report(planned, realised, priors, season):
    print(f"\nWhat the chips were really worth, {season} (actual points, "
          f"all {realised['gameweek'].nunique()} gameweeks)")
    print(f"{'chip':<10}{'n':>7}{'p25':>8}{'median':>9}{'p75':>8}{'p90':>8}")
    for chip in ("bboost", "3xc"):
        print(_row(chip, realised.loc[realised["chip"] == chip, "value"].to_numpy()))
    for chip in ("freehit", "wildcard"):
        values = planned.loc[planned["chip"] == chip, "realised"].to_numpy()
        print(_row(chip + "*", values))
    print("  * holdout rounds only - both need a forecast to choose the "
          "alternative squad")

    print("\nWhat the planner computes (predicted points - the units the "
          "floors are in)")
    print(f"{'chip':<10}{'n':>7}{'p25':>8}{'median':>9}{'p75':>8}{'p90':>8}")
    for chip in ("bboost", "3xc", "freehit", "wildcard"):
        print(_row(chip, planned.loc[planned["chip"] == chip, "value"].to_numpy()))

    print("\nDoes a double gameweek matter? (realised median, by week type)")
    contexts = [c for c in ("normal", "double", "blank")
                if c in set(realised["context"])]
    print(f"{'chip':<10}" + "".join(f"{c:>10}" for c in contexts)
          + f"{'double/normal':>15}")
    for chip in ("bboost", "3xc"):
        sub = realised[realised["chip"] == chip]
        medians = {c: sub.loc[sub["context"] == c, "value"].median() for c in contexts}
        base = medians.get("normal")
        ratio = (medians["double"] / base) if base and "double" in medians else float("nan")
        print(f"{chip:<10}" + "".join(f"{medians[c]:>10.1f}" for c in contexts)
              + f"{ratio:>15.2f}")

    weeks = realised.drop_duplicates(["gameweek", "context"])
    print("\nGameweeks of each kind, whole season:",
          dict(weeks.groupby("context").size()))
    print("Gameweeks of each kind, GW1-19    :",
          dict(weeks[weeks["gameweek"] <= 19].groupby("context").size())
          or "{all ordinary}")
    print("  -> the first half is where the chips have to be spent, and in "
          "2025-26 it contained no doubles and no blanks at all.")

    print("\nFloors written (predicted points a chip must clear while there is "
          "still time to wait):")
    for chip, value in sorted(priors["floor"].items()):
        print(f"  {chip:<10}{value:>6.1f}")


def main(argv=None):
    argv = argv or sys.argv[1:]
    season = argv[0] if argv else SIM_SEASON

    print(f"Simulating chip returns for {season} ...")
    realised = simulate_realised(season)
    if realised.empty:
        raise RuntimeError("simulation produced no realised samples")

    print("Replaying the planner's own arithmetic on the model holdout ...")
    planned, cutoff = simulate_planned(season)
    if planned.empty:
        raise RuntimeError(
            "no planned samples - the trained model bundles are needed for "
            "this step. Run train_model.py first.")

    priors = build_priors(planned, realised, cutoff)
    report(planned, realised, priors, season)

    os.makedirs(seasons.REFERENCE_DIR, exist_ok=True)
    path = os.path.join(seasons.REFERENCE_DIR, "chip_priors.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(priors, fh, indent=2, sort_keys=True)
    print(f"\nWrote {path}")
    return priors


if __name__ == "__main__":
    main()
