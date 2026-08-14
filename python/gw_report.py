"""The weekly gameweek report - the newspaper page at /gameweek/<n>.

Four sections, all derived from data the site already computes: who is in
form, who is a differential, whose fixtures turn, and who is hurt. Nothing
here fetches anything; it takes the page index that `player_pages.build_index`
already produces and the rotation table `fixture_rotator` already builds, and
decides what is worth printing.

The prose is templated from thresholds rather than written freely. Every
sentence this module emits is a restatement of a number that is also displayed
next to it, which means the page cannot say something the data doesn't support
- the failure mode that matters when a page publishes itself nightly with
nobody reading it first.

Editions are frozen at the deadline (see `db.save_gw_report`), so this module
is only ever building the *current* one. It never reconstructs a past week.
"""

import math
from datetime import datetime, timezone

import player_pages

# A player needs to have actually played to be judged on form. Without this the
# in-form list fills with squad players whose two substitute appearances
# produced one goal and a form figure nobody should act on.
MIN_MINUTES_FOR_FORM = 180

# Ownership below which a pick counts as a differential. 5% is the usual line
# in FPL discussion, and it's roughly where "my rank moves if he returns"
# starts being true - above about 10% you're mostly just keeping up.
DIFFERENTIAL_OWNERSHIP = 5.0

# A differential is only interesting if the model actually rates him. Without a
# floor this section becomes a list of cheap players nobody owns for the good
# reason that they are not very good.
DIFFERENTIAL_MIN_PREDICTED = 3.0

# Ownership above which an injury is news rather than trivia. A 2%-owned player
# picking up a knock is not something most readers need to act on.
NEWS_MIN_OWNERSHIP = 8.0

# How many gameweeks of fixtures the fixture section judges a run over. Matches
# the rotation planner's own horizon so the two pages can't disagree.
FIXTURE_HORIZON = 5

SECTION_SIZE = 3


def _stat(rec, key, default=None):
    """A stat from the record, or `default`. Values can be missing entirely -
    FPL changes its published columns between seasons - and can be NaN, which
    is not the same thing and does not compare usefully."""
    v = (rec.get("stats") or {}).get(key, default)
    if isinstance(v, float) and math.isnan(v):
        return default
    return v


def _predicted_for(rec, gameweek):
    """This player's projection for `gameweek` specifically.

    Matching on `event` rather than taking next_gameweeks[0] is deliberate and
    is the bug the AI squads already had to fix once: that list is built from
    unfinished fixtures, so its first entry silently rolls forward the moment a
    round completes. A report generated late on a Sunday would otherwise print
    next week's numbers under this week's headline.

    The key is `points` - that is what `rating_model` writes into these
    entries. `predicted_points` is the name the same number goes by in the
    per-player API payload, and reading for it here returns None for every
    player: the sections don't error, they just come back empty, which looks
    exactly like a quiet week."""
    for g in rec.get("next_gameweeks") or []:
        if g.get("event") == gameweek:
            v = g.get("points", g.get("predicted_points"))
            return float(v) if v is not None else None
    return None


def _fixtures_from(rec, gameweek, n=3):
    """The next `n` fixtures from `gameweek` onward, as display labels."""
    out = []
    for g in rec.get("next_gameweeks") or []:
        if g.get("event") is not None and g["event"] >= gameweek:
            out.append({"event": int(g["event"]),
                        "label": player_pages.fixture_label(g)})
    return out[:n]


def _is_available(rec):
    """Fit enough to be worth recommending.

    A flagged player is excluded from the three "buy him" sections outright
    rather than discounted: this page is read by people deciding transfers, and
    a recommendation carrying an asterisk is worse than no recommendation. The
    injury section is where flagged players belong.

    No published chance means fit, not doubtful. FPL only fills that field in
    when it has something to say, so it is null for the great majority of the
    pool - and NaN rather than None whenever the record came through pandas.
    Both have to read as "nothing known", because the alternative is what this
    function did until it was fixed: reject every unflagged player in the game
    and leave the sections to be filled by the handful FPL happens to have
    stamped 100%."""
    if (rec.get("status") or "a").lower() != "a":
        return False
    chance = rec.get("chance_of_playing_next_round")
    if chance is None or chance != chance:
        return True
    return chance >= 100


