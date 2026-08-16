"""One player, written up properly, once a night.

The gameweek briefing covers four sections at three players each and gives none
of them more than a sentence. This is the opposite: one player, five or six
paragraphs, and the actual case for or against picking him. It exists so there
is something new to post on the days between briefings - which is most days.

How a subject is chosen
-----------------------
Nine detectors run over the whole pool, each looking for a different reason a
player is worth writing about tonight. Every candidate they return carries a
score, the evidence behind it, and the angle it was found by; the highest score
across all of them wins the night.

Scores are comparable ACROSS detectors on purpose, and that is the one genuinely
delicate thing in this module. Each detector weights its own finding onto a
shared scale - roughly 3 to 9 for a normal candidate - by how much a reader
should care, so a player returning from six weeks out beats a mild
expected-goals overperformance, and both lose to nothing at all if the pool is
quiet. Getting this wrong doesn't break anything; it just picks a duller player.

Six of the nine, and three of the nine
--------------------------------------
The first six detectors all read this season's per-gameweek history, and until
that history exists none of them can fire. That is not a rare edge: it is every
night of preseason and the first fortnight of the season, and it used to mean
`choose` returned None for weeks at a stretch while the job reported success -
a silence indistinguishable from a broken cron.

So there are three more, gated on `ctx["early"]`, built only from data that
exists before a ball is kicked: last season's totals, the price FPL published,
and the fixture list. They retire on their own once EARLY_ROUNDS_MAX rounds
have been played, which is roughly when the six real ones start finding things.
Nothing else about them is special - same shape, same scale, same ledger.

Why there is no language model in here
--------------------------------------
The prose is templated from thresholds, exactly like `gw_report`. Every sentence
restates a number printed beside it, which means this cannot say something the
data doesn't support - and it publishes itself at 03:15 with nobody reading it
first. The cost of that choice is that a template repeats itself, so:

  * nine angles, and the ledger stops the same angle running two nights running
  * three or four phrasings of each clause, chosen by a hash of (code, date),
    so the wording is reproducible but not identical week to week
  * the ledger also blocks the same player inside a fortnight

That gets a season's worth of posts out of it without the same paragraph
appearing twice in a row. It does not make it read like a person, and it isn't
meant to.
"""

import hashlib
import math
from datetime import date

# --- windows ---------------------------------------------------------------

# How many recent rounds the form-shaped detectors judge over. Five is about
# six weeks of football: long enough that one good afternoon doesn't carry it,
# short enough to still be describing now.
FORM_WINDOW = 5

# A player has to have played about this much across the window for anything
# derived from it to mean something. Two substitute appearances produce ratios
# that look dramatic and describe nothing.
MIN_WINDOW_MINUTES = 200

# --- injury return ---------------------------------------------------------

# Rounds missed before a return is a story. Two is a knock; three is an absence
# that changed the team, which is what makes the before-and-after worth printing.
INJURY_MIN_ROUNDS_OUT = 3

# And he has to have been worth something before it. A player who was not
# playing well before he got hurt is not a returning asset, he is a squad
# player who is now fit.
INJURY_MIN_POINTS_PER_90 = 3.0

# --- underlying-stats detectors --------------------------------------------

# How far actual returns must sit from the expected-goal involvements over the
# window before it is worth writing about. Below this it is noise: xGI is a
# model of chances, not a promise, and half a goal either way is the normal
# spread rather than a signal.
UNDERLYING_MIN_GAP = 1.5

# --- minutes ---------------------------------------------------------------

# Starts in the last three rounds, against starts in the three before, that
# make a player "newly nailed".
NAILED_RECENT_STARTS = 3
NAILED_PRIOR_MAX_STARTS = 1

# --- fixtures --------------------------------------------------------------

# FPL's own fixture difficulty runs 1-5. A swing of this much between the last
# five games and the next five is a genuine change of schedule rather than the
# ordinary lumpiness of a fixture list.
FIXTURE_MIN_SWING = 0.8

# --- team-level ------------------------------------------------------------

# Goals conceded against expected goals conceded, across the window. A defence
# a goal and a half worse than its chances conceded has been unlucky, and that
# is a clean sheet waiting to happen.
TEAM_MIN_GAP = 1.5

# --- early season ----------------------------------------------------------

# Rounds of this season that have to be in the history before the four
# early-season angles stand down.
#
# Three rather than one, because the six in-season detectors do not become
# useful the moment a round exists - most of them want MIN_WINDOW_MINUTES
# behind them, which is two or three full games. Retiring these at the first
# whistle would reopen the same silence a fortnight later.
EARLY_ROUNDS_MAX = 3

# Minutes a player needs from LAST season before his totals are worth quoting.
# 900 is ten full games - the same bar team_service uses for its previous-season
# fallback, and about where a per-90 stops being an artefact of three cameos.
PRESEASON_MIN_MINUTES = 900

# Goals per 90 last season that make an attacker worth a post. 0.30 is roughly
# eleven goals in a full season of minutes: a genuine returner rather than a
# midfielder who got on the end of a few.
PRESEASON_MIN_GOALS_PER_90 = 0.30

# Price drift since FPL published the season's prices, in tenths of a million.
# Two is 0.2m, which preseason is a lot of managers moving in one direction.
PRICE_MIN_CHANGE = 2

# Mean FPL difficulty across the opening run that counts as an easy start.
#
# The scale is 1-5, but the means bunch far harder than that suggests: over
# 2026-27's first five gameweeks every club sits between 2.6 and 3.6, and
# thirteen of the twenty are on 3.0 exactly. So this is not "a soft schedule on
# a 1-5 scale", it is "measurably below the league's own flat middle" - set at
# 2.9 rather than the 2.6 that reads better and matches one club in twenty.
OPENING_MAX_DIFFICULTY = 2.9

# --- scoring ---------------------------------------------------------------

# The ceiling every detector clamps its score to.
#
# Deliberately well above where a normal candidate lands (most sit between 3
# and 9), because a clamp that bites is a clamp that creates ties - and a tie
# at the top is resolved by whichever detector happens to run first, which is
# a silent editorial decision made by the order of a tuple. This exists only to
# stop one absurd input dominating a season, not to compress the scale.
SCORE_CEILING = 20.0

# --- the ledger ------------------------------------------------------------

# How long before the same player can be written about again. A fortnight is
# roughly two gameweeks, so a player genuinely worth a second post gets one
# once something has actually changed.
REPEAT_PLAYER_DAYS = 14


# ---------------------------------------------------------------------------
#  Small helpers
# ---------------------------------------------------------------------------

def _num(value, default=None):
    """A number, or `default`. None and NaN both mean absent."""
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


def _is_available(rec):
    """Fit enough to be recommended. Same rule as the briefing: no published
    chance means fit, because FPL only fills that field in when it has
    something to say."""
    if (rec.get("status") or "a").lower() != "a":
        return False
    chance = rec.get("chance_of_playing_next_round")
    if chance is None or chance != chance:
        return True
    return chance >= 100


def _prev(rec, key, default=None):
    """One of last season's totals for this player, or `default`.

    `prev_season` is attached by player_pages.build_index and carries five
    columns - minutes, goals_scored, expected_goals, goals_conceded and
    expected_goals_conceded - summed across last season. It is None for anyone
    who did not play in the Premier League last season, which the early-season
    detectors read as a signal rather than as missing data.
    """
    prev = rec.get("prev_season")
    if not isinstance(prev, dict):
        return default
    return _num(prev.get(key), default)


