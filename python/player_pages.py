"""
Per-player pages.

One indexable page per player in the game, built from the already-rated pool so
no extra model run or API call is needed to render one.

Two decisions worth knowing about before changing anything here:

**URLs are keyed on `code`, not `id`.** FPL reassigns `id` every summer. A URL
built on it would come back in August pointing at a different footballer -
hundreds of pages silently about the wrong person, which is worse than not
having the pages at all. `code` is stable for as long as a player is in the
game, so /player/mohamed-salah-118748 means the same player next season and the
season after.

**The page is prose, not just a table.** A grid of numbers gives a search engine
almost nothing to match a query against - nobody types "Salah 8.1 rating 14.5m".
The sentences in `_describe()` are generated from the same numbers shown in the
table, which is the part that can actually rank for "is Salah worth it fpl" and
the several hundred similar questions people really ask.
"""

import re
import unicodedata

from team_service import (POS_SHORT, PREV_SEASON_MIN_MINUTES, _num,
                          _prev_season_stats_by_code)

POS_NAME = {"GK": "goalkeeper", "DEF": "defender", "MID": "midfielder", "FWD": "forward"}

# Characters that NFKD won't decompose, because they're distinct letters rather
# than a letter plus an accent. Without these, "Gross" comes out as "Gro" and
# "Hojlund" as "Hjlund".
_LETTER_MAP = str.maketrans({
    "ß": "ss", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d", "Ð": "D", "ð": "d",
    "Þ": "Th", "þ": "th", "Ł": "L", "ł": "l", "Æ": "Ae", "æ": "ae",
    "Œ": "Oe", "œ": "oe", "İ": "I", "ı": "i",
})

# Season-total columns lifted straight from the FPL bootstrap. Anything absent
# for a given player simply doesn't render - the columns FPL publishes change
# between seasons and a missing one shouldn't take the page down.
_STAT_COLS = [
    "total_points", "points_per_game", "minutes", "starts", "goals_scored",
    "assists", "clean_sheets", "goals_conceded", "own_goals", "saves",
    "penalties_saved", "penalties_missed", "yellow_cards", "red_cards",
    "bonus", "bps", "expected_goals", "expected_assists",
    "expected_goal_involvements", "expected_goals_conceded",
    "influence", "creativity", "threat", "ict_index",
    "selected_by_percent", "form", "value_season",
    # Market movement rather than performance, and the only two columns here
    # that say anything at all before a ball is kicked: `cost_change_start` is
    # price drift since FPL published the season's prices, and `transfers_in`
    # is cumulative from the same moment. player_spotlight's early-season
    # angles are built on them - every other column in this list is zero until
    # the season starts.
    "cost_change_start", "transfers_in",
]


def slugify(text):
    """Accent-folded, lowercase, hyphen-separated - safe in a URL and readable.

    Deliberately lossy: "Guehi" and "Guéhi" produce the same slug. That's fine,
    because the code on the end of the URL is what actually identifies the
    player; the slug is there for humans and for the search keywords it puts in
    the path."""
    text = (text or "").translate(_LETTER_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text)
    return text.strip("-").lower()


def _full_name(row):
    name = " ".join(x for x in [(row.get("first_name") or "").strip(),
                                (row.get("second_name") or "").strip()] if x)
    return name or (row.get("web_name") or "").strip() or "Unknown player"


def _fmt(n, dp=0):
    """Numbers for prose: no trailing '.0', thousands separated.

    NaN is treated as absent, the same as None. A missing stat arrives as NaN
    far more often than as None - it is what pandas puts in an empty cell - and
    int(round(nan)) raises while f"{nan:.1f}" quietly writes the word "nan"
    into the sentence. Both are worse than the caller's own "no value" path.
    """
    if n is None or n != n:
        return None
    if dp == 0:
        return f"{int(round(n)):,}"
    return f"{n:.{dp}f}".rstrip("0").rstrip(".")


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _article(word):
    """"a" or "an". Crude - it goes on the letter, not the sound - but the only
    club names it sees are Premier League ones, where the letter is right every
    time (an Arsenal, an Aston Villa, an Everton, an Ipswich)."""
    # `in` on an empty string is True for any haystack, so a blank club name
    # produced "an" without the check on the left.
    first = (word or "")[:1].upper()
    return "an" if first and first in "AEIOU" else "a"