def _base_card(rec, gameweek):
    """The fields every section's player card shares.

    Everything the template needs is copied in here, because the row this ends
    up inside is frozen at the deadline and has to render years later without
    consulting a pipeline that has moved on. `path` is included so an archived
    edition still links to the player page, and `team_code` so the kit SVG
    still draws."""
    return {
        "code": rec.get("code"),
        "name": rec.get("web_name") or rec.get("full_name"),
        "full_name": rec.get("full_name"),
        "path": rec.get("path"),
        "pos": rec.get("pos"),
        "team_code": rec.get("team_code"),
        "team_name": rec.get("team_name") or "",
        "cost": rec.get("cost"),
        "form": _stat(rec, "form"),
        "owned": _stat(rec, "selected_by_percent"),
        "predicted": _predicted_for(rec, gameweek),
        "fixtures": _fixtures_from(rec, gameweek),
    }


# ---- section 1: in form ---------------------------------------------------

def in_form(pages, gameweek, limit=SECTION_SIZE):
    """Highest form among players who are fit and have actually been playing.

    Sorted on form with the model's projection as the tie-break, not the other
    way round: this section answers "who is hot right now", and the projection
    already has a section of its own on the players page.

    Form must be greater than zero, not merely present. Between seasons FPL
    reports every player at 0.0, and without this the section ranks a field of
    ties, picks three arbitrarily, and prints "X is on 0.0 form" under a
    heading that says In Form. Returning nothing is the honest answer to "who
    is in form" before anyone has played."""
    picks = []
    for rec in pages.values():
        if not _is_available(rec):
            continue
        if (_stat(rec, "minutes") or 0) < MIN_MINUTES_FOR_FORM:
            continue
        form = _stat(rec, "form")
        if form is None or float(form) <= 0:
            continue
        picks.append((float(form), _predicted_for(rec, gameweek) or 0.0, rec))

    picks.sort(key=lambda t: (-t[0], -t[1]))

    out = []
    for form, _pred, rec in picks[:limit]:
        card = _base_card(rec, gameweek)
        card["headline"] = f"{form:.1f} form"
        card["why"] = _in_form_reason(rec, form)
        out.append(card)
    return out


def _in_form_reason(rec, form):
    """One sentence, every clause backed by a printed number."""
    name = rec.get("web_name") or rec.get("full_name")
    goals, assists = _stat(rec, "goals_scored"), _stat(rec, "assists")
    bits = []
    if goals:
        bits.append(f"{int(goals)} goal{'s' if goals != 1 else ''}")
    if assists:
        bits.append(f"{int(assists)} assist{'s' if assists != 1 else ''}")

    line = f"{name} is on {form:.1f} form"
    if bits:
        line += f" with {' and '.join(bits)} this season"
    xgi = _stat(rec, "expected_goal_involvements")
    if xgi:
        line += f", from {float(xgi):.1f} expected goal involvements"
    return line + "."


# ---- section 2: differentials ---------------------------------------------

def differentials(pages, gameweek, limit=SECTION_SIZE):
    """Low ownership, high projection.

    The ranking is on projected points rather than on ownership: the goal is
    the best player nobody owns, not the least-owned player on the list, and
    sorting by ownership ascending would return the cheapest reserve in the
    league every week."""
    picks = []
    for rec in pages.values():
        if not _is_available(rec):
            continue
        owned = _stat(rec, "selected_by_percent")
        if owned is None or float(owned) > DIFFERENTIAL_OWNERSHIP:
            continue
        pred = _predicted_for(rec, gameweek)
        if pred is None or pred < DIFFERENTIAL_MIN_PREDICTED:
            continue
        picks.append((pred, -float(owned), rec))

    picks.sort(key=lambda t: (-t[0], t[1]))

    out = []
    for pred, _neg_owned, rec in picks[:limit]:
        card = _base_card(rec, gameweek)
        owned = float(_stat(rec, "selected_by_percent") or 0)
        card["headline"] = f"{owned:.1f}% owned"
        card["why"] = _differential_reason(rec, pred, owned)
        out.append(card)
    return out


