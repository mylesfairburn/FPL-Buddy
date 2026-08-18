"""When to play a chip, treated as a scheduling problem rather than a threshold.

Asking one question per chip - "is this week good enough?" - against a
hard-coded number has no right answer, because the value of playing a chip this
week is not a property of this week. It is the difference between this week and
the best week left. A rule that never looks at the weeks left fails in both
directions: a bench projecting 18 points fires the Bench Boost in GW3 whether or
not GW9 is a double, and a captain threshold set in actual-points intuition
(9.0) never fires against a model that shrinks its top midfielders to 5-6.5.

What actually constrains the problem:

  * Four chips, at most one a gameweek, all returned after GW19. A chip not
    played by then is not saved, it is wasted.
  * So the real question is an assignment - which gameweek does each chip go in?
    That is an integer program, and PuLP is already here solving the same shape
    for squad selection.

Framing it that way makes the deadline behaviour fall out rather than needing a
countdown special case. Early in a half there are far more gameweeks than chips,
so the solver has slack and parks each chip on the best week it can see - not
usually this one, so the bot holds. As the weeks run down the slack disappears,
and when there are as many gameweeks left as chips the assignment is forced.
Nothing has to notice that GW18 is approaching.

The other half of the problem: a gameweek eight weeks out has no squad to
evaluate, since transfers will change it. So a far gameweek is scored by what a
chip is *typically* worth in a week of its kind (double, blank, ordinary),
measured by simulating last season in train_chip_model.py and read from
data/reference/chip_priors.json.

One subtlety in how those priors are used. The value of holding a chip for
eleven more weeks is not the value of a typical week - it is the value of the
best of eleven, because you get to choose. Scoring far weeks at the median would
undervalue waiting and make the bot spend early, so they are scored at an
order-statistic quantile, q = 1 - 1/(n+1) for n remaining opportunities, which
is the expected maximum of n draws. As n falls that quantile falls with it, near
weeks start winning on merit, and the bot becomes decisive on its own.

Everything here is a pure function of the rated pool and the fixture list. No
database, no persistence - the AI Manager owns those, and the user-facing chip
advice on My Team calls these same functions against the user's own squad.
"""

import json
import os

import pulp

import fixture_structure
import seasons
from squad_optimiser import (DEFAULT_BUDGET, OptimisationError, availability,
                             optimise_squad)
from valuation import (coverage_requirement, gameweek_points, has_fixture,
                       horizon_value, squad_horizon_value,
                       squad_selection_values)

# FPL's own chip codes, as the names a reader recognises. The codes are what the
# API uses and what the planner works in; "Chip played: bboost" is a line only
# somebody who has read this code can parse. This is the one definition -
# team_service and social both import it rather than keeping their own.
CHIP_NAMES = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
}
CHIP_CODES = tuple(CHIP_NAMES)

# How fast a computed gain stops being believable as it moves away from the
# gameweek being planned. At lead 1 a gain is trusted about two thirds; by lead
# 4, a third. The squad that week is a guess - transfers happen in between.
LEAD_DECAY = 0.5

# Ceiling on the order-statistic quantile used to value holding. Without it, a
# half with fifteen unseen weeks left values waiting at the very top of the
# distribution and the bot never plays anything until it is forced.
MAX_HOLD_QUANTILE = 0.90

# How much of a rebuild's long-term value counts against a Free Hit. Not 1.0:
# some overlap between "best this week" and "best for two months" is normal and
# shouldn't disqualify a genuine one-week spike.
FREE_HIT_PERMANENCE = 0.5

# Roughly a point a gameweek across the four-week horizon, before decay. Below
# that a player is not contributing, whatever the reason.
DEADWEIGHT_HORIZON_POINTS = 4.0

# Breaks ties between gameweeks nothing is known about yet toward the LATER one.
# Beyond the rated pool's horizon every ordinary week scores identically, so
# without this the solver picks among them arbitrarily and the tab announces
# that the Bench Boost is being saved for GW12 as though that were a decision.
# Later is the right way to break it: the expected value is the same either way,
# and waiting means the fixture list, the injuries and the form are all known
# before committing. Kept at 1e-4 - across a whole season it is worth under half
# a point, so a genuine difference of any size still wins.
LATE_TIEBREAK = 1e-4