def _predicted_for(rec, gameweek):
    """This player's projection for `gameweek` specifically - matched on
    `event`, never taken as the first entry. See gw_report._predicted_for for
    why that distinction is load-bearing."""
    for g in rec.get("next_gameweeks") or []:
        if g.get("event") == gameweek:
            v = g.get("points", g.get("predicted_points"))
            return float(v) if v is not None else None
    return None


def variant(options, code, day, salt=""):
    """One of several phrasings, chosen deterministically.

    Hashed on the player and the date rather than picked at random, so the same
    post regenerated is the same post - a job that produced different prose on
    a re-run would make every bug in here unreproducible. The hash is only ever
    used to pick a sentence, so its weakness is irrelevant.
    """
    if not options:
        return ""
    key = f"{code}|{day}|{salt}".encode("utf-8")
    return options[hashlib.sha1(key).digest()[0] % len(options)]


# ---------------------------------------------------------------------------
#  History
# ---------------------------------------------------------------------------

def history_by_player(history_df):
    """{element_id: [row dicts, oldest round first]}.

    Built once and passed to every detector rather than filtered per player:
    the frame carries a row per player per round, so filtering it ~700 times is
    the difference between a job that takes a second and one that takes a
    minute.

    Rows for rounds that haven't happened are not in the file at all, and a
    round a player wasn't in the squad for is usually absent rather than
    present with zero minutes - so absence and a blank are both treated as "did
    not play" by everything downstream.
    """
    if history_df is None or not len(history_df):
        return {}
    needed = {"player_id", "round"}
    if not needed.issubset(set(history_df.columns)):
        return {}

    out = {}
    for row in history_df.to_dict("records"):
        pid, rnd = _num(row.get("player_id")), _num(row.get("round"))
        if pid is None or rnd is None:
            continue
        out.setdefault(int(pid), []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: _num(r.get("round"), 0))
    return out


def _window(rows, n=FORM_WINDOW):
    """The last `n` rounds a player actually appeared in the data for."""
    return rows[-n:] if rows else []


def _totals(rows, *columns):
    """Column sums across rows, as a dict. Missing columns total zero, which is
    the right answer for a stat FPL stopped publishing rather than a crash."""
    out = {}
    for col in columns:
        out[col] = sum(_num(r.get(col), 0.0) or 0.0 for r in rows)
    return out


def _per_90(value, minutes):
    return (value * 90.0 / minutes) if minutes else None


# ---------------------------------------------------------------------------
#  Team-level tables, built from fixtures
# ---------------------------------------------------------------------------

def team_results(fixtures_df):
    """{team_id: {event: (goals_for, goals_against)}} for finished fixtures.

    The injury section's before-and-after needs this: "the defence conceded 1.2
    a game while he played and 2.1 while he was out" is the sentence the whole
    angle is built around, and there is nowhere else in the data to get it.
    """
    if fixtures_df is None or not len(fixtures_df):
        return {}
    out = {}
    for fx in fixtures_df.to_dict("records"):
        if not bool(fx.get("finished")):
            continue
        event = _num(fx.get("event"))
        home, away = _num(fx.get("team_h")), _num(fx.get("team_a"))
        hs, as_ = _num(fx.get("team_h_score")), _num(fx.get("team_a_score"))
        if None in (event, home, away, hs, as_):
            continue
        event = int(event)
        out.setdefault(int(home), {})[event] = (int(hs), int(as_))
        out.setdefault(int(away), {})[event] = (int(as_), int(hs))
    return out


def team_difficulty(fixtures_df, team_id, gameweek, back=FORM_WINDOW,
                    forward=FORM_WINDOW):
    """(mean difficulty of the last `back` finished games,
        mean difficulty of the next `forward` scheduled ones,
        labels for those upcoming games).

    Uses FPL's own 1-5 difficulty rating, which is published per fixture per
    side. It is a blunt instrument - it barely moves during a season - but it
    is the one difficulty figure a reader has also seen in the official app,
    which makes it the right one to quote at them.
    """
    if fixtures_df is None or not len(fixtures_df):
        return None, None, []

    past, future = [], []
    for fx in fixtures_df.to_dict("records"):
        event = _num(fx.get("event"))
        home, away = _num(fx.get("team_h")), _num(fx.get("team_a"))
        if event is None or home is None or away is None:
            continue
        if int(home) == team_id:
            difficulty, opponent, at_home = _num(fx.get("team_h_difficulty")), int(away), True
        elif int(away) == team_id:
            difficulty, opponent, at_home = _num(fx.get("team_a_difficulty")), int(home), False
        else:
            continue
        if difficulty is None:
            continue
        if bool(fx.get("finished")) and int(event) < gameweek:
            past.append((int(event), difficulty))
        elif int(event) >= gameweek:
            future.append((int(event), difficulty, opponent, at_home))

    past.sort(key=lambda t: t[0])
    future.sort(key=lambda t: t[0])
    past = past[-back:]
    future = future[:forward]

    past_mean = sum(d for _e, d in past) / len(past) if past else None
    future_mean = (sum(d for _e, d, _o, _h in future) / len(future)
                   if future else None)
    return past_mean, future_mean, future


def team_expected_conceded(history_rows_by_player, team_of, team_id, rounds):
    """(goals conceded, expected goals conceded) for a club across `rounds`.

    Both come from the per-player gameweek rows, because FPL publishes no
    team-level table: every player who was on the pitch for a whole match
    carries his side's figures for that match, so one full-match player per
    round is enough. Players with fewer than an hour are excluded - a
    substitute's `expected_goals_conceded` covers only the minutes he played
    and would drag the average below what the defence actually faced.
    """
    per_round = {}
    for element_id, rows in history_rows_by_player.items():
        if team_of.get(element_id) != team_id:
            continue
        for row in rows:
            rnd = _num(row.get("round"))
            if rnd is None or int(rnd) not in rounds:
                continue
            if (_num(row.get("minutes"), 0) or 0) < 60:
                continue
            gc = _num(row.get("goals_conceded"))
            xgc = _num(row.get("expected_goals_conceded"))
            if gc is None:
                continue
            # Highest xGC among the full-match players for that round. They
            # should all be identical; taking the max rather than the mean
            # means one substituted defender's part-match figure can't pull it
            # down.
            best = per_round.get(int(rnd))
            if best is None or (xgc or 0) > (best[1] or 0):
                per_round[int(rnd)] = (gc, xgc)

    if not per_round:
        return None, None
    conceded = sum(gc for gc, _x in per_round.values())
    expected = sum(x for _g, x in per_round.values() if x is not None)
    return conceded, (expected if any(x is not None for _g, x in per_round.values()) else None)


# ---------------------------------------------------------------------------
#  Detectors
#
#  Each takes the same context dict and returns candidate dicts:
#
#     {"code", "angle", "score", "evidence": {...}}
#
#  `score` is on a shared 0-10 scale so the six can be compared. `evidence` is
#  whatever the prose for that angle needs, and is stored on the post so the
#  page can print the numbers the sentences are made of.
# ---------------------------------------------------------------------------

