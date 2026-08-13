"""Server-rendered versions of the tables the four tool pages draw in JavaScript.

Why this exists
---------------
`/players`, `/ai-teams` and `/fixture-rotator` build every table in the browser
from `/api/*`. That is the right architecture for a person - the tables sort,
filter and refresh without a page load - and it is invisible to everything else.
Crawlers largely do not run JavaScript, and `robots.txt` disallows `/api/`
anyway, so the endpoints holding the data are the ones nothing is allowed to
fetch. The result was that the best content on the site - several hundred rated
players, two solved squads a week, twenty clubs' fixture difficulty - existed
only as an empty <tbody> as far as a search engine or a language model was
concerned.

So the same rows are also rendered on the server, into the very containers the
scripts later fill. Not a second copy of the page behind a `noscript`, and not a
crawler-only variant: one set of markup that a browser upgrades in place. That
matters beyond tidiness - serving a crawler different content from a reader is
cloaking, and the way to be obviously not doing it is to have only one version.

What it is not
--------------
This is a snapshot, not the tool. It carries the top of each table with no
controls: no sorting, no filtering, no gameweek arrows. Anyone with JavaScript
gets the real thing a moment later, and anyone without gets something worth
reading rather than an empty box.

Cost
----
Everything here is derived once per data load and cached by the caller (see
`seo_tables()` in main.py), the same way the per-player pages are. Nothing is
computed inside a request: these pages are the ones crawlers hit hardest, and
re-deriving several hundred rows per hit is how a crawl turns into an outage.
"""

# How much of each table to serve. Enough to be substantial and to cover every
# player anyone is realistically searching for; not so much that the document
# becomes hundreds of kilobytes of table that pushes the readable part of the
# page below anything a model will quote from.
#
# 100 players is roughly the top sixth of the pool - past that the rows are
# £4.0m bench fodder whose ratings are noise, and each one still costs bytes on
# every request from every crawler.
TOP_PLAYERS = 100

# Rotation pairs and track-record rows are naturally short; these are the same
# counts the front end shows, so the server-rendered table is the same table
# rather than a truncated version of it.
TOP_PAIRS = 6
TRACK_RECORD_ROWS = 10


def _fmt(value, places=1):
    """A number for display, or an en dash. Mirrors the front end's `–` for a
    missing value so the two renderings of the same row read identically."""
    if value is None or value != value:
        return "–"
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "–"


def _int(value):
    if value is None or value != value:
        return "–"
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "–"


def _fixtures(player, n=3):
    """The next `n` gameweeks as {label, points, difficulty} for one player.

    `label` is the same "MUN (H)" form the interactive table uses. Difficulty
    comes through so the template can colour the cell with the site's one
    difficulty scale rather than inventing a second."""
    out = []
    for gw in (player.get("next_gameweeks") or [])[:n]:
        opponent = gw.get("opponent")
        if opponent is None:
            continue
        home = gw.get("was_home")
        suffix = "" if home is None else (" (H)" if home else " (A)")
        out.append({
            "label": f"{opponent}{suffix}",
            "event": gw.get("event"),
            "points": _fmt(gw.get("points")),
            "difficulty": gw.get("difficulty"),
        })
    return out


def player_rows(players, top_n=TOP_PLAYERS):
    """The highest-rated players, as display-ready rows.

    Sorted by rating, which is what the interactive table opens on - a reader
    arriving from a search result should find the page already showing what the
    snippet promised.

    Each row carries `path`, the player's own page. That is the half of this
    that compounds: a crawler reading the table finds several hundred internal
    links to pages that are themselves fully server-rendered, rather than a
    table of names it can do nothing with."""
    ranked = sorted(
        (p for p in players or [] if p.get("rating") is not None),
        key=lambda p: (-(p.get("rating") or 0), p.get("web_name") or ""))
    rows = []
    for p in ranked[:top_n]:
        rows.append({
            "name": p.get("web_name") or "",
            "path": p.get("path"),
            "pos": p.get("pos") or "",
            "team_name": p.get("team_name") or "",
            "team_code": p.get("team_code"),
            "form": _fmt(p.get("form")),
            "rating": _int(p.get("rating")),
            "cost": _fmt(p.get("cost")),
            "predicted": _fmt(p.get("predicted")),
            "status": (p.get("status") or "a").lower(),
            "fixtures": _fixtures(p),
        })
    return rows


def squad_rows(squad):
    """One AI squad as table rows, starters first then the bench.

    Deliberately the same columns as the Best XI table in the pane, including
    the captain and vice-captain marks: this replaces that table's contents, so
    a reader who does have JavaScript should not see the shape of the page
    change underneath them when the real one arrives."""
    rows = []
    for p in sorted(squad or [], key=lambda p: p.get("position") or 99):
        rows.append({
            "name": p.get("web_name") or "",
            "pos": p.get("pos") or "",
            "team_name": p.get("team_name") or "",
            "team_code": p.get("team_code"),
            "cost": _fmt(p.get("cost")),
            "predicted": _fmt(p.get("predicted")),
            "actual": _int(p.get("actual_points")) if p.get("actual_points") is not None else "–",
            "starting": bool(p.get("starting")),
            "armband": "C" if p.get("is_captain") else ("V" if p.get("is_vice_captain") else ""),
        })
    return rows