# Future gameweeks a chip's gain must be computable in before the planner trusts
# the squad's own spread over the simulated priors. Below this the sample is too
# small to be a distribution; Free Hit and Wildcard never reach it by design,
# because both need a solve against a squad that only exists today.
MIN_SELF_CALIBRATION = 4

# Below this availability a player counts as carrying a real injury rather than
# a knock, for the purpose of explaining a wildcard.
INJURED_BELOW = 0.75

# Used when data/reference/chip_priors.json is missing - a fresh checkout, or
# before train_chip_model.py has been run. Deliberately conservative: these are
# roughly what the 2025-26 simulation produces, so the planner behaves sanely
# rather than refusing to plan, but the real file should always win.
DEFAULT_PRIORS = {
    "provenance": "fallback",
    "floor": {"bboost": 12.0, "3xc": 6.0, "freehit": 14.0, "wildcard": 16.0},
    "quantiles": {
        "bboost":   {"p25": 7.0, "p50": 12.0, "p75": 17.0, "p90": 23.0},
        "3xc":      {"p25": 2.0, "p50": 6.0,  "p75": 10.0, "p90": 15.0},
        "freehit":  {"p25": 8.0, "p50": 14.0, "p75": 20.0, "p90": 28.0},
        "wildcard": {"p25": 9.0, "p50": 16.0, "p75": 24.0, "p90": 34.0},
    },
    # Split by what kind of week it is, and kept as a distribution rather than a
    # single number. Both halves of that matter. A double gameweek is a
    # different animal from an ordinary one, so a single pooled figure flatters
    # ordinary weeks and undersells doubles - and the FIRST half of 2025-26 had
    # no doubles or blanks at all, so a planner working to GW19 that valued
    # waiting at the pooled p90 would be holding out for a week that does not
    # exist. Keeping the spread per context is what lets "the best of eleven
    # ordinary weeks" be a different number from "the best of eleven weeks".
    "conditional": {
        "bboost": {
            "normal": {"p25": 6.0, "p50": 10.0, "p75": 14.0, "p90": 18.0},
            "double": {"p25": 13.0, "p50": 19.0, "p75": 25.0, "p90": 32.0},
            "blank":  {"p25": 3.0, "p50": 7.0, "p75": 11.0, "p90": 15.0},
        },
        "3xc": {
            "normal": {"p25": 2.0, "p50": 5.0, "p75": 9.0, "p90": 13.0},
            "double": {"p25": 6.0, "p50": 11.0, "p75": 16.0, "p90": 22.0},
            "blank":  {"p25": 2.0, "p50": 5.0, "p75": 8.0, "p90": 12.0},
        },
        "freehit": {
            "normal": {"p25": 6.0, "p50": 11.0, "p75": 16.0, "p90": 21.0},
            "double": {"p25": 11.0, "p50": 18.0, "p75": 24.0, "p90": 31.0},
            "blank":  {"p25": 16.0, "p50": 25.0, "p75": 33.0, "p90": 42.0},
        },
        "wildcard": {
            "normal": {"p25": 9.0, "p50": 16.0, "p75": 23.0, "p90": 31.0},
            "double": {"p25": 12.0, "p50": 20.0, "p75": 27.0, "p90": 35.0},
            "blank":  {"p25": 10.0, "p50": 18.0, "p75": 25.0, "p90": 33.0},
        },
    },
}

_PRIORS_CACHE = {}


def chip_name(chip):
    """A chip's display name, falling back to whatever FPL called it. A new chip
    - they have added them before - reads as its code rather than disappearing."""
    return CHIP_NAMES.get(chip, chip)


def priors_path():
    return os.path.join(seasons.REFERENCE_DIR, "chip_priors.json")