def detect_injury_return(ctx):
    """Fit again after a run of rounds out.

    The angle the task was really asking for: what he was doing before, what
    the team did without him, and whether he is worth coming back in for.

    The absence is read off the gameweek rows rather than off FPL's status
    flag, because the flag only says what is true now. A player unflagged this
    morning has no record anywhere of having been injured - the six rounds of
    zero minutes behind him are the only evidence that he was.
    """
    out = []
    for code, rec in ctx["pages"].items():
        if not _is_available(rec):
            continue
        rows = ctx["history"].get(rec.get("id")) or []
        if not rows:
            continue

        # The trailing run of rounds with no minutes.
        out_rounds = []
        for row in reversed(rows):
            if (_num(row.get("minutes"), 0) or 0) > 0:
                break
            out_rounds.append(int(_num(row.get("round"), 0)))
        if len(out_rounds) < INJURY_MIN_ROUNDS_OUT:
            continue
        out_rounds = sorted(out_rounds)

        # What he was doing before it. The rounds before the gap, capped at the
        # form window so a player who was excellent last autumn and mediocre
        # since isn't described by the autumn.
        played = [r for r in rows if (_num(r.get("minutes"), 0) or 0) > 0]
        before = _window(played)
        if not before:
            continue
        totals = _totals(before, "minutes", "total_points", "goals_scored",
                         "assists", "expected_goal_involvements")
        minutes = totals["minutes"]
        if minutes < MIN_WINDOW_MINUTES:
            continue
        points_per_90 = _per_90(totals["total_points"], minutes)
        if points_per_90 is None or points_per_90 < INJURY_MIN_POINTS_PER_90:
            continue

        team_id = rec.get("team")
        played_rounds = {int(_num(r.get("round"), 0)) for r in before}
        gc_in, xgc_in = team_expected_conceded(
            ctx["history"], ctx["team_of"], team_id, played_rounds)
        gc_out, xgc_out = team_expected_conceded(
            ctx["history"], ctx["team_of"], team_id, set(out_rounds))

        predicted = _predicted_for(rec, ctx["gameweek"])

        evidence = {
            "rounds_out": len(out_rounds),
            "out_from": out_rounds[0],
            "out_to": out_rounds[-1],
            "before_rounds": len(before),
            "before_minutes": round(minutes),
            "before_points": round(totals["total_points"]),
            "before_points_per_90": round(points_per_90, 2),
            "before_goals": round(totals["goals_scored"]),
            "before_assists": round(totals["assists"]),
            "before_xgi_per_90": (round(_per_90(totals["expected_goal_involvements"],
                                                minutes), 2)
                                  if minutes else None),
            "team_conceded_with": (round(gc_in / len(played_rounds), 2)
                                   if gc_in is not None and played_rounds else None),
            "team_conceded_without": (round(gc_out / len(out_rounds), 2)
                                      if gc_out is not None and out_rounds else None),
            "predicted": round(predicted, 1) if predicted is not None else None,
        }

        # Weighted on what he was producing, lengthened a little by how long he
        # was away - a six-week absence is a bigger story than a three-week one
        # because more people will have moved him on.
        score = min(SCORE_CEILING, points_per_90 * 1.4
                    + min(len(out_rounds), 8) * 0.35
                    + (predicted or 0) * 0.3)
        out.append({"code": code, "angle": "injury_return", "score": score,
                    "evidence": evidence})
    return out


def _underlying_candidates(ctx, direction):
    """Shared body for the two underlying-stats angles.

    `direction` is +1 for a player whose expected numbers are ahead of his
    returns (unlucky, and the interesting one) and -1 for the reverse
    (overperforming, and the one to be careful of). The arithmetic is identical
    and only the sign of the gap and the wording differ, so writing it twice
    would be two places for the threshold to drift apart.
    """
    angle = "unlucky" if direction > 0 else "regression"
    out = []
    for code, rec in ctx["pages"].items():
        if not _is_available(rec):
            continue
        rows = _window(ctx["history"].get(rec.get("id")) or [])
        if not rows:
            continue
        totals = _totals(rows, "minutes", "total_points", "goals_scored",
                         "assists", "expected_goals", "expected_assists",
                         "expected_goal_involvements")
        minutes = totals["minutes"]
        if minutes < MIN_WINDOW_MINUTES:
            continue

        returns = totals["goals_scored"] + totals["assists"]
        expected = totals["expected_goal_involvements"]
        gap = (expected - returns) * direction
        if gap < UNDERLYING_MIN_GAP:
            continue

        predicted = _predicted_for(rec, ctx["gameweek"])
        owned = _num(_stat(rec, "selected_by_percent"))
        out.append({
            "code": code, "angle": angle,
            # The gap is the story, and the projection is how much anyone
            # should act on it. A big gap on a player the model doesn't rate is
            # a curiosity rather than a transfer.
            "score": min(SCORE_CEILING, gap * 1.8 + (predicted or 0) * 0.45),
            "evidence": {
                "rounds": len(rows),
                "minutes": round(minutes),
                "points": round(totals["total_points"]),
                "goals": round(totals["goals_scored"]),
                "assists": round(totals["assists"]),
                "returns": round(returns),
                "xg": round(totals["expected_goals"], 2),
                "xa": round(totals["expected_assists"], 2),
                "xgi": round(expected, 2),
                "gap": round(abs(expected - returns), 2),
                "owned": round(owned, 1) if owned is not None else None,
                "predicted": round(predicted, 1) if predicted is not None else None,
            },
        })
    return out


def detect_unlucky(ctx):
    """Creating and not scoring. The buy-low case."""
    return _underlying_candidates(ctx, +1)


def detect_regression(ctx):
    """Scoring more than the chances justify. The be-careful case.

    Included deliberately, even though it is the post nobody wants to read
    about a player they own. A site that only ever publishes reasons to buy is
    a site whose recommendations are worth nothing.
    """
    return _underlying_candidates(ctx, -1)


def detect_newly_nailed(ctx):
    """Started the last three after not starting before them.

    Minutes are the single biggest predictor of FPL points and the slowest
    thing for the market to notice, because ownership moves on goals. A player
    who has quietly become a starter is the most actionable thing in the data
    and the least visible.
    """
    out = []
    for code, rec in ctx["pages"].items():
        if not _is_available(rec):
            continue
        rows = ctx["history"].get(rec.get("id")) or []
        if len(rows) < NAILED_RECENT_STARTS * 2:
            continue
        recent = rows[-NAILED_RECENT_STARTS:]
        prior = rows[-NAILED_RECENT_STARTS * 2:-NAILED_RECENT_STARTS]

        def starts(window):
            # `starts` is FPL's own column. Falling back to a 60-minute rule
            # keeps this working for a season where the column is absent, which
            # has happened before.
            return sum(1 for r in window
                       if (_num(r.get("starts"), 0) or 0) >= 1
                       or (_num(r.get("minutes"), 0) or 0) >= 60)

        recent_starts, prior_starts = starts(recent), starts(prior)
        if recent_starts < NAILED_RECENT_STARTS or prior_starts > NAILED_PRIOR_MAX_STARTS:
            continue

        recent_totals = _totals(recent, "minutes", "total_points",
                                "goals_scored", "assists",
                                "expected_goal_involvements")
        prior_totals = _totals(prior, "minutes")
        predicted = _predicted_for(rec, ctx["gameweek"])
        owned = _num(_stat(rec, "selected_by_percent"))

        out.append({
            "code": code, "angle": "newly_nailed",
            # Low ownership lifts this: a nailed-on starter nobody has noticed
            # is the whole point, and one 30% of the game already owns is news
            # to nobody.
            "score": min(SCORE_CEILING, 3.5 + (predicted or 0) * 0.7
                         + (2.0 if (owned or 100) < 10 else 0.0)),
            "evidence": {
                "recent_rounds": len(recent),
                "recent_starts": recent_starts,
                "recent_minutes": round(recent_totals["minutes"]),
                "recent_points": round(recent_totals["total_points"]),
                "recent_goals": round(recent_totals["goals_scored"]),
                "recent_assists": round(recent_totals["assists"]),
                "recent_xgi": round(recent_totals["expected_goal_involvements"], 2),
                "prior_rounds": len(prior),
                "prior_starts": prior_starts,
                "prior_minutes": round(prior_totals["minutes"]),
                "owned": round(owned, 1) if owned is not None else None,
                "predicted": round(predicted, 1) if predicted is not None else None,
            },
        })
    return out