def best_xv_block(result):
    """The AI Best XI section: the squad plus the numbers above it."""
    if not result or not result.get("squad"):
        return None
    return {
        "gameweek": result.get("gameweek"),
        "formation": result.get("formation"),
        "squad_cost": _fmt(result.get("squad_cost")),
        "predicted_points": _fmt(result.get("predicted_points")),
        "team_rating": result.get("team_rating"),
        "rows": squad_rows(result["squad"]),
    }


def best_xv_history(snapshots, limit=TRACK_RECORD_ROWS):
    """Predicted vs actual for the frozen Best XI snapshots.

    This is the part of the site with something to say that nowhere else has:
    a published, unedited record of what a model predicted before the fact.
    It is worth being readable without JavaScript for that reason alone."""
    rows = []
    for s in (snapshots or [])[:limit]:
        predicted, actual = s.get("predicted_points"), s.get("actual_points")
        diff = None
        if predicted is not None and actual is not None:
            diff = actual - predicted
        rows.append({
            "gameweek": s.get("gameweek"),
            "formation": s.get("formation") or "",
            "squad_cost": _fmt(s.get("squad_cost")),
            "predicted": _fmt(predicted),
            "actual": _int(actual) if actual is not None else "pending",
            "diff": ("+" if (diff or 0) >= 0 else "") + _fmt(diff) if diff is not None else "–",
            "diff_positive": diff is not None and diff >= 0,
        })
    return rows


def manager_history(history, limit=TRACK_RECORD_ROWS):
    """The AI Manager's season so far, newest first."""
    rows = []
    for h in (history or [])[:limit]:
        rows.append({
            "gameweek": h.get("gameweek"),
            "value": _fmt(h.get("value")),
            "bank": _fmt(h.get("bank")),
            "chip": h.get("active_chip") or "–",
            "predicted": _fmt(h.get("predicted_points")),
            "actual": _int(h.get("points")) if h.get("points") is not None else "pending",
        })
    return rows


def rotation_block(rotation_df, difficulty_col="defensive_difficulty",
                   top_pairs=TOP_PAIRS):
    """The fixture rotator's two tables: the ranked pairs and the club grid.

    Only the defensive view is server-rendered. The attacking one is the same
    twenty clubs over the same gameweeks scored differently, and publishing both
    would double the page's byte count to say very nearly the same thing - which
    is also how two halves of one page end up competing as near-duplicates.
    Anyone who wants the attacking view has JavaScript, because it is behind a
    button."""
    from fixture_rotator import rank_rotation_pairs, team_fixture_map

    if rotation_df is None or rotation_df.empty:
        return None

    gameweeks = sorted(int(e) for e in rotation_df["event"].dropna().unique())
    fixture_map = team_fixture_map(rotation_df, difficulty_col)
    averages = rotation_df.groupby("team_name")[difficulty_col].mean().sort_values()

    # The green-to-red scale, reproduced exactly as colorFor() in app.js draws
    # it - same hue sweep, same span across the observed min and max rather than
    # a fixed 1-5. If the two disagreed, every cell on the page would change
    # colour the moment the script replaced the table, which reads as a bug even
    # though the numbers underneath are identical.
    observed = [f["difficulty"] for fixtures in fixture_map.values()
                for f in fixtures.values() if f.get("difficulty") is not None]
    lo, hi = (min(observed), max(observed)) if observed else (0, 0)

    def colour(difficulty):
        if difficulty is None:
            return None
        if hi == lo:
            return "hsl(120, 70%, 85%)"
        hue = 120 - ((float(difficulty) - lo) / (hi - lo) * 120)
        return f"hsl({hue:g}, 70%, 82%)"

    def cells(team):
        fixtures = fixture_map.get(team, {})
        out = []
        for gw in gameweeks:
            f = fixtures.get(gw) or fixtures.get(str(gw)) or {}
            out.append({"opponent": f.get("opponent") or "",
                        "colour": colour(f.get("difficulty"))})
        return out

    teams = [{"team_name": team, "average": round(float(avg), 1), "cells": cells(team)}
             for team, avg in averages.items()]

    pairs = []
    try:
        ranked = rank_rotation_pairs(rotation_df, difficulty_col)
    except Exception:
        # The pair ranking is the one part here that can fail on its own (it
        # pivots the table and needs at least a few clubs with fixtures). The
        # club grid below doesn't depend on it, so a failure costs one section
        # rather than the whole block.
        ranked = None
    if ranked is not None:
        for _, row in ranked.head(top_pairs).iterrows():
            pairs.append({
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "worst_week": round(float(row["worst_week"]), 1),
                "avg_difficulty": round(float(row["avg_difficulty"]), 1),
            })

    return {"gameweeks": gameweeks, "teams": teams, "pairs": pairs}


def build(players=None, rotation_df=None, best_xv=None, snapshots=None,
          mgr_history=None):
    """Everything the templates need, in one dict.

    Every section is independent and optional. A section that can't be built -
    no upcoming gameweek to solve a squad for, an empty database, a rotation
    table that needs fixtures the API hasn't published yet - comes back as None
    and its template renders nothing at all, rather than an empty table with a
    heading over it promising data that isn't there."""
    return {
        "players": player_rows(players) if players else [],
        "rotation": rotation_block(rotation_df) if rotation_df is not None else None,
        "best_xv": best_xv_block(best_xv) if best_xv else None,
        "best_xv_history": best_xv_history(snapshots),
        "manager_history": manager_history(mgr_history),
    }
