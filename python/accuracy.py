"""Predicted against actual, for /accuracy.

Arithmetic over `ai_team_snapshot_picks` and `manager_team_picks`, both of
which already hold a frozen prediction beside a backfilled actual. No model is
loaded and nothing is recomputed - the freeze is what makes the comparison
honest, so re-deriving anything here would defeat it.

Three things a reader of the output needs to know, all stated on the page:

  * The sample is squads somebody PICKED, mostly the model's own. Easier to
    score well on than the whole pool, most of which never plays and is
    trivially projected at nearly zero.
  * Stored predictions are risk-adjusted (P(available) x points), so they sit
    below the raw projections on a player's page.
  * Every figure is reported against predicting the mean for everybody. An MAE
    on its own says nothing about whether the model earns its keep.
"""

import math

from db import AI_MANAGER_FPL_ID, connect

# Roughly four gameweeks of one squad. Driven by the rank correlation, which
# swings wildly on a couple of dozen pairs; MAE and bias are steadier and are
# shown either way, always beside `n`.
MIN_PREDICTIONS = 60

# What each sample is OF. Data rather than three near-identical functions:
# only the WHERE clause differs.
SOURCES = {
    "best_xi": {
        "label": "AI Best XI",
        "note": "the squad the optimiser picks fresh each week, so the players "
                "the model rates highest",
    },
    "manager": {
        "label": "AI Manager",
        "note": "one squad carried across the season, so it holds players "
                "bought several weeks earlier",
    },
    "real": {
        "label": "Real managers",
        "note": "squads captured at the deadline from people using the site - "
                "picked by humans, so the least flattering sample here",
    },
}


def _pairs(source):
    """(predicted, actual, gameweek) for every settled pick from `source`.

    A NULL `actual_points` means FPL never reported him, not that he scored
    nothing - see ai_team.backfill_actuals - so those rows are excluded rather
    than counted as zero.
    """
    if source == "best_xi":
        sql = """SELECT p.predicted_points AS predicted, p.actual_points AS actual,
                        s.gameweek AS gameweek
                 FROM ai_team_snapshot_picks p
                 JOIN ai_team_snapshot s ON s.id = p.snapshot_id
                 WHERE p.predicted_points IS NOT NULL
                   AND p.actual_points IS NOT NULL"""
        params = ()
    else:
        # The AI Manager is a manager with a reserved id; that is the only
        # thing separating the two halves of this table.
        comparison = "=" if source == "manager" else "!="
        sql = f"""SELECT p.predicted_points AS predicted, p.actual_points AS actual,
                         t.gameweek AS gameweek
                  FROM manager_team_picks p
                  JOIN manager_team t ON t.id = p.manager_team_id
                  WHERE t.fpl_id {comparison} ?
                    AND p.predicted_points IS NOT NULL
                    AND p.actual_points IS NOT NULL"""
        params = (AI_MANAGER_FPL_ID,)

    with connect() as conn:
        return [(float(r["predicted"]), float(r["actual"]), int(r["gameweek"]))
                for r in conn.execute(sql, params)]


def _ranks(values):
    """Ranks with ties averaged, as Spearman requires.

    Ties dominate here - most actual scores are 0, 1 or 2 - so an ordinal rank
    would invent an order among equal values and report a correlation partly
    built out of it.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _pearson(xs, ys):
    """Correlation, or None when either side has no spread.

    None rather than zero: a week where everyone scored the same is not one the
    model got wrong, it is one with nothing to be right about.
    """
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _metrics(pairs):
    """The summary statistics for a list of (predicted, actual, gameweek)."""
    n = len(pairs)
    if not n:
        return {"n": 0, "sufficient": False}

    predicted = [p for p, _, _ in pairs]
    actual = [a for _, a, _ in pairs]
    errors = [p - a for p, a, _ in pairs]

    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    bias = sum(errors) / n

    # The thing to beat: the best you can do knowing nothing about the players.
    mean_actual = sum(actual) / n
    baseline_mae = sum(abs(mean_actual - a) for a in actual) / n
    improvement = (100.0 * (baseline_mae - mae) / baseline_mae
                   if baseline_mae > 0 else None)

    # The headline figure: the site's claim is "useful for comparing players,
    # poor as a forecast of any single score", which is a statement about rank.
    spearman = _pearson(_ranks(predicted), _ranks(actual))

    return {
        "n": n,
        "sufficient": n >= MIN_PREDICTIONS,
        "gameweeks": sorted({gw for _, _, gw in pairs}),
        "mae": round(mae, 2),
        "rmse": round(rmse, 2),
        "bias": round(bias, 2),
        "baseline_mae": round(baseline_mae, 2),
        "improvement_pct": round(improvement, 1) if improvement is not None else None,
        "spearman": round(spearman, 3) if spearman is not None else None,
        "mean_predicted": round(sum(predicted) / n, 2),
        "mean_actual": round(mean_actual, 2),
    }


def by_source():
    """Metrics per sample. Fetched independently so one unreadable table
    cannot cost the page the others."""
    out = []
    for key, meta in SOURCES.items():
        try:
            pairs = _pairs(key)
        except Exception as e:                       # pragma: no cover - defensive
            print(f"couldn't read accuracy pairs for {key}: {e}")
            continue
        if not pairs:
            continue
        out.append({"key": key, **meta, **_metrics(pairs)})
    return out


def by_gameweek(source="best_xi"):
    """Per-gameweek accuracy, oldest first. The spread is more use than the
    season average when deciding how much to trust a projection."""
    try:
        pairs = _pairs(source)
    except Exception as e:                           # pragma: no cover - defensive
        print(f"couldn't read per-gameweek accuracy for {source}: {e}")
        return []
    grouped = {}
    for p, a, gw in pairs:
        grouped.setdefault(gw, []).append((p, a, gw))
    return [{"gameweek": gw, **_metrics(rows)}
            for gw, rows in sorted(grouped.items())]


def squad_totals():
    """What each frozen squad was projected to score, and what it scored.

    A different question from the player-level figures: a squad only needs the
    ORDER roughly right, so it can score well in a week the projections were
    poor.
    """
    rows = []
    try:
        with connect() as conn:
            for r in conn.execute(
                    """SELECT gameweek, predicted_points, actual_points
                       FROM ai_team_snapshot
                       WHERE actual_points IS NOT NULL
                       ORDER BY gameweek"""):
                rows.append({"gameweek": int(r["gameweek"]),
                             "predicted": round(float(r["predicted_points"]), 1),
                             "actual": int(r["actual_points"]),
                             "source": "best_xi"})
    except Exception as e:                           # pragma: no cover - defensive
        print(f"couldn't read squad totals: {e}")
    return rows


def summary():
    """Everything the page renders. `available` is False until something has
    settled - a page of zeroes would be worse than a paragraph saying so."""
    sources = by_source()
    totals = squad_totals()
    overall = next((s for s in sources if s["key"] == "best_xi"), None)
    return {
        "available": bool(sources),
        "sources": sources,
        "per_gameweek": by_gameweek("best_xi"),
        "squad_totals": totals,
        "headline": overall,
        "min_predictions": MIN_PREDICTIONS,
        "settled_gameweeks": sorted({r["gameweek"] for r in totals}),
    }