def detect_fixture_swing(ctx):
    """A club whose schedule turns, and its best-projected player.

    Judged on FPL's own published difficulty rather than on this site's
    rotation figure. The rotation planner's number is better, but it is a net
    strength difference on a scale nobody has seen before; the 1-5 rating is
    the one already printed next to every fixture in the official app, so
    quoting it means a reader can check the claim in ten seconds.
    """
    out = []
    seen_teams = set()
    for code, rec in sorted(ctx["pages"].items(),
                            key=lambda kv: -(_predicted_for(kv[1], ctx["gameweek"]) or 0)):
        team_id = rec.get("team")
        if team_id is None or team_id in seen_teams or not _is_available(rec):
            continue
        past, future, upcoming = team_difficulty(
            ctx["fixtures"], team_id, ctx["gameweek"])
        if past is None or future is None:
            continue
        swing = past - future
        if swing < FIXTURE_MIN_SWING:
            continue

        # One player per club - the best-projected fit one, which is what the
        # sorted() above delivers first. Without this the whole section is six
        # players from the same team.
        seen_teams.add(team_id)
        predicted = _predicted_for(rec, ctx["gameweek"])
        names = ctx.get("team_names") or {}
        out.append({
            "code": code, "angle": "fixture_swing",
            "score": min(SCORE_CEILING, swing * 2.2 + (predicted or 0) * 0.5),
            "evidence": {
                "team_name": names.get(team_id) or rec.get("team_name"),
                "past_difficulty": round(past, 2),
                "future_difficulty": round(future, 2),
                "swing": round(swing, 2),
                "upcoming": [
                    {"event": e,
                     "opponent": (ctx.get("team_shorts") or {}).get(o) or str(o),
                     "at_home": bool(h), "difficulty": round(d, 1)}
                    for e, d, o, h in upcoming],
                "predicted": round(predicted, 1) if predicted is not None else None,
            },
        })
    return out


def detect_unlucky_defence(ctx):
    """A club conceding more than the chances it gives up, and its best
    defensive asset.

    The team-level version of the unlucky angle, and the one that produces the
    cheapest useful transfer in FPL - a defence whose expected goals conceded
    says it should have three clean sheets and has none is a defence about to
    get some.
    """
    out = []
    by_team = {}
    for code, rec in ctx["pages"].items():
        if rec.get("pos") not in ("DEF", "GK") or not _is_available(rec):
            continue
        predicted = _predicted_for(rec, ctx["gameweek"]) or 0
        team_id = rec.get("team")
        if team_id is None:
            continue
        if team_id not in by_team or predicted > by_team[team_id][1]:
            by_team[team_id] = (code, predicted)

    recent_rounds = set(ctx["recent_rounds"])
    if not recent_rounds:
        return out

    for team_id, (code, predicted) in by_team.items():
        conceded, expected = team_expected_conceded(
            ctx["history"], ctx["team_of"], team_id, recent_rounds)
        if conceded is None or expected is None:
            continue
        gap = conceded - expected
        if gap < TEAM_MIN_GAP:
            continue
        rec = ctx["pages"][code]
        names = ctx.get("team_names") or {}
        out.append({
            "code": code, "angle": "unlucky_defence",
            "score": min(SCORE_CEILING, gap * 1.6 + predicted * 0.6),
            "evidence": {
                "team_name": names.get(team_id) or rec.get("team_name"),
                "rounds": len(recent_rounds),
                "conceded": round(conceded),
                "expected_conceded": round(expected, 2),
                "gap": round(gap, 2),
                "conceded_per_game": round(conceded / len(recent_rounds), 2),
                "expected_per_game": round(expected / len(recent_rounds), 2),
                # The per-game gap as well as the window total. The table beside
                # the prose prints per-game figures, and a total sitting among
                # them reads as a per-game number that is wildly out - "3.0
                # conceded, 1.0 expected, gap 10.0" invites exactly the wrong
                # arithmetic.
                "gap_per_game": round(gap / len(recent_rounds), 2),
                "predicted": round(predicted, 1),
            },
        })
    return out


# ---------------------------------------------------------------------------
#  Early season
#
#  The four below fire only while ctx["early"] is set - preseason and the first
#  couple of rounds. Everything above this line reads this season's per-gameweek
#  history; everything below it deliberately does not touch it, because during
#  the window these run in, it does not exist.
#
#  They are held to the same standard as the rest: every claim comes off a
#  number that is printed beside it. "Last season" is stated in every sentence
#  they produce, because a per-90 from May quoted without its date is the one
#  genuinely misleading thing this module could publish.
# ---------------------------------------------------------------------------

def detect_preseason_form(ctx):
    """An attacker who scored at a real rate last season.

    Restricted to midfielders and forwards because the only attacking columns
    `prev_season` carries are goals and expected goals - no assists - and a
    defender judged on goals alone would be ranked by the thing he does least.
    Defenders get their preseason look through the fixtures angle below.
    """
    if not ctx["early"]:
        return []
    out = []
    for code, rec in ctx["pages"].items():
        if rec.get("pos") not in ("MID", "FWD") or not _is_available(rec):
            continue
        minutes = _prev(rec, "minutes")
        goals = _prev(rec, "goals_scored")
        if not minutes or minutes < PRESEASON_MIN_MINUTES or goals is None:
            continue

        per_90 = _per_90(goals, minutes)
        if per_90 is None or per_90 < PRESEASON_MIN_GOALS_PER_90:
            continue

        xg = _prev(rec, "expected_goals")
        predicted = _predicted_for(rec, ctx["gameweek"])
        owned = _num(_stat(rec, "selected_by_percent"))
        out.append({
            "code": code, "angle": "preseason_form",
            # The rate is the claim and the projection is what to do about it,
            # same balance the in-season angles strike. Low ownership lifts it
            # for the same reason it lifts newly_nailed: a proven scorer nobody
            # has drafted is the more useful post.
            "score": min(SCORE_CEILING, per_90 * 8.0 + (predicted or 0) * 0.5
                         + (1.5 if (owned or 100) < 10 else 0.0)),
            "evidence": {
                "prev_minutes": round(minutes),
                "prev_goals": round(goals),
                "goals_per_90": round(per_90, 2),
                "prev_xg": round(xg, 2) if xg is not None else None,
                "xg_per_90": (round(_per_90(xg, minutes), 2)
                              if xg is not None else None),
                "owned": round(owned, 1) if owned is not None else None,
                "predicted": round(predicted, 1) if predicted is not None else None,
            },
        })
    return out