def load_priors(refresh=False):
    """The simulated chip-value distributions, or the built-in fallback.

    Cached: this is read on every chip decision and on every My Team request,
    and it changes only when train_chip_model.py is re-run.
    """
    if not refresh and "priors" in _PRIORS_CACHE:
        return _PRIORS_CACHE["priors"]
    priors = DEFAULT_PRIORS
    path = priors_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if loaded.get("floor") and loaded.get("quantiles"):
                priors = loaded
    except (OSError, ValueError):
        pass                      # a corrupt priors file must not break a deadline
    _PRIORS_CACHE["priors"] = priors
    return priors


def floor(chip, priors=None):
    """The least a chip is worth burning on, when there is time to wait."""
    priors = priors or load_priors()
    return float((priors.get("floor") or {}).get(chip, 0.0))


def _interpolate(qs, quantile):
    """Linear interpolation across stored {"p25": ..., "p50": ...} points."""
    if not qs:
        return None
    points = sorted((float(str(k).lstrip("p")) / 100.0, float(v)) for k, v in qs.items())
    if quantile <= points[0][0]:
        return points[0][1]
    if quantile >= points[-1][0]:
        return points[-1][1]
    for (q0, v0), (q1, v1) in zip(points, points[1:]):
        if q0 <= quantile <= q1:
            span = (q1 - q0) or 1.0
            return v0 + (v1 - v0) * (quantile - q0) / span
    return points[-1][1]


def prior_quantile(chip, quantile, priors=None):
    """Where a value sits in a chip's overall return distribution."""
    priors = priors or load_priors()
    return _interpolate((priors.get("quantiles") or {}).get(chip), quantile) or 0.0


def context_quantile(chip, context, quantile, priors=None):
    """A quantile of what this chip returns in a week of this KIND.

    Falls back to the pooled distribution when a context has no numbers of its
    own - a season with no blank gameweeks produces no blank-week samples, and a
    missing bucket should read as "no better information" rather than as zero.
    """
    priors = priors or load_priors()
    by_context = (priors.get("conditional") or {}).get(chip) or {}
    qs = by_context.get(context)
    if isinstance(qs, (int, float)):
        # An older priors file storing one number per context. Treat it as the
        # median and borrow the pooled spread around it.
        median = float(qs)
        pooled = _interpolate((priors.get("quantiles") or {}).get(chip), 0.5) or 0.0
        shift = median - pooled
        return (prior_quantile(chip, quantile, priors) + shift)
    value = _interpolate(qs, quantile)
    return value if value is not None else prior_quantile(chip, quantile, priors)



def percentile_of(chip, value, priors=None):
    """Where a computed gain sits in the simulated distribution, 0-100.

    Reported rather than the raw number because "your bench projects 14" means
    nothing on its own, and "better than 80% of the weeks we simulated" means
    something to anyone.
    """
    priors = priors or load_priors()
    qs = (priors.get("quantiles") or {}).get(chip)
    if not qs:
        return None
    points = sorted((float(str(k).lstrip("p")) / 100.0, float(v))
                    for k, v in qs.items())
    if value <= points[0][1]:
        return round(points[0][0] * 100)
    if value >= points[-1][1]:
        return round(points[-1][0] * 100)
    for (q0, v0), (q1, v1) in zip(points, points[1:]):
        if v0 <= value <= v1:
            span = (v1 - v0) or 1.0
            return round((q0 + (q1 - q0) * (value - v0) / span) * 100)
    return None


def realised_median(chip, priors=None):
    """What this chip really returned last season, in actual points.

    Kept apart from `quantiles`, which are in the model's predicted points. The
    two are not the same unit and must never be compared to each other - this
    one exists to be quoted to a reader, not to be thresholded against.
    """
    priors = priors or load_priors()
    got = (priors.get("realised") or {}).get(chip) or {}
    return got.get("p50")