def _short(rec):
    """What to call a player after the first mention.

    Not the last word of the full name: that files David Raya Martin under
    "Martin" and Kepa Arrizabalaga Revuelta under "Revuelta", neither of which
    is what anyone calls them. `web_name` is FPL's own display name and is
    exactly this - Raya, Kepa, Chadi Riad, Richarlison."""
    return rec.get("web_name") or rec["full_name"].split()[-1]


def build_index(position_dfs):
    """code -> a complete page record. Built once per data load, not per request.

    Returns {} when there's no rated pool yet, so a request during startup 404s
    rather than raising."""
    if not position_dfs:
        return {}

    prev = _prev_season_stats_by_code()
    pages = {}

    for pos_name, df in position_dfs.items():
        for _, row in df.iterrows():
            code = row.get("code")
            if code is None or code != code:      # NaN
                continue
            code = int(code)
            pos = POS_SHORT.get(int(row["element_type"]), "?")
            full_name = _full_name(row)
            slug = f"{slugify(full_name)}-{code}"

            stats = {}
            for col in _STAT_COLS:
                v = _num(row.get(col))
                if v is not None and v == v:
                    stats[col] = v

            next_gws = row.get("next_gameweeks")
            next_gws = next_gws if isinstance(next_gws, list) else []

            rec = {
                "code": code,
                "id": int(row["id"]),
                "web_name": (row.get("web_name") or "").strip(),
                "full_name": full_name,
                "slug": slug,
                "path": f"/player/{slug}",
                "pos": pos,
                "pos_name": POS_NAME.get(pos, "player"),
                "position": pos_name,
                "team": int(row["team"]) if row.get("team") == row.get("team") else None,
                "team_code": int(row["team_code"]) if row.get("team_code") == row.get("team_code") else None,
                "cost": round(float(row["now_cost"]) / 10, 1) if _num(row.get("now_cost")) else None,
                "rating": round(float(row["rating"]), 1) if _num(row.get("rating")) is not None else None,
                "status": row.get("status", "a") or "a",
                "news": (row.get("news") or "").strip(),
                "chance_of_playing_next_round": _num(row.get("chance_of_playing_next_round")),
                "next_gameweeks": next_gws,
                "stats": stats,
                # Carried whole rather than pre-filtered, because two readers
                # want different things from it: the page prints it beside this
                # season only for players who actually played, while the prose
                # below still uses a thin one to explain an empty current
                # season. Eligibility is applied where it is displayed.
                "prev_season": prev.get(code),
            }
            pages[code] = rec

    return pages


def attach_team_names(pages, team_names, team_shorts):
    """Fill in club names. Kept separate from build_index so the team lookup -
    the one part that can hit the network - can fail without costing us the
    pages."""
    for rec in pages.values():
        rec["team_name"] = team_names.get(rec["team"]) or team_shorts.get(rec["team"]) or "their club"
        rec["team_short"] = team_shorts.get(rec["team"]) or ""
    return pages


# ---------------------------------------------------------------------------
#  Prose
# ---------------------------------------------------------------------------

def horizon_points(rec, n=8):
    """Total predicted points over the next n gameweeks."""
    pts = [g.get("points") for g in (rec.get("next_gameweeks") or [])[:n]
           if g.get("points") is not None]
    return round(sum(pts), 1) if pts else None