def detect_price_watch(ctx):
    """A player the market has already moved on, before a ball is kicked.

    `cost_change_start` is the one column that carries information in August:
    it is the only thing in the bootstrap that moves during preseason, and it
    moves because hundreds of thousands of managers have drafted the same
    player. Worth writing up in both directions - a riser is a consensus pick
    to check, a faller is one the market has quietly given up on.
    """
    if not ctx["early"]:
        return []
    out = []
    for code, rec in ctx["pages"].items():
        if not _is_available(rec):
            continue
        change = _num(_stat(rec, "cost_change_start"))
        if change is None or abs(change) < PRICE_MIN_CHANGE:
            continue

        predicted = _predicted_for(rec, ctx["gameweek"])
        owned = _num(_stat(rec, "selected_by_percent"))
        transfers = _num(_stat(rec, "transfers_in"))
        out.append({
            "code": code, "angle": "price_watch",
            "score": min(SCORE_CEILING, 2.5 + abs(change) * 0.6
                         + (predicted or 0) * 0.4),
            "evidence": {
                # Tenths of a million in the raw column; nobody outside the API
                # thinks in tenths, so it is converted once, here, rather than
                # in each of the three places that print it.
                "price_change": round(change / 10.0, 1),
                "direction": "risen" if change > 0 else "fallen",
                "cost": rec.get("cost"),
                "transfers_in": round(transfers) if transfers is not None else None,
                "owned": round(owned, 1) if owned is not None else None,
                "predicted": round(predicted, 1) if predicted is not None else None,
            },
        })
    return out


# There is deliberately no "new signing" angle here, and it is worth writing
# down why so it isn't added back.
#
# Spotting one is trivial - a player with no `prev_season` row recorded no
# Premier League minutes last season - and in 2026-27 that matches 88 players
# priced at £5.0m or more. The problem is that there is nothing to say about
# any of them. The ratings this window runs on are built from last season's
# numbers, so a player with no last season has no projection: of those 88, six
# carried a projection at all and every one of those was 0.00. The post would
# be a name, a price, and an admission that this site has no opinion - which is
# the exact post the module docstring says not to publish.
#
# It would need the rating model to price a newcomer from something other than
# his own history - transfer fee, or the league he came from - and that is a
# change to the model, not a detector.


def detect_opening_fixtures(ctx):
    """A club with a kind opening run, and its best-projected player.

    The forward-looking half of `detect_fixture_swing`. That one needs a `past`
    mean to measure a swing against, and before the season there are no
    finished fixtures to build one from - so it judges the opening run on its
    own merits instead. Same difficulty scale, same one-player-per-club rule.
    """
    if not ctx["early"]:
        return []
    out = []
    seen_teams = set()
    for code, rec in sorted(ctx["pages"].items(),
                            key=lambda kv: -(_predicted_for(kv[1], ctx["gameweek"]) or 0)):
        team_id = rec.get("team")
        if team_id is None or team_id in seen_teams or not _is_available(rec):
            continue
        _past, future, upcoming = team_difficulty(
            ctx["fixtures"], team_id, ctx["gameweek"])
        if future is None or future > OPENING_MAX_DIFFICULTY:
            continue

        seen_teams.add(team_id)
        predicted = _predicted_for(rec, ctx["gameweek"])
        names = ctx.get("team_names") or {}
        out.append({
            "code": code, "angle": "opening_fixtures",
            # Distance below an average schedule, not the raw mean: a smaller
            # number is better here, and scoring the mean directly would rank
            # the hardest run top.
            "score": min(SCORE_CEILING, (3.0 - future) * 3.0
                         + (predicted or 0) * 0.6),
            "evidence": {
                "team_name": names.get(team_id) or rec.get("team_name"),
                "future_difficulty": round(future, 2),
                "games": len(upcoming),
                "upcoming": [
                    {"event": e,
                     "opponent": (ctx.get("team_shorts") or {}).get(o) or str(o),
                     "at_home": bool(h), "difficulty": round(d, 1)}
                    for e, d, o, h in upcoming],
                "predicted": round(predicted, 1) if predicted is not None else None,
            },
        })
    return out


DETECTORS = (detect_injury_return, detect_unlucky, detect_regression,
             detect_newly_nailed, detect_fixture_swing, detect_unlucky_defence,
             detect_preseason_form, detect_price_watch,
             detect_opening_fixtures)


# ---------------------------------------------------------------------------
#  Choosing tonight's subject
# ---------------------------------------------------------------------------

def candidates(pages, gameweek, history_df=None, fixtures_df=None,
               team_names=None, team_shorts=None):
    """Every candidate every detector found, best first."""
    history = history_by_player(history_df)
    team_of = {rec["id"]: rec.get("team") for rec in pages.values()
               if rec.get("id") is not None}

    rounds = sorted({int(_num(r.get("round"), 0))
                     for rows in history.values() for r in rows
                     if _num(r.get("round")) is not None})
    ctx = {
        "pages": pages, "gameweek": gameweek, "history": history,
        "fixtures": fixtures_df, "team_of": team_of,
        "team_names": team_names or {}, "team_shorts": team_shorts or {},
        "recent_rounds": rounds[-FORM_WINDOW:],
        # Derived from the data rather than from the calendar or a mode string.
        # A missing gameweek_stats.csv and a season that hasn't started produce
        # the same empty history, and the answer to "can the six in-season
        # detectors find anything" is the same in both cases: no.
        "early": len(rounds) < EARLY_ROUNDS_MAX,
    }

    found = []
    for detector in DETECTORS:
        try:
            found.extend(detector(ctx) or [])
        except Exception:
            # One detector failing must not cost the night's post. There are
            # six, and any five of them still produce something to publish.
            continue
    found.sort(key=lambda c: -c["score"])
    return found


def choose(pages, gameweek, history_df=None, fixtures_df=None, team_names=None,
           team_shorts=None, recent_posts=(), today=None):
    """Tonight's subject, or None if nothing clears the bar.

    `recent_posts` is the ledger: an iterable of {code, angle, post_date}. Two
    rules come off it, and both exist because the failure mode of a generated
    daily post is not being wrong, it is being the same.

      * the same player cannot appear twice inside REPEAT_PLAYER_DAYS
      * the same angle cannot run two nights in a row

    Returning None is a legitimate outcome - preseason, an international break,
    a quiet week - and is better than publishing the least uninteresting player
    in the game under a headline saying he is worth a look.
    """
    today = today or date.today()
    recent_posts = list(recent_posts)

    blocked_codes = set()
    for post in recent_posts:
        try:
            when = date.fromisoformat(str(post["post_date"])[:10])
        except (KeyError, TypeError, ValueError):
            continue
        if (today - when).days < REPEAT_PLAYER_DAYS:
            blocked_codes.add(post.get("code"))

    # The ledger is newest first, so the most recent entry is last night's.
    last_angle = recent_posts[0].get("angle") if recent_posts else None

    for candidate in candidates(pages, gameweek, history_df, fixtures_df,
                                team_names, team_shorts):
        if candidate["code"] in blocked_codes:
            continue
        if candidate["angle"] == last_angle:
            continue
        return candidate
    return None