def hold_quantile(opportunities):
    """The quantile that represents "the best of `opportunities` weeks".

    E[max of n draws] sits near the 1 - 1/(n+1) quantile. This is the whole
    reason the bot is patient in September and decisive in December: with twelve
    weeks left, holding is worth the top of the distribution; with two, it is
    worth about the upper quartile; with one, the median.
    """
    n = max(int(opportunities), 1)
    return min(1.0 - 1.0 / (n + 1), MAX_HOLD_QUANTILE)


def confidence(lead):
    """How much a computed gain `lead` gameweeks out is worth believing."""
    return 1.0 / (1.0 + LEAD_DECAY * max(int(lead), 0))


def quantile_of(values, quantile):
    """Plain linear-interpolated quantile of a list. No numpy for four numbers."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = quantile * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


# ---- Per-chip gain --------------------------------------------------------

def _points_in(player, gameweek):
    """A player's points in one gameweek, or his blended figure if the pool has
    no per-gameweek breakdown for him at all.

    The distinction matters: a player whose club blanks scores a real 0 that
    week, and falling back to his season-average projection there would value a
    Bench Boost on a blank week as if everyone played.
    """
    if player.get("next_gameweeks"):
        return gameweek_points(player, gameweek)
    return float(player.get("predicted") or 0.0)


def bench_boost_gain(lineup, gameweek):
    """Extra points from the four players who would otherwise not score."""
    bench = [p for p in (lineup or {}).get("squad", []) if not p.get("starting")]
    return sum(_points_in(p, gameweek) for p in bench)


def triple_captain_gain(lineup, gameweek):
    """The extra 1x on the armband - a captain already scores double."""
    captain = next((p for p in (lineup or {}).get("squad", [])
                    if p.get("is_captain")), None)
    return _points_in(captain, gameweek) if captain else 0.0


def wildcard_gain(squad, pool, gameweek, bank=0.0, cache=None):
    """What rebuilding the squad is worth over the long view, and why.

    Measured over SQUAD_HORIZON with SQUAD_DECAY rather than on this gameweek.
    The old rule compared a one-week rebuild against the current XI, which is a
    Free Hit's question - it fires on a bad week rather than on a squad that has
    genuinely drifted.
    """
    if cache is not None and ("wildcard", gameweek) in cache:
        return cache[("wildcard", gameweek)]
    try:
        rebuilt = optimise_squad(
            pool, gameweek, budget=DEFAULT_BUDGET + (bank or 0.0),
            squad_values=squad_selection_values(pool, gameweek),
            coverage=coverage_requirement(gameweek))
        gain = (squad_horizon_value(rebuilt["squad"], gameweek)
                - squad_horizon_value(squad, gameweek))
        result = (gain, wildcard_factors(squad, gameweek))
    except OptimisationError:
        result = (0.0, {})
    if cache is not None:
        cache[("wildcard", gameweek)] = result
    return result


def wildcard_factors(squad, gameweek):
    """The three things that make a squad worth tearing up, as numbers.

    These do not decide anything - the horizon gain above does. They exist so
    the bot can say why, because "rebuild projects 19 more points" is a verdict
    and "three injuries and four players on a wall of fixtures" is a reason.
    """
    return {
        "injury_load": round(sum(1.0 - availability(p) for p in squad), 2),
        "injured": sum(1 for p in squad if availability(p) < INJURED_BELOW),
        "blanking": sum(1 for p in squad if not has_fixture(p, gameweek)),
        # A player worth owning contributes over the horizon; one who doesn't is
        # out of form, out of the team, or facing nobody he can score against.
        "deadweight": sum(1 for p in squad
                          if horizon_value(p, gameweek) < DEADWEIGHT_HORIZON_POINTS),
    }


def free_hit_gain(squad, pool, gameweek, bank=0.0, lineup=None, cache=None):
    """What a one-week rental buys, net of what a permanent rebuild would.

    A Free Hit is for a spike the squad cannot cover and does not need to cover
    again - a blank week, or a double it happens to miss. If a rebuilt squad
    would be better for the next two months as well, that is a Wildcard, and
    burning a Free Hit on it means paying for one week of a fix you needed
    permanently. Subtracting part of the horizon gain is what separates the two.
    """
    try:
        rented = optimise_squad(pool, gameweek,
                                budget=DEFAULT_BUDGET + (bank or 0.0))
    except OptimisationError:
        return 0.0, {}

    held = (lineup or {}).get("predicted_points") or 0.0
    one_week = rented["predicted_points"] - held
    permanent = wildcard_gain(squad, pool, gameweek, bank, cache)[0]

    gain = one_week - max(permanent, 0.0) * FREE_HIT_PERMANENCE
    return max(gain, 0.0), {
        "one_week_gain": round(one_week, 1),
        "permanent_gain": round(permanent, 1),
    }


def chip_gain(chip, gameweek, squad, pool, bank=0.0, lineup=None, cache=None):
    """Expected extra points from playing `chip` in `gameweek`.

    Returns (gain, factors). Bench Boost and Triple Captain are cheap and exact
    for any gameweek the rated pool covers. Free Hit and Wildcard each need a
    solve, so they are only computed for the gameweek being planned - a rebuild
    six weeks out would be against a squad that will not exist by then anyway.
    """
    if chip == "bboost":
        return bench_boost_gain(lineup, gameweek), {}
    if chip == "3xc":
        return triple_captain_gain(lineup, gameweek), {}
    if chip == "freehit":
        return free_hit_gain(squad, pool, gameweek, bank, lineup, cache)
    if chip == "wildcard":
        return wildcard_gain(squad, pool, gameweek, bank, cache)
    return 0.0, {}


def computable(chip, gameweek, now, squad):
    """Whether `chip`'s gain in `gameweek` can be worked out rather than assumed."""
    if chip in ("freehit", "wildcard"):
        # Both need a solve against a squad, and the only squad that is known is
        # today's. Six weeks of transfers make any other one fiction.
        return gameweek == now
    # Bench Boost and Triple Captain need per-gameweek predictions, which run
    # out at the end of the rated pool's horizon.
    return any(has_fixture(p, gameweek) for p in (squad or []))


