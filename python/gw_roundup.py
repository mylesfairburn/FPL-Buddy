"""The gameweek roundup - the page at /gameweek/<n>/roundup.

The briefing's opposite number. `gw_report` is written before a deadline and is
a set of claims; this is written after the round is settled and is a set of
results. Four sections: who scored, who didn't, which games went against the
table, and which clubs are on a run.

Same rules as the briefing, and for the same reasons:

  * Nothing here fetches anything. It takes the page index, the round's live
    stats and the season's fixtures, and decides what is worth printing.
  * Every sentence restates a number printed beside it. This page publishes
    itself overnight with nobody reading it first, so it must not be capable of
    saying something the data doesn't support.
  * An edition is built ONCE, when FPL has confirmed the round's stats, and is
    frozen immediately. A roundup that could be rebuilt later would drift as
    the pipeline moved on, and a results page that changes is worthless.

The one genuinely new judgement here is the shock section, which needs a league
table to call anything a shock. See `MIN_ROUNDS_FOR_TABLE`.
"""

import math

# A player has to have been on the pitch long enough for a blank to be his
# rather than the manager's. A 12-minute substitute who returned nothing is not
# an underperformer, and printing him as one is the section discrediting itself.
UNDERPERFORMER_MIN_MINUTES = 60

# And enough people have to own him for it to be news. This is the same
# reasoning as the briefing's injury section: a 3%-owned player blanking
# affects almost nobody reading.
UNDERPERFORMER_MIN_OWNERSHIP = 15.0

# What counts as a blank. Two points is an appearance and nothing else - one
# point plus a defensive contribution, or 60 minutes with no return.
UNDERPERFORMER_MAX_POINTS = 2

# How far below its opponent a winning side has to sit in the table before the
# result is a shock rather than a Saturday. Six places is roughly the gap
# between the bottom of one third of the table and the top of the next; below
# that, "8th beat 5th" is a sentence about nothing.
SHOCK_MIN_POSITION_GAP = 6

# The table has to mean something before it can judge one. Four rounds in, a
# single win still moves a club ten places, so a "shock" computed against it is
# noise given a headline. Below this many completed rounds the section returns
# nothing - the honest answer to "was that a shock" in August.
MIN_ROUNDS_FOR_TABLE = 5

# A run is three. Two of anything is a coincidence.
MOMENTUM_MIN_STREAK = 3

SECTION_SIZE = 3


def _num(value, default=None):
    """A number, or `default`. Values arrive from JSON and from pandas, so both
    None and NaN mean absent - and NaN does not compare usefully."""
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def _stat(rec, key, default=None):
    v = (rec.get("stats") or {}).get(key, default)
    if isinstance(v, float) and math.isnan(v):
        return default
    return v


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _join(items):
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def by_id(pages):
    """The page index re-keyed on FPL's `id`.

    The pages are keyed on `code`, which is stable across seasons and is what
    the URLs are built from - but the live endpoint reports on `id`, which is
    reassigned every summer. Both keys are on every record, so this is a
    re-index rather than a lookup that can fail.
    """
    return {rec["id"]: rec for rec in pages.values() if rec.get("id") is not None}


def _base_card(rec):
    """The fields every roundup card shares.

    Copied in rather than referenced, for the same reason the briefing copies
    them: this row is frozen and has to render years later without consulting a
    pipeline that has moved on.
    """
    return {
        "code": rec.get("code"),
        "name": rec.get("web_name") or rec.get("full_name"),
        "full_name": rec.get("full_name"),
        "path": rec.get("path"),
        "pos": rec.get("pos"),
        "team_code": rec.get("team_code"),
        "team_name": rec.get("team_name") or "",
        "cost": rec.get("cost"),
        "owned": _stat(rec, "selected_by_percent"),
    }


# ---- section 1: the top scorers -------------------------------------------