# ---------------------------------------------------------------------------
#  The prose
# ---------------------------------------------------------------------------

ANGLE_LABELS = {
    "injury_return": "Back from injury",
    "unlucky": "Due a return",
    "regression": "Running hot",
    "newly_nailed": "Now starting",
    "fixture_swing": "The fixtures turn",
    "unlucky_defence": "A defence due a clean sheet",
    "preseason_form": "Last season's numbers",
    "price_watch": "The market has moved",
    "opening_fixtures": "A kind opening run",
}


def _headline(rec, angle, code, day):
    name = rec.get("web_name") or rec.get("full_name")
    options = {
        "injury_return": [
            f"{name} is fit again — is he worth coming back in for?",
            f"{name} is available for the first time in weeks",
            f"What {name}'s return is actually worth",
        ],
        "unlucky": [
            f"{name} is creating plenty and scoring none of it",
            f"The numbers behind {name} say the returns are coming",
            f"{name} has been unlucky, and the underlying stats show it",
        ],
        "regression": [
            f"{name} is scoring more than the chances justify",
            f"How long can {name} keep this up?",
            f"{name} is outrunning his underlying numbers",
        ],
        "newly_nailed": [
            f"{name} has quietly become a starter",
            f"{name} is playing every week now",
            f"Nobody has noticed {name} is nailed on",
        ],
        "fixture_swing": [
            f"{name}'s fixtures have turned",
            f"The schedule opens up for {name}",
            f"{name} has the run of games to take advantage of",
        ],
        "unlucky_defence": [
            f"{name}'s defence is due a clean sheet",
            f"The numbers say {name} should have more clean sheets than this",
            f"{name} is at the back of a defence conceding more than it deserves",
        ],
        "preseason_form": [
            f"What {name} did last season, and whether to start with him",
            f"{name} scored at a real rate last year — is he worth a place?",
            f"The case for starting the season with {name}",
        ],
        "price_watch": [
            f"The market has already made its mind up about {name}",
            f"{name}'s price has moved before a ball has been kicked",
            f"Why so many managers are drafting {name}",
        ],
        "opening_fixtures": [
            f"{name} has the opening run to start the season with",
            f"The fixture list is kind to {name} early on",
            f"{name}'s first few gameweeks are as good as they get",
        ],
    }
    return variant(options.get(angle, [f"{name}"]), code, day, "headline")


def _paragraphs(rec, angle, evidence, gameweek, code, day):
    """The body, as a list of strings. Every clause restates a number that also
    appears in the stat table beside it."""
    name = rec.get("full_name") or rec.get("web_name")
    short = rec.get("web_name") or name
    club = rec.get("team_name") or "their club"
    price = f"£{rec['cost']}m" if rec.get("cost") else "an unlisted price"
    owned = _num(_stat(rec, "selected_by_percent"))
    pos_word = {"GK": "goalkeeper", "DEF": "defender", "MID": "midfielder",
                "FWD": "forward"}.get(rec.get("pos"), "player")

    opening = variant([
        f"{name} is a {club} {pos_word} at {price}",
        f"{name}, the {club} {pos_word}, is priced at {price}",
        f"At {price}, {name} is {club}'s {pos_word}",
    ], code, day, "opening")
    if owned is not None:
        opening += f", owned by {owned:.1f}% of managers"
    paras = [opening + "."]

    builder = {
        "injury_return": _injury_paragraphs,
        "unlucky": _underlying_paragraphs,
        "regression": _underlying_paragraphs,
        "newly_nailed": _nailed_paragraphs,
        "fixture_swing": _fixture_paragraphs,
        "unlucky_defence": _defence_paragraphs,
        "preseason_form": _preseason_form_paragraphs,
        "price_watch": _price_paragraphs,
        "opening_fixtures": _opening_paragraphs,
    }.get(angle)
    if builder:
        paras += builder(rec, angle, evidence, short, code, day)

    paras.append(_verdict(rec, angle, evidence, short, gameweek, code, day))
    return [p for p in paras if p]


def _injury_paragraphs(rec, angle, e, short, code, day):
    paras = []
    span = (f"gameweeks {e['out_from']} to {e['out_to']}"
            if e["out_from"] != e["out_to"] else f"gameweek {e['out_from']}")
    paras.append(
        f"{short} missed {e['rounds_out']} {_plural(e['rounds_out'], 'gameweek')} "
        f"({span}) and is listed as fit again.")

    returns = []
    if e["before_goals"]:
        returns.append(f"{e['before_goals']} {_plural(e['before_goals'], 'goal')}")
    if e["before_assists"]:
        returns.append(f"{e['before_assists']} {_plural(e['before_assists'], 'assist')}")
    line = (f"Across the {e['before_rounds']} "
            f"{_plural(e['before_rounds'], 'gameweek')} he last played, "
            f"{short} took {e['before_points']} points from "
            f"{e['before_minutes']} minutes — {e['before_points_per_90']} per 90")
    if returns:
        line += f", with {_join(returns)}"
    if e.get("before_xgi_per_90"):
        line += f", from {e['before_xgi_per_90']} expected goal involvements per 90"
    paras.append(line + ".")

    # The before-and-after the task asked for. Only printed when both halves
    # exist: one of them alone is a number with nothing to compare it to.
    with_him, without = e.get("team_conceded_with"), e.get("team_conceded_without")
    if with_him is not None and without is not None:
        if without > with_him:
            paras.append(
                f"The club conceded {with_him} goals a game in those matches "
                f"and {without} a game while he was out — a defence that got "
                f"worse without him, which is the part a clean-sheet bet turns on.")
        elif with_him > without:
            paras.append(
                f"The club conceded {with_him} goals a game in those matches "
                f"and {without} a game without him. The defence was not worse "
                f"for his absence, so the case for him is what he adds going "
                f"forward rather than at the back.")
        else:
            paras.append(
                f"The club conceded {with_him} goals a game both with him and "
                f"without, so his absence did not move the defence either way.")
    return paras


def _underlying_paragraphs(rec, angle, e, short, code, day):
    paras = []
    paras.append(
        f"Over the last {e['rounds']} {_plural(e['rounds'], 'gameweek')} "
        f"{short} has played {e['minutes']} minutes for {e['points']} points, "
        f"with {e['goals']} {_plural(e['goals'], 'goal')} and "
        f"{e['assists']} {_plural(e['assists'], 'assist')}.")

    if angle == "unlucky":
        paras.append(
            f"The chances behind that were worth more: {e['xgi']} expected goal "
            f"involvements against {e['returns']} actual, a gap of {e['gap']}. "
            f"That breaks down as {e['xg']} expected goals and {e['xa']} "
            f"expected assists.")
        paras.append(variant([
            "A gap that size usually closes, and it closes by the returns "
            "arriving rather than by the chances drying up.",
            "Expected-goal gaps are noisy over five games, but they are the "
            "closest thing in the data to a leading indicator.",
            "Nothing guarantees it corrects. What the number says is that the "
            "chances are being created, which is the part a player controls.",
        ], code, day, "unlucky"))
    else:
        paras.append(
            f"The chances behind that were worth less: {e['xgi']} expected goal "
            f"involvements against {e['returns']} actual, a gap of {e['gap']} in "
            f"the other direction. That breaks down as {e['xg']} expected goals "
            f"and {e['xa']} expected assists.")
        paras.append(variant([
            "Finishing at that rate is not usually sustained, so the price and "
            "the ownership are likely ahead of the underlying player.",
            "That is a run rather than a level. It can continue — but the "
            "chances being created do not currently support it.",
            "Worth holding while it lasts, worth not paying up for.",
        ], code, day, "regression"))
    return paras