def gain_matrix(available, now, gameweeks, squad, pool, outlook, bank=0.0,
                lineup=None, priors=None):
    """{chip: {gameweek: (effective, computed_or_None, factors)}}.

    Computed gains are blended toward what a week of that kind is typically
    worth, by how far out they are; gameweeks that cannot be computed take the
    prior outright. See the module docstring for why the prior a *future* week
    is valued at is an upper quantile rather than the median - that quantile is
    the value of still having the choice.
    """
    priors = priors or load_priors()
    gameweeks = sorted(gameweeks)
    cache = {}

    # How many future weeks of each kind are left. A chip held through eleven
    # ordinary weeks is worth the best of eleven ORDINARY weeks - not the best
    # of eleven weeks generally, which is a much bigger number that only exists
    # if some of them are doubles. Getting this wrong is what makes a planner
    # hold out all season for a week the fixture list never contained.
    remaining = {}
    for gw in gameweeks:
        if gw != now:
            ctx = fixture_structure.context(outlook.get(gw))
            remaining[ctx] = remaining.get(ctx, 0) + 1

    matrix = {}
    for chip in available:
        # First pass: what this chip is actually worth, week by week, wherever
        # that can be worked out at all.
        computed = {}
        for gw in gameweeks:
            if computable(chip, gw, now, squad):
                computed[gw] = chip_gain(chip, gw, squad, pool, bank, lineup, cache)

        # What holding this chip is worth, measured on THIS squad wherever
        # there is enough of it to measure. The priors describe a strong
        # manager's squad; this one is built by an optimiser told that bench
        # points do not count, so its bench is cheap by construction. Valuing
        # the option to wait at someone else's distribution makes every future
        # week look better than every present one, and the bot holds every chip
        # until the deadline forces it out. Its own spread cannot lie to it.
        future = [g for gw, (g, _f) in computed.items() if gw != now]
        self_reference = (quantile_of(future, hold_quantile(len(future)))
                          if len(future) >= MIN_SELF_CALIBRATION else None)

        row = {}
        for gw in gameweeks:
            ctx = fixture_structure.context(outlook.get(gw))
            if gw == now:
                reference = context_quantile(chip, ctx, 0.5, priors)
            elif self_reference is not None:
                reference = self_reference
            else:
                reference = context_quantile(
                    chip, ctx, hold_quantile(remaining.get(ctx, 1)), priors)

            if gw in computed:
                gain, factors = computed[gw]
                conf = confidence(gw - now)
                effective = conf * gain + (1.0 - conf) * reference
            else:
                gain, factors = None, {}
                effective = reference + gw * LATE_TIEBREAK
            row[gw] = (effective, gain, factors)
        matrix[chip] = row
    return matrix