def top_scorers(pages_by_id, live_stats, limit=SECTION_SIZE):
    """The gameweek's highest points totals, with what made them up.

    Ranked on points, ownership descending as the tie-break. Two players on 13
    are the same story told at different volumes: the widely-owned one is the
    week most people had, and that is the one worth leading with.
    """
    picks = []
    for element_id, stats in (live_stats or {}).items():
        rec = pages_by_id.get(element_id)
        if rec is None:
            continue
        pts = _num(stats.get("total_points"))
        if pts is None:
            continue
        picks.append((pts, _num(_stat(rec, "selected_by_percent"), 0.0), rec, stats))

    picks.sort(key=lambda t: (-t[0], -t[1]))

    out = []
    for pts, _owned, rec, stats in picks[:limit]:
        card = _base_card(rec)
        card["points"] = int(pts)
        card["headline"] = f"{int(pts)} {_plural(int(pts), 'point')}"
        card["minutes"] = _num(stats.get("minutes"), 0)
        card["goals"] = _num(stats.get("goals_scored"), 0)
        card["assists"] = _num(stats.get("assists"), 0)
        card["bonus"] = _num(stats.get("bonus"), 0)
        card["bps"] = _num(stats.get("bps"), 0)
        card["clean_sheet"] = bool(_num(stats.get("clean_sheets"), 0))
        card["saves"] = _num(stats.get("saves"), 0)
        card["why"] = _scorer_reason(card)
        out.append(card)
    return out


def _scorer_reason(card):
    """One sentence, every clause a number also printed on the card."""
    name = card["name"]
    parts = []
    for value, word in ((card["goals"], "goal"), (card["assists"], "assist"),
                        (card["saves"], "save")):
        if value:
            parts.append(f"{int(value)} {_plural(int(value), word)}")
    if card["clean_sheet"]:
        parts.append("a clean sheet")
    if card["bonus"]:
        parts.append(f"{int(card['bonus'])} bonus {_plural(int(card['bonus']), 'point')}")

    line = f"{name} scored {card['points']} {_plural(card['points'], 'point')}"
    if parts:
        line += " — " + _join(parts)
    if card["minutes"]:
        # No comma when there was no breakdown to separate it from: "scored 2
        # points, from 90 minutes" is a comma splice waiting to be noticed.
        line += (", from " if parts else " from ") + f"{int(card['minutes'])} minutes"
    return line + "."


# ---- section 2: the underperformers ----------------------------------------

def underperformers(pages_by_id, live_stats, limit=SECTION_SIZE):
    """Widely-owned players who played and returned nothing.

    Ordered by ownership, because this section's whole justification is how
    many readers it happened to.

    The expected-goals figure is what makes it worth printing rather than a
    list of names to be annoyed at. "Blanked" and "blanked from 0.9 expected
    goals" are different weeks and lead to opposite decisions - the second is a
    player to keep - and this is the only place on the site that says which one
    it was.
    """
    picks = []
    for element_id, stats in (live_stats or {}).items():
        rec = pages_by_id.get(element_id)
        if rec is None:
            continue
        pts = _num(stats.get("total_points"))
        minutes = _num(stats.get("minutes"), 0)
        owned = _num(_stat(rec, "selected_by_percent"))
        if pts is None or owned is None:
            continue
        if minutes < UNDERPERFORMER_MIN_MINUTES:
            continue
        if pts > UNDERPERFORMER_MAX_POINTS or owned < UNDERPERFORMER_MIN_OWNERSHIP:
            continue
        picks.append((owned, rec, stats, pts, minutes))

    picks.sort(key=lambda t: -t[0])

    out = []
    for owned, rec, stats, pts, minutes in picks[:limit]:
        card = _base_card(rec)
        card["points"] = int(pts)
        card["headline"] = f"{int(pts)} {_plural(int(pts), 'point')}"
        card["minutes"] = minutes
        card["xg"] = _num(stats.get("expected_goals"))
        card["xa"] = _num(stats.get("expected_assists"))
        card["xgi"] = _num(stats.get("expected_goal_involvements"))
        card["bps"] = _num(stats.get("bps"), 0)
        card["why"] = _underperformer_reason(card, owned)
        out.append(card)
    return out