def _nailed_paragraphs(rec, angle, e, short, code, day):
    return [
        f"{short} has started {e['recent_starts']} of the last "
        f"{e['recent_rounds']} gameweeks for {e['recent_minutes']} minutes, "
        f"having started {e['prior_starts']} of the {e['prior_rounds']} before "
        f"them for {e['prior_minutes']}.",
        f"Those {e['recent_rounds']} starts have produced {e['recent_points']} "
        f"points, with {e['recent_goals']} {_plural(e['recent_goals'], 'goal')}, "
        f"{e['recent_assists']} {_plural(e['recent_assists'], 'assist')} and "
        f"{e['recent_xgi']} expected goal involvements.",
        variant([
            "Minutes are the most reliable thing in FPL and the slowest to be "
            "priced in — ownership moves on goals, not on team sheets.",
            "A player who has just become a starter is usually cheaper than he "
            "will be in a month, because the market is still pricing the "
            "substitute.",
            "The returns may or may not follow. The minutes are the part that "
            "has already changed.",
        ], code, day, "nailed"),
    ]


def _fixture_paragraphs(rec, angle, e, short, code, day):
    games = ", ".join(
        f"{g['opponent']} ({'H' if g['at_home'] else 'A'}, {g['difficulty']:.0f})"
        for g in e.get("upcoming", [])[:5])
    paras = [
        f"{e['team_name']}'s last five games rated {e['past_difficulty']} on "
        f"FPL's own difficulty scale. The next five rate "
        f"{e['future_difficulty']} — a swing of {e['swing']} towards the easier "
        f"end.",
    ]
    if games:
        paras.append(f"Those games: {games}. The number in brackets is FPL's "
                     f"difficulty rating, where 5 is hardest.")
    paras.append(variant([
        "Fixture difficulty is a blunt instrument and it is not a projection. "
        "It is a reasonable way to decide which of two similar players to own.",
        "A run of easier games is worth something on its own, and worth more "
        "when it lands on a player who is already playing well.",
        "The rating barely moves during a season, so a swing this size is the "
        "schedule changing rather than the clubs.",
    ], code, day, "fixtures"))
    return paras


def _defence_paragraphs(rec, angle, e, short, code, day):
    return [
        f"{e['team_name']} have conceded {e['conceded']} goals in the last "
        f"{e['rounds']} gameweeks from {e['expected_conceded']} expected — "
        f"{e['conceded_per_game']} a game against {e['expected_per_game']} "
        f"the chances were worth.",
        variant([
            "A defence conceding well above its expected goals conceded is "
            "usually one that has faced good finishing rather than good "
            "chances, and that does not repeat.",
            "The gap between those two numbers is the part that tends to "
            "close, and it closes towards clean sheets.",
            "Expected goals conceded is a better guide to a defence than goals "
            "conceded, precisely because it ignores how well the other side "
            "finished.",
        ], code, day, "defence"),
    ]


def _preseason_form_paragraphs(rec, angle, e, short, code, day):
    line = (f"Last season {short} played {e['prev_minutes']} minutes and scored "
            f"{e['prev_goals']} {_plural(e['prev_goals'], 'goal')} — "
            f"{e['goals_per_90']} per 90")
    if e.get("xg_per_90") is not None:
        line += (f", from {e['prev_xg']} expected goals, or {e['xg_per_90']} "
                 f"per 90")
    paras = [line + "."]

    # The one comparison worth making from these two numbers, and the reason
    # xG is fetched at all: whether the rate came from the chances or from the
    # finishing. Only drawn when both halves exist.
    per_90, xg_90 = e.get("goals_per_90"), e.get("xg_per_90")
    if per_90 is not None and xg_90 is not None:
        if per_90 > xg_90 + 0.10:
            paras.append(
                "He scored more than the chances were worth, which is either "
                "finishing the numbers do not capture or a rate that was never "
                "going to hold. A year on, it is worth treating as the second "
                "until he shows otherwise.")
        elif xg_90 > per_90 + 0.10:
            paras.append(
                "The chances behind that were worth more than he took, so the "
                "rate above is the floor of what he was doing rather than the "
                "ceiling.")
        else:
            paras.append(
                "The goals and the chances behind them agree, which is the "
                "boring answer and the one most likely to repeat.")

    paras.append(variant([
        "None of this is from this season, because there is no this season "
        "yet. It is the best evidence available in August, and it is evidence "
        "about a player in a squad that may have changed around him.",
        "A full season of minutes is the largest sample this site holds on "
        "anyone. It is also twelve months old, and says nothing about a new "
        "manager, a new role or a summer signing ahead of him.",
        "Last year's rate is where everyone starts in August. It is worth "
        "less than three games of this season will be, and those are the "
        "games that have not happened.",
    ], code, day, "preseason"))
    return paras


def _price_paragraphs(rec, angle, e, short, code, day):
    change = abs(e["price_change"])
    paras = [
        f"{short} has {e['direction']} £{change}m since FPL published this "
        f"season's prices, and now costs £{e['cost']}m."
    ]
    if e.get("transfers_in") is not None:
        paras.append(
            f"That move came from {e['transfers_in']:,} transfers in across "
            f"the game — price changes here are driven by how many managers "
            f"buy a player, not by how well he is playing.")

    if e["direction"] == "risen":
        paras.append(variant([
            "A preseason riser is a consensus pick. That is worth knowing in "
            "both directions: the crowd is usually right about who is good, "
            "and owning what everyone owns is how you finish where everyone "
            "finishes.",
            "The market has decided before anyone has played. Whether to "
            "follow it depends on whether you want the points or the rank — "
            "a template player gives you the first and not the second.",
            "Rising prices in August reflect popularity rather than form, "
            "because there is no form. Worth checking the projection below "
            "against the crowd's enthusiasm.",
        ], code, day, "price"))
    else:
        paras.append(variant([
            "A preseason faller is a player the crowd has quietly moved off, "
            "usually on news rather than on numbers. Sometimes that news is "
            "real and sometimes it is a rumour that never happened.",
            "Falling prices in August mean managers are selling, which is not "
            "the same as a player getting worse. It does mean he is cheaper "
            "than he was, and cheaper than the crowd once thought he was worth.",
            "The market has cooled on him. That is worth a look precisely "
            "because everyone else has stopped looking.",
        ], code, day, "price"))
    return paras