def describe(rec, season_label, stats_season=None, season_started=True):
    """The page's paragraphs, as a list of strings.

    `season_started` is the important argument. Before the first deadline FPL's
    bootstrap still carries LAST season's totals, so a page that prints them
    under this season's heading is simply wrong on every player at once - and it
    stays wrong for the whole of the summer, which is exactly when a new site is
    trying to get itself indexed. When the season hasn't started, the same
    numbers are attributed to `stats_season` instead.

    (Once a rollover has happened and the totals really are zeroed, the
    minutes == 0 branch takes over and says the season hasn't started rather
    than reporting a player as having scored nothing.)"""
    s = rec["stats"]
    name = rec["full_name"]
    short = _short(rec)
    club = rec.get("team_name", "their club")
    paras = []

    # Deliberately no pronouns anywhere below. These are real people whose
    # pronouns this project has no source for, and the surname reads better in a
    # stats context anyway.

    # --- who, where, how much ---
    price = f"£{rec['cost']}m" if rec["cost"] else "an unlisted price"
    owned = s.get("selected_by_percent")
    line = (f"{name} is {_article(club)} {club} {rec['pos_name']} in Fantasy "
            f"Premier League {season_label}, priced at {price}")
    if owned is not None:
        line += f" and selected by {_fmt(owned, 1)}% of managers"
    paras.append(line + ".")

    # --- the returns, attributed to the season they actually belong to ---
    minutes = s.get("minutes") or 0
    stats_season = stats_season or season_label
    if minutes > 0:
        pts = s.get("total_points") or 0
        avg = (f", an average of {_fmt(s['points_per_game'], 1)} per game"
               if s.get("points_per_game") else "")
        if season_started:
            paras.append(f"{short} has scored {_fmt(pts)} {_plural(pts, 'point')} "
                         f"from {_fmt(minutes)} minutes{avg}.")
        else:
            paras.append(f"The {season_label} season hasn't kicked off yet. In "
                         f"{stats_season}, {short} scored {_fmt(pts)} "
                         f"{_plural(pts, 'point')} from {_fmt(minutes)} "
                         f"minutes{avg}.")

        returns = []
        for col, word in (("goals_scored", "goal"), ("assists", "assist"),
                          ("clean_sheets", "clean sheet")):
            v = s.get(col)
            if v:
                returns.append(f"{_fmt(v)} {_plural(v, word)}")
        if s.get("bonus"):
            returns.append(f"{_fmt(s['bonus'])} bonus {_plural(s['bonus'], 'point')}")
        if returns:
            paras.append(("That breaks down as " if season_started
                          else "That was ") + _join(returns) + ".")

        # A gameweek or two into a season, this season's totals are a sample
        # too small to say anything, and the page previously offered no
        # context for them at all - last season was mentioned only when there
        # was nothing else to print. This is the sentence that makes a
        # two-point return readable.
        prev = rec.get("prev_season") or {}
        prev_minutes = prev.get("minutes") or 0
        if season_started and prev_minutes >= PREV_SEASON_MIN_MINUTES:
            prev_points = prev.get("total_points") or 0
            ppg = prev.get("points_per_game")
            rate = f", {_fmt(ppg, 1)} per game" if ppg else ""
            paras.append(
                f"For comparison, {short} scored {_fmt(prev_points)} "
                f"{_plural(prev_points, 'point')} from {_fmt(prev_minutes)} "
                f"minutes last season{rate}.")
    else:
        prev = rec.get("prev_season") or {}
        if prev.get("minutes"):
            goals = prev.get("goals_scored") or 0
            paras.append(
                f"The season hasn't started yet, so {name} has no {season_label} "
                f"returns to show. Last season: {_fmt(prev['minutes'])} minutes "
                f"and {_fmt(goals)} {_plural(goals, 'goal')}.")
        else:
            paras.append(f"The season hasn't started yet, so there are no "
                         f"{season_label} returns for {name} to show.")

    # --- underlying numbers ---
    # Skipped when both are effectively nil. Every goalkeeper and most defenders
    # sit at 0.00 xG, and "underneath that sit 0 expected goals" is a sentence
    # that tells a reader nothing while making the page look auto-generated.
    xg, xa = s.get("expected_goals"), s.get("expected_assists")
    if minutes > 0 and ((xg or 0) >= 0.5 or (xa or 0) >= 0.5):
        under = []
        if xg:
            under.append(f"{_fmt(xg, 2)} expected goals")
        if xa:
            under.append(f"{_fmt(xa, 2)} expected assists")
        line = (f"Underneath that {'sit' if season_started else 'sat'} "
                + _join(under))
        goals = s.get("goals_scored")
        if xg and goals is not None:
            diff = goals - xg
            if abs(diff) >= 1:
                line += (f" — {_fmt(abs(diff), 1)} "
                         f"{'ahead of' if diff > 0 else 'behind'} the expected-goals "
                         f"figure, which is the kind of gap that tends to close")
        paras.append(line + ".")

    # --- what this site thinks ---
    rating = rec.get("rating")
    nxt = rec["next_gameweeks"][0].get("points") if rec["next_gameweeks"] else None
    horizon = horizon_points(rec)
    if rating is not None or nxt is not None:
        bits = []
        if rating is not None:
            bits.append(f"rates {short} {_fmt(rating)} out of 100")
        if nxt is not None:
            bits.append(f"projects {_fmt(nxt, 1)} points in the next gameweek")
        if horizon is not None:
            bits.append(f"{_fmt(horizon, 1)} over the next "
                        f"{len(rec['next_gameweeks'][:8])} gameweeks")
        paras.append("FPL Buddy " + _join(bits) + ".")

    # --- availability ---
    paras.append(availability_sentence(rec))

    # --- fixtures ---
    fx = [g for g in rec["next_gameweeks"][:3] if g.get("opponent")]
    if fx:
        paras.append(f"Next up: {_join([fixture_label(g) for g in fx])}.")

    return paras