def _differential_reason(rec, pred, owned):
    name = rec.get("web_name") or rec.get("full_name")
    cost = rec.get("cost")
    line = f"Projected {pred:.1f} points at {owned:.1f}% ownership"
    if cost:
        line += f", and £{cost:.1f}m"
    xgi = _stat(rec, "expected_goal_involvements")
    if xgi:
        line += f". {name} has {float(xgi):.1f} expected goal involvements behind that"
    return line + "."


# ---- section 3: fixture swings --------------------------------------------

def fixture_runs(rotation_df, pages, gameweek, limit=SECTION_SIZE):
    """The clubs whose next few games are kindest, at each end of the pitch.

    Difficulty here is the rotation planner's own net figure - a team's
    strength against its opponent's, not the opponent's in isolation - so a
    good defence facing a weak attack rates as easy, which is the whole point
    of that column. Lower is easier, and it can go negative.

    Returns two lists: attacking runs (target the forwards) and defensive runs
    (target the defenders and keeper)."""
    if rotation_df is None or not len(rotation_df):
        return {"attack": [], "defence": []}

    horizon = rotation_df[
        (rotation_df["event"] >= gameweek)
        & (rotation_df["event"] < gameweek + FIXTURE_HORIZON)]
    if not len(horizon):
        return {"attack": [], "defence": []}

    # The rotation table identifies clubs by SHORT name ("MCI"); the page
    # records carry the full name in `team_name` and the short one in
    # `team_short`. Matching the two on the wrong field is silent - every club
    # simply returns no players - so the mapping is built once, explicitly,
    # and used for both the lookup and the display name.
    full_names = {rec.get("team_short"): rec.get("team_name")
                  for rec in pages.values() if rec.get("team_short")}

    out = {}
    for key, col, positions in (("attack", "attacking_difficulty", ("FWD", "MID")),
                                ("defence", "defensive_difficulty", ("DEF", "GK"))):
        means = horizon.groupby("team_name")[col].mean().sort_values()

        # The raw column is a difference between two of FPL's own strength
        # figures, which run to about 1000 - so it comes out as -803.5 or
        # 462.3, numbers that mean nothing to a reader and don't even share a
        # sign between the two ends of the pitch. Rescaled to 0-10 across all
        # twenty clubs, where 10 is the easiest run in the division.
        #
        # This is a genuinely relative measure and printing it as one is the
        # honest presentation: it says "kindest in the league", which is what
        # the section is claiming, rather than implying an absolute scale that
        # doesn't exist.
        easiest, hardest = float(means.min()), float(means.max())
        span = hardest - easiest

        runs = []
        for short_name, difficulty in list(means.items())[:limit]:
            ease = 10.0 if span <= 0 else 10.0 * (hardest - float(difficulty)) / span
            games = horizon[horizon["team_name"] == short_name].sort_values("event")
            team_code = None
            if "team_code" in games.columns and len(games):
                v = games.iloc[0]["team_code"]
                team_code = int(v) if v == v else None
            runs.append({
                # Full name for a reader, short name kept because the fixture
                # labels beside it are short names too.
                "team_name": full_names.get(short_name) or short_name,
                "team_short": short_name,
                "team_code": team_code,
                # `ease` is what the page prints. `difficulty` is kept because
                # it's the figure the rotation planner works in, so the two
                # pages can still be reconciled when one of them looks wrong.
                "ease": round(ease, 1),
                "difficulty": round(float(difficulty), 1),
                "fixtures": [
                    {"event": int(r["event"]),
                     "label": f"{r['opponent']} ({'H' if r['was_home'] else 'A'})"}
                    for _, r in games.iterrows()],
                "players": _players_from_team(pages, short_name, gameweek, positions),
            })
        out[key] = runs
    return out


def _players_from_team(pages, team_short, gameweek, positions, limit=2):
    """The best-projected fit players at a club, in the positions that benefit.

    Matched on `team_short`, which is what the rotation table's team column
    holds. Matching on the full name here returns nothing for every club and
    does it quietly - the section still renders, just with no players under any
    of the fixture runs."""
    picks = []
    for rec in pages.values():
        if rec.get("team_short") != team_short or rec.get("pos") not in positions:
            continue
        if not _is_available(rec):
            continue
        pred = _predicted_for(rec, gameweek)
        if pred is None:
            continue
        picks.append((pred, rec))
    picks.sort(key=lambda t: -t[0])
    return [{"name": r.get("web_name") or r.get("full_name"),
             "path": r.get("path"), "pos": r.get("pos"), "cost": r.get("cost"),
             "predicted": round(p, 1)}
            for p, r in picks[:limit]]