# ---- The schedule ---------------------------------------------------------

def schedule_chips(available, now, deadline, matrix, priors=None):
    """Assign each remaining chip to a gameweek, at most one chip per gameweek.

    Returns {gameweek: chip}. The chip to play now is whatever lands on `now`.

    Two rules beyond "maximise expected points":

      * A chip is not scheduled below its floor while there is slack. The floor
        is the median return the simulation says a chip is worth; spending one
        under that in September, when eleven weeks are still available, is how a
        season's chips get wasted.

      * Once there are no more gameweeks left than chips, every chip is forced
        out and the floor is dropped. A chip played in a poor week scores
        something; a chip never played scores nothing.
    """
    priors = priors or load_priors()
    chips = [c for c in available if c in matrix]
    gameweeks = sorted({gw for row in matrix.values() for gw in row
                        if now <= gw <= deadline})
    if not chips or not gameweeks:
        return {}

    slack = len(gameweeks) - len(chips)
    prob = pulp.LpProblem("chip_schedule", pulp.LpMaximize)
    x = {(c, gw): pulp.LpVariable(f"x_{c}_{gw}", cat="Binary")
         for c in chips for gw in gameweeks}

    prob += pulp.lpSum(matrix[c][gw][0] * x[(c, gw)]
                       for c in chips for gw in gameweeks)

    for c in chips:
        prob += pulp.lpSum(x[(c, gw)] for gw in gameweeks) <= 1
        # Forced once the weeks have run down to the number of chips left. Only
        # when there is actually room for all of them - with five chips and
        # three gameweeks this would be infeasible, and two chips lost is still
        # better than a solver error at a deadline.
        if slack <= 0 and len(gameweeks) >= len(chips):
            prob += pulp.lpSum(x[(c, gw)] for gw in gameweeks) >= 1

    for gw in gameweeks:
        # The rule the whole feature has to respect: never two chips in a week.
        prob += pulp.lpSum(x[(c, gw)] for c in chips) <= 1

    if slack > 0:
        for c in chips:
            for gw in gameweeks:
                if matrix[c][gw][0] < floor(c, priors):
                    prob += x[(c, gw)] == 0

    try:
        status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
        if pulp.LpStatus[status] != "Optimal":
            raise OptimisationError(pulp.LpStatus[status])
    except (OptimisationError, pulp.PulpSolverError):
        return _greedy_schedule(chips, gameweeks, matrix, priors, slack)

    return {gw: c for c in chips for gw in gameweeks
            if x[(c, gw)].value() and round(x[(c, gw)].value()) == 1}


def _greedy_schedule(chips, gameweeks, matrix, priors, slack):
    """Fallback if the solver is unavailable: best chip-week pairs, first come.

    A deadline job must not fail because CBC did. This gives up optimality, not
    legality - the one-chip-per-gameweek rule is still enforced.
    """
    pairs = sorted(((matrix[c][gw][0], c, gw) for c in chips for gw in gameweeks),
                   reverse=True, key=lambda t: t[0])
    taken_gws, taken_chips, out = set(), set(), {}
    for gain, c, gw in pairs:
        if c in taken_chips or gw in taken_gws:
            continue
        if slack > 0 and gain < floor(c, priors):
            continue
        out[gw] = c
        taken_chips.add(c)
        taken_gws.add(gw)
    return out