def _underperformer_reason(card, owned):
    name = card["name"]
    line = (f"{name} returned {card['points']} "
            f"{_plural(card['points'], 'point')} from {int(card['minutes'])} "
            f"minutes, owned by {owned:.1f}% of managers")

    # The distinction the section exists to draw. A meaningful expected-goals
    # figure with no return is bad luck; nothing behind it is a bad week.
    xgi = card["xgi"]
    if xgi is not None and xgi >= 0.3:
        line += (f". The underlying numbers were better than the score — "
                 f"{xgi:.2f} expected goal involvements")
    elif xgi is not None:
        line += f". There was little behind it either: {xgi:.2f} expected goal involvements"
    return line + "."


# ---- the league table ------------------------------------------------------

def league_table(fixtures_df, up_to_event=None):
    """A table built from results, as {team_id: row}.

    Computed here rather than fetched because FPL publishes no table endpoint,
    and the fixtures file already carries every score. Three points for a win,
    ordered on points then goal difference then goals scored - which is the
    Premier League's own order, minus the head-to-head tie-break that has never
    been needed.

    `up_to_event` is inclusive and optional. The shock section wants the table
    as it stood BEFORE the round it is judging, because a club that has just
    been beaten has already fallen for it.
    """
    if fixtures_df is None or not len(fixtures_df):
        return {}

    rows = {}

    def row(team_id):
        return rows.setdefault(int(team_id), {
            "team": int(team_id), "played": 0, "won": 0, "drawn": 0, "lost": 0,
            "gf": 0, "ga": 0, "gd": 0, "points": 0})

    for _, fx in fixtures_df.iterrows():
        if not bool(fx.get("finished")):
            continue
        event = _num(fx.get("event"))
        if event is None:
            continue
        if up_to_event is not None and event > up_to_event:
            continue
        home, away = _num(fx.get("team_h")), _num(fx.get("team_a"))
        hs, as_ = _num(fx.get("team_h_score")), _num(fx.get("team_a_score"))
        if None in (home, away, hs, as_):
            continue
        hs, as_ = int(hs), int(as_)

        h, a = row(home), row(away)
        h["played"] += 1
        a["played"] += 1
        h["gf"] += hs
        h["ga"] += as_
        a["gf"] += as_
        a["ga"] += hs
        if hs > as_:
            h["won"] += 1
            h["points"] += 3
            a["lost"] += 1
        elif as_ > hs:
            a["won"] += 1
            a["points"] += 3
            h["lost"] += 1
        else:
            h["drawn"] += 1
            a["drawn"] += 1
            h["points"] += 1
            a["points"] += 1

    for r in rows.values():
        r["gd"] = r["gf"] - r["ga"]

    ordered = sorted(rows.values(),
                     key=lambda r: (-r["points"], -r["gd"], -r["gf"]))
    for position, r in enumerate(ordered, start=1):
        r["position"] = position
    return rows


def completed_rounds(fixtures_df, up_to_event=None):
    """How many gameweeks have actually been played. The shock section needs a
    table with some history behind it before it can call anything unexpected."""
    if fixtures_df is None or not len(fixtures_df):
        return 0
    events = set()
    for _, fx in fixtures_df.iterrows():
        event = _num(fx.get("event"))
        if not bool(fx.get("finished")) or event is None:
            continue
        if up_to_event is not None and event > up_to_event:
            continue
        events.add(int(event))
    return len(events)


# ---- section 3: shock results ----------------------------------------------