def fixture_label(g):
    """'ARS (H)'. `was_home` can legitimately be None for a fixture FPL hasn't
    finalised, in which case the venue is left off rather than guessed."""
    opp = g.get("opponent") or "TBC"
    home = g.get("was_home")
    if home is None:
        return str(opp)
    return f"{opp} ({'H' if home else 'A'})"


def availability_sentence(rec):
    """Plain English for the status flag. Never invents a percentage: when FPL
    publishes none, the status letter is all that's actually known."""
    status = (rec.get("status") or "a").lower()
    chance = rec.get("chance_of_playing_next_round")
    # NaN is FPL publishing nothing, same as None - see team_service._num. Left
    # as NaN it fails the `is None` test below and then survives the clamp,
    # because min(100, nan) returns 100: every fit player's page would state a
    # 100% chance FPL never actually published.
    if chance is not None and chance != chance:
        chance = None
    name = _short(rec)
    news = rec.get("news")

    words = {"i": "injured", "s": "suspended", "u": "unavailable",
             "n": "not in the squad", "d": "a doubt"}

    if status == "a" and chance is None:
        return f"{name} is fit and available."
    if chance is not None:
        pct = int(max(0, min(100, chance)))
        line = f"FPL lists {name} at a {pct}% chance of playing the next gameweek"
        if news:
            line += f" — {news.rstrip('.')}"
        return line + "."
    label = words.get(status, "flagged")
    line = f"{name} is currently {label}"
    if news:
        line += f" — {news.rstrip('.')}"
    return line + "."