# ---- section 4: team news -------------------------------------------------

def team_news(pages, gameweek, limit=6):
    """Flagged players that enough people own to matter.

    Ordered by ownership rather than by severity: a 40%-owned player rated a
    doubt affects far more readers than a 9%-owned player ruled out, and the
    reader who owns the second one can see the flag on his page anyway."""
    items = []
    for rec in pages.values():
        status = (rec.get("status") or "a").lower()
        chance = rec.get("chance_of_playing_next_round")
        flagged = status != "a" or (chance is not None and chance < 100)
        if not flagged:
            continue
        owned = _stat(rec, "selected_by_percent")
        if owned is None or float(owned) < NEWS_MIN_OWNERSHIP:
            continue
        card = _base_card(rec, gameweek)
        card["status"] = status
        card["chance"] = chance
        # `availability_sentence` is the same wording the player page uses, so
        # the two can't contradict each other, and it never invents a
        # percentage FPL hasn't published.
        card["why"] = player_pages.availability_sentence(rec)
        items.append((float(owned), card))

    items.sort(key=lambda t: -t[0])
    return [card for _owned, card in items[:limit]]


# ---- assembly -------------------------------------------------------------

def deadline_label(deadline):
    """FPL's deadline timestamp as something a person reads.

    "Sat 22 Aug, 11:00" rather than "2026-08-22T11:00:00Z". Times are printed
    as UTC because that is what the API states and the site does not ask where
    anyone is - and for most of the season UK time is UTC anyway. Returns an
    empty string rather than raising: a briefing with no deadline on it is
    worth publishing, a job that dies formatting one is not."""
    if not deadline:
        return ""
    try:
        when = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    when = when.astimezone(timezone.utc)
    # %-d isn't portable to Windows, where this gets run by hand for testing.
    day = str(when.day)
    return f"{when:%a} {day} {when:%b}, {when:%H:%M} UK"


def build(pages, gameweek, rotation_df=None, deadline=None, season_label=None):
    """The whole edition, as the JSON-serialisable dict stored in `gw_report`.

    Sections can legitimately come back empty - preseason there is no form to
    report, and an international break leaves the fixture section thin - so the
    template renders whatever is present rather than assuming four sections.
    An edition with nothing in it at all is still worth storing: it records
    that the week was quiet, rather than looking like a job that failed."""
    runs = fixture_runs(rotation_df, pages, gameweek)
    report = {
        "gameweek": int(gameweek),
        "season": season_label,
        "deadline": deadline,
        # A readable form of the same timestamp, for the page and for the social
        # drafts. Written here rather than formatted at each use so the deadline
        # a reader sees on the page is the one the post links to it with.
        "deadline_label": deadline_label(deadline),
        # Publishing stage: draft while it's still being rebuilt nightly,
        # preview once it's worth posting a day out, final once frozen. Set by
        # jobs.py as the edition moves through them; see social.STAGES.
        "stage": "draft",
        "in_form": in_form(pages, gameweek),
        "differentials": differentials(pages, gameweek),
        "attack_runs": runs["attack"],
        "defence_runs": runs["defence"],
        "news": team_news(pages, gameweek),
    }
    report["summary"] = summarise(report)
    return report


def summarise(report):
    """The standfirst under the masthead, and the text the social drafts and
    the RSS description reuse. Built from whichever sections have content."""
    gw = report["gameweek"]
    bits = []
    if report["in_form"]:
        names = ", ".join(p["name"] for p in report["in_form"])
        bits.append(f"{names} lead the form table")
    if report["differentials"]:
        d = report["differentials"][0]
        bits.append(f"{d['name']} is the standout differential at {d['headline']}")
    if report["attack_runs"]:
        bits.append(f"{report['attack_runs'][0]['team_name']} have the kindest attacking run")
    if report["news"]:
        bits.append(f"{len(report['news'])} widely-owned players carry a fitness flag")

    if not bits:
        return (f"The Gameweek {gw} briefing. Not enough has been played yet for "
                "form, fixtures or ownership to say anything useful - this page "
                "fills out once the season is under way.")
    return f"Gameweek {gw}: " + "; ".join(bits) + "."