_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n):
    """1 -> '1st'. The table positions read as positions rather than as
    quantities, which is what makes "14th beat 3rd" scan.

    The teens are the exception every naive version gets wrong: 11, 12 and 13
    take "th" despite ending in 1, 2 and 3."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIX.get(n % 10, 'th')}"


def shock_results(fixtures_df, gameweek, team_names=None, team_codes=None,
                  limit=SECTION_SIZE):
    """Results where the table said the other thing.

    Judged on the table as it stood going INTO this round - positions from
    every finished fixture before it. Using the table after the round would be
    circular: the win being judged has already moved both clubs, which flatters
    every upset into looking smaller than it was.

    Returns nothing at all for the first few rounds of a season. That is the
    section being honest rather than empty: a table five games old cannot
    support the claim that a result was against it, and generating one anyway
    is how a page starts printing confident nonsense every August.
    """
    if fixtures_df is None or not len(fixtures_df):
        return []
    if completed_rounds(fixtures_df, up_to_event=gameweek - 1) < MIN_ROUNDS_FOR_TABLE:
        return []

    before = league_table(fixtures_df, up_to_event=gameweek - 1)
    if not before:
        return []

    names = team_names or {}
    codes = team_codes or {}
    shocks = []

    for _, fx in fixtures_df.iterrows():
        if _num(fx.get("event")) != gameweek or not bool(fx.get("finished")):
            continue
        home, away = _num(fx.get("team_h")), _num(fx.get("team_a"))
        hs, as_ = _num(fx.get("team_h_score")), _num(fx.get("team_a_score"))
        if None in (home, away, hs, as_) or hs == as_:
            continue
        home, away, hs, as_ = int(home), int(away), int(hs), int(as_)

        winner, loser = (home, away) if hs > as_ else (away, home)
        w_row, l_row = before.get(winner), before.get(loser)
        if not w_row or not l_row:
            continue
        # A higher position number is a worse league position, so the winner
        # having the larger number is what makes this an upset.
        gap = w_row["position"] - l_row["position"]
        if gap < SHOCK_MIN_POSITION_GAP:
            continue

        shocks.append({
            "gap": gap,
            "home_name": names.get(home, f"Team {home}"),
            "away_name": names.get(away, f"Team {away}"),
            "home_code": codes.get(home),
            "away_code": codes.get(away),
            "home_score": hs,
            "away_score": as_,
            "winner_name": names.get(winner, f"Team {winner}"),
            "loser_name": names.get(loser, f"Team {loser}"),
            "winner_code": codes.get(winner),
            "winner_position": w_row["position"],
            "loser_position": l_row["position"],
            "at_home": winner == home,
            "headline": f"{names.get(home, home)} {hs}–{as_} {names.get(away, away)}",
        })

    shocks.sort(key=lambda s: -s["gap"])
    for s in shocks[:limit]:
        s["why"] = (
            f"{s['winner_name']}, {_ordinal(s['winner_position'])} going into "
            f"the round, beat {s['loser_name']} in "
            f"{_ordinal(s['loser_position'])}"
            f"{' at home' if s['at_home'] else ' away from home'} — "
            f"{s['gap']} {_plural(s['gap'], 'place')} between them in the table.")
    return shocks[:limit]


# ---- section 4: momentum ---------------------------------------------------

def momentum(fixtures_df, gameweek, team_names=None, team_codes=None,
             limit=SECTION_SIZE):
    """Clubs on a run, as of the end of this gameweek.

    Two runs are tracked and a club is reported on whichever is longer: wins,
    which is the league story, and clean sheets, which is the FPL one. A club
    can legitimately appear for both, and is listed once under the longer.

    Counted backwards from this gameweek, so the run is current by definition -
    a club that won five in a row and then lost last week is not on a run, and
    a section that reported it would be describing history rather than form.
    """
    if fixtures_df is None or not len(fixtures_df):
        return []

    names = team_names or {}
    codes = team_codes or {}

    # {team_id: [(event, won, clean_sheet), ...]} oldest first.
    history = {}
    for _, fx in fixtures_df.iterrows():
        event = _num(fx.get("event"))
        if not bool(fx.get("finished")) or event is None or event > gameweek:
            continue
        home, away = _num(fx.get("team_h")), _num(fx.get("team_a"))
        hs, as_ = _num(fx.get("team_h_score")), _num(fx.get("team_a_score"))
        if None in (home, away, hs, as_):
            continue
        event, hs, as_ = int(event), int(hs), int(as_)
        history.setdefault(int(home), []).append((event, hs > as_, as_ == 0))
        history.setdefault(int(away), []).append((event, as_ > hs, hs == 0))

    runs = []
    for team_id, games in history.items():
        games.sort(key=lambda g: g[0])
        # The run has to be live: if the most recent game isn't this gameweek,
        # the club didn't play (a blank week) and its streak is not news now.
        if not games or games[-1][0] != gameweek:
            continue

        wins = clean_sheets = 0
        for _event, won, _cs in reversed(games):
            if won:
                wins += 1
            else:
                break
        for _event, _won, cs in reversed(games):
            if cs:
                clean_sheets += 1
            else:
                break

        kind, count = ("wins", wins) if wins >= clean_sheets else ("clean sheets", clean_sheets)
        if count < MOMENTUM_MIN_STREAK:
            continue
        name = names.get(team_id, f"Team {team_id}")
        runs.append({
            "team": team_id,
            "team_name": name,
            "team_code": codes.get(team_id),
            "kind": kind,
            "count": count,
            "wins": wins,
            "clean_sheets": clean_sheets,
            "headline": f"{count} straight {kind}",
            "why": (f"{name} have won {wins} in a row." if kind == "wins"
                    else f"{name} have kept {clean_sheets} clean sheets in a row."),
        })

    runs.sort(key=lambda r: (-r["count"], r["team_name"]))
    return runs[:limit]


# ---- assembly --------------------------------------------------------------

def build(pages, gameweek, live_stats=None, fixtures_df=None, team_names=None,
          team_codes=None, season_label=None, scorecard=None):
    """The whole roundup, as the JSON-serialisable dict stored in `gw_roundup`.

    Every section can legitimately come back empty - a blank gameweek, an
    unreachable API, the first month of a season - and the template renders
    whatever is present. An edition with nothing in it is still worth storing:
    it records that the round was covered, rather than looking like a job that
    never ran.

    `scorecard` is passed in rather than read here, because it comes out of
    SQLite and this module deliberately touches nothing but its arguments.
    """
    pages_by_id = by_id(pages or {})
    live_stats = live_stats or {}

    roundup = {
        "gameweek": int(gameweek),
        "season": season_label,
        "top_scorers": top_scorers(pages_by_id, live_stats),
        "underperformers": underperformers(pages_by_id, live_stats),
        "shocks": shock_results(fixtures_df, int(gameweek), team_names, team_codes),
        "momentum": momentum(fixtures_df, int(gameweek), team_names, team_codes),
        # What the AI Best XI was predicted to score before the round and what
        # it actually scored. The one number on the site that costs something
        # to publish, which is exactly why it is worth publishing.
        "scorecard": scorecard,
    }
    roundup["summary"] = summarise(roundup)
    return roundup


def summarise(roundup):
    """The standfirst, and the text the social drafts and the RSS description
    reuse. Built from whichever sections have content."""
    gw = roundup["gameweek"]
    bits = []
    if roundup["top_scorers"]:
        t = roundup["top_scorers"][0]
        bits.append(f"{t['name']} top scored with {t['points']}")
    if roundup["shocks"]:
        bits.append(f"{roundup['shocks'][0]['headline']} was the result of the round")
    if roundup["momentum"]:
        m = roundup["momentum"][0]
        bits.append(f"{m['team_name']} are on {m['headline']}")
    if roundup["underperformers"]:
        u = roundup["underperformers"][0]
        bits.append(f"{u['name']} blanked for the {u['owned']:.0f}% who own him")

    if not bits:
        return (f"The Gameweek {gw} roundup. Nothing was recorded for this "
                "round — either it was a blank gameweek, or the stats weren't "
                "available when this page was written.")
    return f"Gameweek {gw}: " + "; ".join(bits) + "."