def _opening_paragraphs(rec, angle, e, short, code, day):
    games = ", ".join(
        f"{g['opponent']} ({'H' if g['at_home'] else 'A'}, {g['difficulty']:.0f})"
        for g in e.get("upcoming", [])[:5])
    paras = [
        f"{e['team_name']}'s next {e['games']} "
        f"{_plural(e['games'], 'game')} rate {e['future_difficulty']} on FPL's "
        f"own difficulty scale, where 3 is about average and 5 is hardest.",
    ]
    if games:
        paras.append(f"Those games: {games}.")
    paras.append(variant([
        "An easy opening run is the one thing about the season that is known "
        "in advance. It is also the thing every other manager can see, so it "
        "is a tie-breaker between similar players rather than a reason on "
        "its own.",
        "Fixture difficulty barely moves across a season and it is not a "
        "projection. What it is good for is deciding which of two players you "
        "were already weighing up to start with.",
        "Starting the season with kind fixtures is worth real points, and it "
        "is worth them early, when a bad start is hardest to make back.",
    ], code, day, "opening"))
    return paras


def _verdict(rec, angle, e, short, gameweek, code, day):
    """The explicit answer. The task asked for "whether picking him is a good
    choice", and a write-up that lays out five paragraphs of evidence and then
    declines to conclude is the most annoying possible version of this page."""
    predicted = e.get("predicted")
    owned = _num(_stat(rec, "selected_by_percent"))
    price = f"£{rec['cost']}m" if rec.get("cost") else None

    if predicted is None:
        return (f"There is no projection for {short} in gameweek {gameweek}, "
                "so this is a case worth watching rather than acting on.")

    bits = [f"The model projects {predicted} points for {short} in gameweek "
            f"{gameweek}"]
    if price:
        bits.append(f"at {price}")
    if owned is not None:
        bits.append(f"and {owned:.1f}% ownership")
    line = " ".join(bits) + ". "

    # The verdict is drawn from the projection and the ownership together,
    # because they answer different questions - one is "will he score", the
    # other is "does owning him move my rank".
    if predicted >= 5.0 and (owned or 0) < 10:
        line += variant([
            "That is a strong projection at an ownership almost nobody has, "
            "which is the combination worth acting on.",
            "Well rated and barely owned — the case for is about as clean as "
            "this site's numbers get.",
        ], code, day, "verdict")
    elif predicted >= 5.0:
        line += variant([
            "The projection is strong, but enough managers already own him "
            "that this is about keeping up rather than gaining ground.",
            "A solid pick rather than a differential — the points are there, "
            "the rank gain isn't.",
        ], code, day, "verdict")
    elif predicted >= 3.5:
        line += variant([
            "That is a reasonable return rather than a compelling one, so this "
            "is a hold or a watch rather than a transfer worth taking a hit for.",
            "Fine as a squad player. Not worth a points hit on this evidence.",
        ], code, day, "verdict")
    else:
        line += variant([
            "The model does not rate the coming gameweek highly, so the case "
            "here is about the weeks after it rather than the next one.",
            "The underlying case is better than the immediate projection, "
            "which makes this one to watch rather than to buy this week.",
        ], code, day, "verdict")
    return line


# ---------------------------------------------------------------------------
#  Assembly
# ---------------------------------------------------------------------------

def build(candidate, pages, gameweek, today=None, season_label=None):
    """The whole post, as the JSON-serialisable dict stored in `player_post`.

    Everything the page and the drafts need is copied in, because the row is
    read back weeks later by an admin looking at what was posted and must not
    depend on a pipeline that has moved on.
    """
    today = today or date.today()
    day = today.isoformat()
    code = candidate["code"]
    rec = pages[code]
    angle = candidate["angle"]
    evidence = candidate["evidence"]

    return {
        "date": day,
        "gameweek": int(gameweek),
        "season": season_label,
        "angle": angle,
        "angle_label": ANGLE_LABELS.get(angle, angle),
        "score": round(float(candidate["score"]), 2),
        "code": code,
        "name": rec.get("web_name") or rec.get("full_name"),
        "full_name": rec.get("full_name"),
        "path": rec.get("path"),
        "pos": rec.get("pos"),
        "team_name": rec.get("team_name") or "",
        "team_code": rec.get("team_code"),
        "cost": rec.get("cost"),
        "owned": _num(_stat(rec, "selected_by_percent")),
        "predicted": evidence.get("predicted"),
        "headline": _headline(rec, angle, code, day),
        "paragraphs": _paragraphs(rec, angle, evidence, gameweek, code, day),
        "stats": _stat_rows(angle, evidence),
        "evidence": evidence,
    }


def _stat_rows(angle, e):
    """The evidence as label/value pairs, for the table beside the prose.

    Explicit per angle rather than dumping the evidence dict, because the
    evidence carries working values the reader has no use for and the order
    matters - the first two rows should be the ones the argument rests on.
    """
    rows = {
        "injury_return": [
            ("Gameweeks missed", e.get("rounds_out")),
            ("Points per 90 before", e.get("before_points_per_90")),
            ("Minutes before", e.get("before_minutes")),
            ("Club conceded per game, with him", e.get("team_conceded_with")),
            ("Club conceded per game, without", e.get("team_conceded_without")),
        ],
        "unlucky": [
            ("Expected goal involvements", e.get("xgi")),
            ("Actual returns", e.get("returns")),
            ("Gap", e.get("gap")),
            ("Expected goals", e.get("xg")),
            ("Expected assists", e.get("xa")),
            ("Minutes", e.get("minutes")),
        ],
        "regression": [
            ("Actual returns", e.get("returns")),
            ("Expected goal involvements", e.get("xgi")),
            ("Gap", e.get("gap")),
            ("Expected goals", e.get("xg")),
            ("Expected assists", e.get("xa")),
            ("Minutes", e.get("minutes")),
        ],
        "newly_nailed": [
            ("Starts, last 3", e.get("recent_starts")),
            ("Starts, 3 before", e.get("prior_starts")),
            ("Minutes, last 3", e.get("recent_minutes")),
            ("Minutes, 3 before", e.get("prior_minutes")),
            ("Points, last 3", e.get("recent_points")),
        ],
        "fixture_swing": [
            ("Difficulty, last 5", e.get("past_difficulty")),
            ("Difficulty, next 5", e.get("future_difficulty")),
            ("Swing", e.get("swing")),
        ],
        "unlucky_defence": [
            ("Goals conceded per game", e.get("conceded_per_game")),
            ("Expected per game", e.get("expected_per_game")),
            ("Gap per game", e.get("gap_per_game")),
            ("Gameweeks", e.get("rounds")),
        ],
        # Every label in the four below says "last season" or names the thing
        # as a price. The table is read on its own, without the prose beside
        # it, and a bare "Goals per 90" in August would be read as this season's.
        "preseason_form": [
            ("Goals per 90, last season", e.get("goals_per_90")),
            ("Expected goals per 90, last season", e.get("xg_per_90")),
            ("Goals, last season", e.get("prev_goals")),
            ("Minutes, last season", e.get("prev_minutes")),
        ],
        "price_watch": [
            ("Price change since launch", e.get("price_change")),
            ("Current price", e.get("cost")),
            ("Transfers in", e.get("transfers_in")),
            ("Ownership", e.get("owned")),
        ],
        "opening_fixtures": [
            ("Difficulty, next games", e.get("future_difficulty")),
            ("Games rated", e.get("games")),
        ],
    }.get(angle, [])
    return [{"label": label, "value": value}
            for label, value in rows if value is not None]