def _join(items):
    """'a', 'a and b', 'a, b and c'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def meta_for(rec, season_label, stats_season=None, season_started=True):
    """<title> and <meta name="description"> for one player.

    The description is built from real numbers rather than a template with the
    name swapped in, because a few hundred near-identical descriptions are what
    "thin content" means to a search engine."""
    s = rec["stats"]
    name = rec["full_name"]
    club = rec.get("team_name", "")
    price = f"£{rec['cost']}m" if rec["cost"] else None

    # Kept under ~65 characters, which is roughly where search engines stop
    # rendering a title, so the suffix is short and the name leads.
    #
    # No "| FPL Buddy" here, unlike the fixed pages in main.py. On several
    # hundred pages that's 13 characters of brand spent before the useful words
    # on every one of them, and both Google and Bing append the site name to the
    # result themselves. The full name is used rather than `_short()` because
    # web_name collides - there is more than one Sánchez - and colliding titles
    # across hundreds of pages is a worse problem than a long one.
    title = f"{name} — FPL {season_label} stats, price and points"

    bits = [f"{name}, {club} {rec['pos_name']}".strip().rstrip(",")]
    if price:
        bits.append(f"{price} in Fantasy Premier League")
    if s.get("selected_by_percent") is not None:
        bits.append(f"owned by {_fmt(s['selected_by_percent'], 1)}%")
    if s.get("minutes"):
        when = "this season" if season_started else f"in {stats_season or season_label}"
        bits.append(f"{_fmt(s.get('total_points') or 0)} points {when}")
    horizon = horizon_points(rec)
    if horizon is not None:
        bits.append(f"{_fmt(horizon, 1)} points projected over the next "
                    f"{len(rec['next_gameweeks'][:8])} gameweeks")
    description = ". ".join([_join(bits)]) + ". Ratings, form, underlying numbers and fixtures."

    return {"title": title, "description": description[:320]}


def a_to_z(pages):
    """Players grouped by initial, for the index page.

    Grouped on `web_name`, not the last word of the full name: David Raya Martin
    filed under M is a list nobody can use. FPL's display name is the name a
    reader is looking for.

    An index page matters more than it looks: the sitemap gets a page crawled,
    but an internal link is what says the page is part of the site. Without one,
    several hundred player pages are orphans."""
    def sort_key(r):
        return (slugify(_short(r)), r["full_name"].lower())

    groups = {}
    for rec in sorted(pages.values(), key=sort_key):
        letter = slugify(_short(rec))[:1].upper() or "#"
        groups.setdefault(letter, []).append(rec)
    return [{"letter": k, "players": v} for k, v in sorted(groups.items())]


# How bad each FPL status flag is, worst first. Drives the order of the injury
# list, because "is he playing" is asked about the players least likely to be.
#
# 'd' (a doubt) sits last deliberately: it is the only flag that can still turn
# into a start, so it is the one worth reading after the settled bad news.
_STATUS_RANK = {"i": 0, "s": 1, "u": 2, "n": 3, "d": 4}

_STATUS_LABEL = {
    "i": "Injured", "s": "Suspended", "u": "Unavailable",
    "n": "Not in squad", "d": "Doubtful",
}


def injury_list(pages):
    """Every flagged player, worst news first, then by ownership.

    Ownership second because this page answers a question people ask about
    players they own or are considering: a 40%-owned forward who is a doubt
    matters to far more readers than a 0.1%-owned one who is out for the
    season, and sorting purely by severity would bury him.

    Who counts as flagged is the part worth getting right, and the obvious
    rule is wrong. "status is not 'a', OR a percentage exists" pulls in every
    player FPL has explicitly stamped 100% - Endo, Carvalho, Wataru Endo at
    full fitness - and files them on an injury page under a label implying
    something is wrong. FPL publishes that 100% to say "this one is FINE",
    which is the opposite of what this page is for.

    So: flagged status always counts, and an otherwise-available player counts
    only when the published percentage is BELOW 100. That keeps the case worth
    keeping - FPL sometimes carries a 75% against a player it has not given a
    status letter - and drops the confirmations of fitness.
    """
    rows = []
    for rec in pages.values():
        status = (rec.get("status") or "a").lower()
        chance = rec.get("chance_of_playing_next_round")
        # NaN never equals itself. Left in, min(100, nan) returns 100 later and
        # the page invents a certainty FPL never published - the same trap
        # availability_sentence documents.
        if chance is not None and chance != chance:
            chance = None
        if status == "a" and (chance is None or chance >= 100):
            continue
        owned = (rec.get("stats") or {}).get("selected_by_percent") or 0
        rows.append({
            **rec,
            # A player who reaches here on a sub-100 percentage alone has no
            # status letter to label him with. "Doubtful" is what that is, and
            # it is what FPL's own site calls it - "Flagged" named the
            # mechanism rather than the situation.
            "status_label": _STATUS_LABEL.get(status, "Doubtful"),
            "chance": None if chance is None else int(max(0, min(100, chance))),
            "owned": round(float(owned), 1),
            "sentence": availability_sentence(rec),
        })
    rows.sort(key=lambda r: (_STATUS_RANK.get((r.get("status") or "a").lower(), 9),
                             -r["owned"]))
    return rows
