"""Draft post text for each gameweek edition, written to disk for a human.

Deliberately NOT a posting client. Automated submissions to Reddit are removed
and get accounts banned - r/FantasyPL treats unsolicited tool links as spam,
which is a fair description of what an unattended script produces - and a
banned account is a worse outcome than a week with no post. X's API allows it
but costs money for write access.

So the machine does the writing and the person does the posting. The job
generates a file per gameweek; you read it, edit anything that reads badly, and
paste it. That keeps the judgement call - is this week's page actually worth
posting - with the only party able to make it.

Files land next to the SQLite database, so they're on the same mounted volume
and survive a redeploy.
"""

import os
import re

import db

# One X post. The real limit is 280; the margin is for the edit you make before
# posting, so a word added by hand doesn't silently push it over.
X_LIMIT = 272

# X counts every URL as 23 characters however long it really is, so a raw len()
# on a draft containing one over-counts by however much the URL exceeds that.
# The drafts print their own length, and a printed length that disagrees with
# the one X shows you is worse than not printing it.
X_URL_COST = 23
_URL_RE = re.compile(r"https?://\S+")


def x_length(text):
    """The length X will show for this post."""
    return len(_URL_RE.sub("u" * X_URL_COST, text))


def social_dir():
    """state/social/ alongside the database.

    Derived from FPL_DB_PATH rather than configured separately: there is
    already exactly one writable, persistent directory in this deployment and a
    second setting to keep in sync would be a second thing to get wrong."""
    path = os.path.join(os.path.dirname(os.path.abspath(db.db_path())), "social")
    os.makedirs(path, exist_ok=True)
    return path


def _site_url():
    return os.environ.get("FPL_SITE_URL", "https://fpl.mfhost.co.uk").rstrip("/")


# _truncate() lived here. It cut an over-long post at a word boundary and added
# an ellipsis, which is the right way to truncate and the wrong thing to do at
# all: what it actually removed was always the last section of the briefing, so
# the fixture runs and the injury flags were silently dropped from every busy
# week. _fit() drops whole lines against the same budget instead, which loses
# the same information but produces a post that reads as if it were written that
# length.


def _deadline_line(report, stage):
    """"Deadline: Sat 22 Aug, 11:00 UK", on the preview drafts only.

    It is the reason the preview exists. A post a day out is competing with
    everything else in a timeline, and "you have until Saturday morning" is what
    makes someone open it now rather than scroll past. On the final drafts it
    would be stating the obvious - by then the deadline is an hour away and
    anyone reading is already in the app.

    Returns None rather than "" when there is nothing to say, because `_fit`
    reads "" as a deliberate blank separator line.
    """
    label = report.get("deadline_label")
    return f"⏳ Deadline: {label}" if stage == "preview" and label else None


# ---------------------------------------------------------------------------
#  Facts
#
#  The drafts used to be a list of names: "In form: Semenyo, Saka, Wood". That
#  is the shape of a post nobody engages with, because it asserts a ranking and
#  shows none of the reasoning - a reader has no way to tell whether it came
#  from a model or from a coin. Everything below exists to put the number next
#  to the name, which is the only thing that makes a generated post worth
#  reading and is also the site's whole argument for itself.
# ---------------------------------------------------------------------------

def _n(value, dp=1):
    """A number for a post, or None if it isn't one. NaN reads as absent - it
    arrives that way from pandas and formats as the literal word 'nan'."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return f"{v:.{dp}f}"


def player_facts(p, include=("predicted", "owned", "cost", "form")):
    """The parenthesised numbers after a player's name, e.g.
    "(6.8 pts, 4.1% owned, £7.5m)". Whichever of them the card actually
    carries - a section that doesn't compute form shouldn't print "form None".
    """
    bits = []
    for key in include:
        if key == "predicted" and _n(p.get("predicted")):
            bits.append(f"{_n(p['predicted'])} pts")
        elif key == "owned" and _n(p.get("owned")):
            bits.append(f"{_n(p['owned'])}% owned")
        elif key == "cost" and _n(p.get("cost")):
            bits.append(f"£{_n(p['cost'])}m")
        elif key == "form" and _n(p.get("form")) and float(p["form"]) > 0:
            bits.append(f"{_n(p['form'])} form")
    return ", ".join(bits)


def player_line(p, include=("predicted", "owned", "cost")):
    """"Semenyo (6.8 pts, 4.1% owned, £7.5m)"."""
    facts = player_facts(p, include)
    return f"{p['name']} ({facts})" if facts else p["name"]


# A differential is worth leading a post with once the model rates it this
# highly - below it, "nobody owns him" is a description of a player nobody
# should own.
HOOK_DIFFERENTIAL_MIN = 5.0

# And a form figure has to be genuinely top-of-the-league to be the headline
# rather than a section. Sevens happen most weeks; this is deliberately above
# the level the In Form section itself reports on.
HOOK_FORM_MIN = 7.0


def hook(report):
    """The single most postable fact in the edition, as one sentence.

    A ladder rather than a score. The four candidates are measured in different
    units - form, projected points, ownership, fixture ease - and there is no
    honest way to compare 7.4 form against 6.1 projected points, so anything
    that claimed to rank them would just be encoding a preference as
    arithmetic. This encodes the preference directly instead, and each rung
    carries the threshold that has to be cleared before it fires.

    The order is by what a reader can't get elsewhere. A well-rated name at 3%
    ownership is the one thing on the page no other FPL account is posting; a
    captaincy pick is the thing every one of them is.
    """
    d = (report.get("differentials") or [None])[0]
    if d and (_n(d.get("predicted")) and float(d["predicted"]) >= HOOK_DIFFERENTIAL_MIN):
        return (f"{d['name']} projects {_n(d['predicted'])} points this gameweek "
                f"and is owned by {_n(d.get('owned')) or '<5'}% — the best-rated "
                "name almost nobody has.")

    f = (report.get("in_form") or [None])[0]
    if f and (_n(f.get("form")) and float(f["form"]) >= HOOK_FORM_MIN):
        return (f"{f['name']} is on {_n(f['form'])} form, the highest of any fit "
                "player in the game right now.")

    c = (report.get("captains") or [None])[0]
    if c and _n(c.get("predicted")):
        return (f"{c['name']} is the model's top captain pick this gameweek, "
                f"projected {_n(c['predicted'])} points.")

    r = (report.get("attack_runs") or [None])[0]
    if r:
        n = len(r.get("fixtures") or [])
        over = f" over the next {n} gameweeks" if n else ""
        return (f"{r['team_name']} have the kindest attacking fixtures in the "
                f"division{over}.")

    return report.get("summary") or f"The Gameweek {report['gameweek']} briefing."


def scorecard():
    """How the AI Best XI has actually done: last settled gameweek, and the
    season to date.

    This is the most valuable thing the site can put in a post and the drafts
    carried none of it. Every FPL account publishes picks; almost none publish
    what their last set of picks scored, because the numbers weren't frozen
    beforehand and so can't be quoted honestly afterwards. These were - the
    snapshot's `predicted_points` is written at the deadline and never
    recomputed, which is exactly what makes "predicted 62.4, scored 71" a
    statement rather than a boast.

    Imported lazily. This module is pulled in by the web app at startup, and
    ai_team drags in the ILP solver behind it; a draft-writing helper should
    not be the reason PuLP loads.

    Returns None when nothing has been scored yet - a preseason post shouldn't
    claim a record it doesn't have.
    """
    try:
        import ai_team
        snapshots = ai_team.list_snapshots()
    except Exception:
        return None

    scored = [s for s in snapshots
              if s.get("actual_points") is not None
              and s.get("predicted_points") is not None]
    if not scored:
        return None

    # list_snapshots is newest first, so the first scored row is the most
    # recent settled gameweek.
    last = scored[0]
    return {
        "gameweek": last["gameweek"],
        "predicted": round(float(last["predicted_points"]), 1),
        "actual": int(last["actual_points"]),
        "weeks": len(scored),
        "avg_predicted": round(
            sum(float(s["predicted_points"]) for s in scored) / len(scored), 1),
        "avg_actual": round(
            sum(float(s["actual_points"]) for s in scored) / len(scored), 1),
    }


def scorecard_line(card=None):
    """One sentence of track record, or "" if there isn't one yet."""
    card = card if card is not None else scorecard()
    if not card:
        return ""
    line = (f"Last gameweek the AI Best XI was projected {_n(card['predicted'])} "
            f"and scored {card['actual']}")
    if card["weeks"] > 1:
        line += (f". Across {card['weeks']} settled gameweeks it averages "
                 f"{_n(card['avg_actual'])} actual against {_n(card['avg_predicted'])} "
                 "projected")
    return line + "."


def _fit(required, optional, limit, joiner="\n"):
    """Assemble a post from lines that must appear and lines that would be nice
    to.

    Drops whole optional lines from the end until it fits, rather than cutting
    the text mid-sentence the way the old draft did. A post ending "and Wood is
    on 6.…" reads as broken software; one line shorter reads as edited.

    An empty string is a deliberate blank line between blocks and is kept. None
    is an absent line - which is what the optional helpers return when they have
    nothing to say - and is skipped. Conflating the two cost every blank
    separator in the thread, so the armband and the form list ran together.
    """
    lines = list(required)
    for line in optional:
        if line is None:
            continue
        candidate = lines + [line]
        if x_length(joiner.join(candidate)) <= limit:
            lines = candidate
    # A block that was dropped can leave its separator behind as the last line.
    while lines and lines[-1] == "":
        lines.pop()
    return joiner.join(lines)


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def draft_x(report, stage="draft"):
    """A single standalone post, for when a thread is more than the week
    deserves. Leads on the hook rather than on the masthead: "⚽ FPL Gameweek 4
    briefing" is a headline about a web page, and nobody has ever stopped
    scrolling for one.

    Two or three tags is what actually circulates in FPL; a wall of them reads
    as automated because it is."""
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    tags = "#FPL #FantasyPL"

    required = [hook(report)]
    optional = [_deadline_line(report, stage)]

    if report.get("captains"):
        c = report["captains"][0]
        optional.append(f"©️ {player_line(c, ('predicted', 'owned'))}")
    if report.get("differentials"):
        d = report["differentials"][0]
        optional.append(f"💎 {player_line(d, ('predicted', 'owned', 'cost'))}")
    if report.get("attack_runs"):
        optional.append(f"📅 Best fixtures: {report['attack_runs'][0]['team_name']}")
    if report.get("news"):
        n_flags = len(report["news"])
        optional.append(f"🚑 {n_flags} widely-owned fitness "
                        f"{_plural(n_flags, 'flag')}")

    # The link and tags are reserved out of the budget before the optional
    # lines compete for what's left, so a post can never fit its body and then
    # overflow on the URL that was always going to be added.
    tail = f"\n\n{url}\n\n{tags}"
    body = _fit(required, optional, X_LIMIT - x_length(tail))
    return body + tail


# How many posts a thread runs to. Three is the length that still gets read:
# the first earns the click, the second justifies it, the third asks for
# something. Beyond that the drop-off is steeper than the extra content is
# worth.
X_THREAD_POSTS = 3


def draft_x_thread(report, stage="draft"):
    """The briefing as a thread, one string per post.

    A thread rather than one post because the single post could only ever carry
    four names and no numbers - it was hitting the limit on the names alone and
    then truncating. Split across three, every claim can bring its evidence,
    which is the difference between a post that asserts a ranking and one that
    shows its working.

    The link is in the LAST post, not the first. X is widely reported to
    down-rank posts carrying an external link, and the opening post is the one
    that has to earn impressions for the other two; putting the URL where the
    reader has already decided they're interested costs nothing.
    """
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    n = X_THREAD_POSTS
    posts = []

    # --- 1: the hook ---
    lead_required = [f"{hook(report)}", ""]
    lead_optional = [_deadline_line(report, stage),
                     f"The Gameweek {gw} briefing, thread 🧵 1/{n}"]
    posts.append(_fit(lead_required, lead_optional, X_LIMIT))

    # --- 2: the picks, with their numbers ---
    picks_required = [f"Gameweek {gw} — what the model likes 2/{n}", ""]
    picks_optional = []
    if report.get("captains"):
        picks_optional.append("©️ Armband")
        for p in report["captains"][:3]:
            picks_optional.append(f"• {player_line(p, ('predicted', 'owned'))}")
        picks_optional.append("")
    if report.get("in_form"):
        picks_optional.append("📈 In form")
        for p in report["in_form"][:3]:
            picks_optional.append(f"• {player_line(p, ('form', 'predicted'))}")
    posts.append(_fit(picks_required, picks_optional, X_LIMIT))

    # --- 3: the differentials, the flags, the link ---
    tail = f"\n\n{url}\n\n#FPL #FantasyPL"
    last_required = [f"Where the rank is won {n}/{n}", ""]
    last_optional = []
    if report.get("differentials"):
        last_optional.append("💎 Under 5% owned")
        for p in report["differentials"][:3]:
            last_optional.append(f"• {player_line(p, ('predicted', 'owned', 'cost'))}")
        last_optional.append("")
    if report.get("attack_runs"):
        runs = ", ".join(r["team_name"] for r in report["attack_runs"][:3])
        last_optional.append(f"📅 Kindest attacking fixtures: {runs}")
    if report.get("news"):
        n_flags = len(report["news"])
        last_optional.append(
            f"🚑 {n_flags} widely-owned {_plural(n_flags, 'player')} "
            f"{'carries' if n_flags == 1 else 'carry'} a flag")
    posts.append(_fit(last_required, last_optional,
                      X_LIMIT - x_length(tail)) + tail)

    return posts


def draft_reddit(report, stage="draft"):
    """Markdown, and written to stand on its own.

    The rule that gets link posts removed is that they send people away without
    saying anything. So the numbers are in the comment itself - someone who
    never clicks still got the information, and the link is a source rather
    than a toll gate.

    Three things were added when this stopped being a list of names. The
    armband table, because captaincy is the decision the subreddit argues about
    every week and a post with no opinion on it is a post with nothing to argue
    with. The track record, because "here are my picks" is worth nothing from a
    stranger and "here is what my last set of picks scored, frozen before
    kickoff" is worth reading. And a closing question, because a post that asks
    nothing gets read and scrolled past, and the comments are the entire reason
    to post on Reddit rather than anywhere else.
    """
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    # The title names the sections the post actually carries. Early in a season
    # the armband is withheld (see gw_report.captain_picks), and a headline
    # promising it over a post without it is the sort of thing a subreddit
    # notices first and reads second.
    billed = ["the armband"] if report.get("captains") else []
    billed += ["differentials", "fixture swings"]
    out = [f"**Gameweek {gw}: {', '.join(billed[:-1])} and {billed[-1]} "
           "— from a trained points model**", ""]
    out += [hook(report), ""]

    deadline = _deadline_line(report, stage)
    if deadline:
        out += [f"Deadline **{report['deadline_label']}**.", ""]

    # A table rather than a list. Three rows of three numbers is exactly what a
    # table is for, and it's the format that makes the gap between the top pick
    # and the next one visible at a glance rather than something to work out.
    if report.get("captains"):
        out.append("**The armband — highest projections in the game**")
        out.append("")
        out.append("| | Player | Projected | Owned | Fixture |")
        out.append("|---|---|---|---|---|")
        for p in report["captains"]:
            fx = p["fixtures"][0]["label"] if p.get("fixtures") else "—"
            out.append(f"| {p.get('rank', '')} | {p['name']} ({p['pos']}) "
                       f"| {_n(p.get('predicted')) or '—'} "
                       f"| {_n(p.get('owned')) or '—'}% | {fx} |")
        out.append("")

    if report.get("in_form"):
        out.append("**In form**")
        for p in report["in_form"]:
            team = f", {p['team_name']}" if p.get("team_name") else ""
            out.append(f"- **{p['name']}** ({p['pos']}{team}) — {p['why']}")
        out.append("")
    if report.get("differentials"):
        out.append("**Differentials (under 5% owned)**")
        for p in report["differentials"]:
            # No facts suffix here, unlike the other sections: a differential's
            # `why` already states the projection, the ownership and the price,
            # so appending them again printed each number twice in one line.
            team = f", {p['team_name']}" if p.get("team_name") else ""
            out.append(f"- **{p['name']}** ({p['pos']}{team}) — {p['why']}")
        out.append("")
    if report.get("attack_runs"):
        out.append("**Kindest attacking fixtures**")
        for r in report["attack_runs"]:
            games = ", ".join(f["label"] for f in r["fixtures"][:4])
            ease = _n(r.get("ease"))
            score = f" *({ease}/10 for ease)*" if ease else ""
            out.append(f"- **{r['team_name']}**: {games}{score}")
        out.append("")
    if report.get("defence_runs"):
        out.append("**Kindest defensive fixtures**")
        for r in report["defence_runs"]:
            games = ", ".join(f["label"] for f in r["fixtures"][:4])
            ease = _n(r.get("ease"))
            score = f" *({ease}/10 for ease)*" if ease else ""
            out.append(f"- **{r['team_name']}**: {games}{score}")
        out.append("")
    if report.get("news"):
        out.append("**Fitness flags on widely-owned players**")
        for p in report["news"]:
            owned = f" ({p['owned']:.0f}% owned)" if p.get("owned") else ""
            out.append(f"- **{p['name']}**{owned} — {p['why']}")
        out.append("")

    record = scorecard_line()
    if record:
        out += ["**How the model has actually done**", "",
                record,
                "",
                "Those numbers are frozen at the deadline and never recomputed, "
                "which is the only reason they're worth quoting — a prediction "
                "you can edit afterwards isn't one.",
                ""]

    out.append(f"Full page with the underlying numbers: {url}")
    out.append("")
    out.append("Ratings come from a gradient-boosted points model trained on "
               "per-gameweek data, one model per position. Happy to explain any "
               "of the numbers, and happy to be told where it's wrong.")
    out.append("")
    out += [_reddit_question(report)]
    return "\n".join(out)


def _reddit_question(report):
    """The closing line, picked from what the edition actually contains.

    Named after something specific in the post rather than a generic "thoughts?"
    - a question about a player people have an opinion on gets answers, and a
    question about nothing gets none."""
    if report.get("captains") and len(report["captains"]) > 1:
        a, b = report["captains"][0], report["captains"][1]
        return (f"Who's getting your armband this week — {a['name']} or "
                f"{b['name']}? The model has barely anything between them.")
    if report.get("differentials"):
        d = report["differentials"][0]
        return (f"Anyone actually on {d['name']}? Curious whether the low "
                "ownership is telling us something the numbers aren't.")
    if report.get("news"):
        p = report["news"][0]
        return f"Anyone moving {p['name']} on, or holding and hoping?"
    return "What's the model missing this week?"


def draft_discord(report, stage="draft"):
    """Between the two: short enough to read in a channel, no link-post rules
    to satisfy, and Discord renders the same Markdown.

    Carries the numbers the X post has to leave out and the prose the Reddit
    post has room for, which is roughly what a channel wants - scannable, but
    not so thin that it reads as a bot announcement."""
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    out = [f"**⚽ FPL Gameweek {gw} briefing**", "", hook(report), ""]
    deadline = _deadline_line(report, stage)
    if deadline:
        out += [deadline, ""]
    if report.get("captains"):
        out.append("**©️ Armband:** " + " · ".join(
            player_line(p, ("predicted", "owned")) for p in report["captains"]))
    if report.get("in_form"):
        out.append("**📈 In form:** " + " · ".join(
            player_line(p, ("form", "predicted")) for p in report["in_form"]))
    if report.get("differentials"):
        out.append("**💎 Differentials:** " + " · ".join(
            player_line(p, ("predicted", "owned", "cost"))
            for p in report["differentials"]))
    if report.get("attack_runs"):
        out.append("**📅 Best attacking fixtures:** " + ", ".join(
            r["team_name"] for r in report["attack_runs"]))
    if report.get("defence_runs"):
        out.append("**🧱 Best defensive fixtures:** " + ", ".join(
            r["team_name"] for r in report["defence_runs"]))
    if report.get("news"):
        out.append("**🚑 Flagged:** " + ", ".join(
            f"{p['name']} ({p['owned']:.0f}%)" if p.get("owned") else p["name"]
            for p in report["news"]))

    record = scorecard_line()
    if record:
        out += ["", f"**📊 Track record:** {record}"]

    out.append("")
    # Angle brackets suppress Discord's link preview. The page's own Open Graph
    # card is a fine thing, but in a channel it doubles the height of the
    # message and pushes the numbers above it out of view.
    out.append(f"<{url}>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
#  Roundup drafts
#
#  The other half of the week. The briefing is a set of claims made before a
#  deadline; these are the results, and they are the easier post to make well -
#  nobody has to be persuaded that what happened happened.
#
#  They are also the post that earns the briefing its audience. "Here is who I
#  think will score" from a stranger is worth nothing; the same account posting
#  "here is what last week's picks actually did, and here is the number I
#  published beforehand" is worth following. That is why the scorecard is in
#  every one of these and not an optional extra.
# ---------------------------------------------------------------------------

def roundup_hook(roundup):
    """The most postable fact in a settled gameweek, as one sentence.

    A ladder, like `hook`, and ordered the same way: by what a reader can't get
    from the BBC. A result against the table is a story anyone can tell, so it
    ranks below the top score - but the model's own scorecard outranks both,
    because it is the only line here that nobody else can post.
    """
    card = roundup.get("scorecard")
    if card and _n(card.get("predicted")) and card.get("actual") is not None:
        beat = card["actual"] - float(card["predicted"])
        outcome = (f"{_n(beat)} more than projected" if beat >= 0
                   else f"{_n(abs(beat))} short of it")
        return (f"The AI Best XI was projected {_n(card['predicted'])} points "
                f"before Gameweek {roundup['gameweek']} and scored "
                f"{card['actual']} — {outcome}.")

    t = (roundup.get("top_scorers") or [None])[0]
    if t:
        return f"{t['name']} top scored in Gameweek {roundup['gameweek']} with {t['points']} points."

    s = (roundup.get("shocks") or [None])[0]
    if s:
        return f"{s['headline']} was the result of the round."

    return roundup.get("summary") or f"The Gameweek {roundup['gameweek']} roundup."


def roundup_scorecard_line(roundup):
    """The predicted-versus-actual sentence for this specific round."""
    card = roundup.get("scorecard")
    if not card or card.get("actual") is None or not _n(card.get("predicted")):
        return ""
    diff = card["actual"] - float(card["predicted"])
    return (f"The AI Best XI was projected {_n(card['predicted'])} and scored "
            f"{card['actual']} ({'+' if diff >= 0 else ''}{_n(diff)}). That "
            "projection was frozen at the deadline and never recomputed.")


def draft_roundup_x(roundup):
    """One post. A roundup doesn't need a thread - the result is the whole
    story, and the numbers that support it are three lines rather than ten."""
    gw = roundup["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}/roundup"
    tail = f"\n\n{url}\n\n#FPL #FantasyPL"

    required = [roundup_hook(roundup), ""]
    optional = []
    if roundup.get("top_scorers"):
        optional.append("🏆 Top scorers")
        for p in roundup["top_scorers"][:3]:
            optional.append(f"• {p['name']} — {p['points']} pts")
        optional.append("")
    if roundup.get("shocks"):
        optional.append(f"😳 {roundup['shocks'][0]['headline']}")
    if roundup.get("momentum"):
        m = roundup["momentum"][0]
        optional.append(f"🔥 {m['team_name']} — {m['headline']}")

    return _fit(required, optional, X_LIMIT - x_length(tail)) + tail


def draft_roundup_reddit(roundup):
    """Markdown, written to stand on its own - same rule as the briefing."""
    gw = roundup["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}/roundup"
    out = [f"**Gameweek {gw} roundup: top scorers, the blanks that hurt, and "
           "what the model got wrong**", ""]
    out += [roundup_hook(roundup), ""]

    if roundup.get("top_scorers"):
        out += ["**Top scorers**", ""]
        out.append("| Player | Points | Owned |")
        out.append("|---|---|---|")
        for p in roundup["top_scorers"]:
            out.append(f"| {p['name']} ({p['pos']}, {p['team_name']}) "
                       f"| {p['points']} | {_n(p.get('owned')) or '—'}% |")
        out.append("")
        for p in roundup["top_scorers"]:
            out.append(f"- {p['why']}")
        out.append("")

    if roundup.get("underperformers"):
        out += ["**The blanks**", "",
                "Widely-owned players who played an hour or more and returned "
                "almost nothing. The expected-goal figure is the part worth "
                "reading — it separates a bad week from a bad player.", ""]
        for p in roundup["underperformers"]:
            out.append(f"- **{p['name']}** ({p['pos']}) — {p['why']}")
        out.append("")

    if roundup.get("shocks"):
        out += ["**Against the table**", ""]
        for s in roundup["shocks"]:
            out.append(f"- **{s['headline']}** — {s['why']}")
        out.append("")

    if roundup.get("momentum"):
        out += ["**On a run**", ""]
        for m in roundup["momentum"]:
            out.append(f"- **{m['team_name']}** — {m['why']}")
        out.append("")

    record = roundup_scorecard_line(roundup)
    if record:
        out += ["**How the model did**", "", record, "",
                "Posting this whether it's good or bad is the point — a "
                "prediction you only publish when it worked isn't one.", ""]
    season = scorecard_line()
    if season:
        out += [season, ""]

    out.append(f"Full page with the underlying numbers: {url}")
    out.append("")
    out.append(f"The Gameweek {gw + 1} briefing goes up before the next "
               f"deadline: {_site_url()}/gameweek")
    out.append("")
    out.append(_roundup_question(roundup))
    return "\n".join(out)


def _roundup_question(roundup):
    """The closing line. Named after something in the post, for the same reason
    the briefing's is - a question about nothing gets no answers."""
    if roundup.get("underperformers"):
        p = roundup["underperformers"][0]
        return (f"Anyone selling {p['name']} after that, or is the "
                "expected-goals number enough to hold?")
    if roundup.get("shocks"):
        return (f"Did anyone see {roundup['shocks'][0]['headline']} coming? "
                "Genuinely curious whether the underlying numbers did.")
    if roundup.get("top_scorers"):
        return f"Who had {roundup['top_scorers'][0]['name']} captained?"
    return "What did the model miss this week?"


def draft_roundup_discord(roundup):
    """Scannable, for a channel."""
    gw = roundup["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}/roundup"
    out = [f"**📋 FPL Gameweek {gw} roundup**", "", roundup_hook(roundup), ""]
    if roundup.get("top_scorers"):
        out.append("**🏆 Top scorers:** " + " · ".join(
            f"{p['name']} ({p['points']})" for p in roundup["top_scorers"]))
    if roundup.get("underperformers"):
        out.append("**😐 Blanked:** " + " · ".join(
            f"{p['name']} ({p['points']})" for p in roundup["underperformers"]))
    if roundup.get("shocks"):
        out.append("**😳 Against the table:** " + " · ".join(
            s["headline"] for s in roundup["shocks"]))
    if roundup.get("momentum"):
        out.append("**🔥 On a run:** " + " · ".join(
            f"{m['team_name']} ({m['headline']})" for m in roundup["momentum"]))
    # The scorecard, unless the hook above already led with it - which it does
    # whenever there is one, because it outranks everything else on the ladder.
    # Printing both said the same numbers twice in a five-line message.
    record = roundup_scorecard_line(roundup)
    if record and not roundup.get("scorecard"):
        out += ["", f"**📊 {record}**"]
    out.append("")
    out.append(f"<{url}>")
    return "\n".join(out)


def write_roundup_drafts(roundup):
    """One file per gameweek roundup, holding all three drafts.

    Written once, when the roundup is - there is no nightly rewrite here,
    because the roundup itself never changes. `roundup_` prefixes the filename
    so the briefing and the roundup for the same gameweek sit next to each
    other in a directory listing without one being able to overwrite the other.
    """
    gw = roundup["gameweek"]
    x_post = draft_roundup_x(roundup)

    body = f"""FPL Buddy — Gameweek {gw} ROUNDUP social drafts
{'=' * 46}

STATUS: FINAL — a roundup is written from settled results and never changes.
        Safe to post as soon as you see it.

Edit anything that reads badly before posting. These are generated from
thresholds, so they are accurate but not always well-phrased.

{'-' * 46}
X / TWITTER  ({x_length(x_post)} chars, limit 280)
{'-' * 46}
{x_post}

{'-' * 46}
REDDIT  (r/FantasyPL — a results post is much easier to get right than a
         predictions one; the numbers are all in the post)
{'-' * 46}
{draft_roundup_reddit(roundup)}

{'-' * 46}
DISCORD
{'-' * 46}
{draft_roundup_discord(roundup)}
"""

    path = os.path.join(social_dir(), f"roundup_gw{gw:02d}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def read_roundup_drafts(gameweek):
    """The roundup drafts for a gameweek, or None."""
    path = os.path.join(social_dir(), f"roundup_gw{int(gameweek):02d}.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (FileNotFoundError, OSError):
        return None


def list_roundup_drafts():
    """Gameweek numbers that have a roundup drafts file, newest first."""
    try:
        names = os.listdir(social_dir())
    except OSError:
        return []
    gws = [int(m.group(1)) for n in names
           for m in [re.fullmatch(r"roundup_gw(\d+)\.txt", n)] if m]
    return sorted(gws, reverse=True)


# ---------------------------------------------------------------------------
#  Player spotlight drafts
#
#  The daily post. Unlike the briefing and the roundup there is no page to link
#  to - these are written for the admin to post and are deleted once the
#  gameweek is over - so the drafts have to carry the whole argument themselves
#  and the only link is to the player's own page, which is the part that is
#  permanent.
# ---------------------------------------------------------------------------

def players_dir():
    """state/social/players/ - one file per night, alongside the gameweek
    drafts rather than mixed in with them. A season is ~250 of these and they
    would otherwise bury the 38 files that matter most."""
    path = os.path.join(social_dir(), "players")
    os.makedirs(path, exist_ok=True)
    return path


def _player_url(post):
    return f"{_site_url()}{post['path']}" if post.get("path") else _site_url()


def draft_player_x(post):
    """A thread. One post cannot carry an argument, and an argument is the
    entire difference between this and the briefing's one-line mentions."""
    url = _player_url(post)
    tail = f"\n\n{url}\n\n#FPL #FantasyPL"
    posts = []

    # --- 1: the claim ---
    #
    # The thread marker is appended after the fit rather than passed into it,
    # so it survives when the evidence paragraphs don't. `_fit` only ever drops
    # optional lines, and a first post with no "1/3" on it reads as a stray
    # tweet that happens to have replies.
    marker = "🧵 1/3"
    lead_optional = []
    if post.get("paragraphs"):
        # The first body paragraph is the who-and-what line; the second is the
        # evidence. The evidence is the hook.
        lead_optional.extend(post["paragraphs"][1:3])
    lead = _fit([post["headline"], ""], lead_optional,
                X_LIMIT - x_length(marker) - 2)
    posts.append(f"{lead}\n\n{marker}")

    # --- 2: the numbers ---
    stats_required = [f"{post['name']} — the numbers 2/3", ""]
    stats_optional = [f"• {row['label']}: {row['value']}"
                      for row in post.get("stats", [])]
    posts.append(_fit(stats_required, stats_optional, X_LIMIT))

    # --- 3: the verdict ---
    verdict = post["paragraphs"][-1] if post.get("paragraphs") else ""
    posts.append(_fit([f"So is {post['name']} worth it? 3/3", "", verdict],
                      [], X_LIMIT - x_length(tail)) + tail)
    return posts


def draft_player_reddit(post):
    """Markdown. Stands entirely on its own - there is no page behind this one
    to send anybody to, which makes the self-promotion rules easy to satisfy:
    the post IS the content and the only link is to a free player page."""
    out = [f"**{post['headline']}**", ""]
    for para in post.get("paragraphs", []):
        out += [para, ""]

    if post.get("stats"):
        out += ["**The numbers**", "", "| | |", "|---|---|"]
        for row in post["stats"]:
            out.append(f"| {row['label']} | {row['value']} |")
        out.append("")

    out += [f"Full page for {post['name']}, with fixtures and projections for "
            f"the next eight gameweeks: {_player_url(post)}", ""]
    out.append("Numbers are from FPL's own per-gameweek data; the projection "
               "is from a gradient-boosted model trained on it. Happy to be "
               "told where this is wrong.")
    out += ["", _player_question(post)]
    return "\n".join(out)


def _player_question(post):
    """A closing question matched to the angle, so it asks something the post
    has actually given the reader an opinion about."""
    name = post["name"]
    return {
        "injury_return": f"Anyone bringing {name} straight back in, or waiting to see the minutes first?",
        "unlucky": f"Is anyone holding {name} through this, or has the patience gone?",
        "regression": f"Anyone cashing in on {name} while the price is up?",
        "newly_nailed": f"Has anyone already got {name}, or is this too early?",
        "fixture_swing": f"Worth planning a transfer around {name}'s run, or is that too far ahead?",
        "unlucky_defence": f"Anyone in on that defence yet, or waiting for the clean sheet to actually arrive?",
        "preseason_form": f"Is anyone starting the season with {name}, or is last year's rate not enough to go on?",
        "price_watch": f"Anyone following the crowd on {name}, or is this one to fade?",
        "opening_fixtures": f"Worth starting with {name} for the fixtures alone, or is that not a good enough reason?",
    }.get(post.get("angle"), f"Anyone own {name}?")


def draft_player_discord(post):
    """Short enough for a channel: the claim, the numbers, the verdict."""
    paras = post.get("paragraphs") or []
    out = [f"**{post['headline']}**", ""]
    if len(paras) > 1:
        out += [paras[1], ""]
    if post.get("stats"):
        out.append(" · ".join(f"**{r['label']}:** {r['value']}"
                              for r in post["stats"][:4]))
        out.append("")
    if paras:
        out += [paras[-1], ""]
    out.append(f"<{_player_url(post)}>")
    return "\n".join(out)


def write_player_drafts(post):
    """One file per night's post.

    Named by date rather than by player, because the date is what the admin
    view lists them by and what retention deletes them by - and because two
    posts about the same player in one season would otherwise collide.
    """
    day = post["date"]
    thread = draft_player_x(post)
    thread_block = "\n\n".join(
        f"--- post {i} of {len(thread)}  ({x_length(p)} chars) ---\n{p}"
        for i, p in enumerate(thread, start=1))

    body = f"""FPL Buddy — player write-up for {day}
{'=' * 46}

PLAYER:  {post['name']} ({post.get('pos', '?')}, {post.get('team_name', '')})
ANGLE:   {post.get('angle_label', post.get('angle'))}
FOR:     Gameweek {post['gameweek']}

STATUS: READY — this is generated fresh each night and is not published
        anywhere public. Post it, edit it, or skip it; nothing depends on it.
        These are deleted once the Gameweek {post['gameweek']} roundup is
        written, so save anything you want to keep.

{'-' * 46}
X / TWITTER — THREAD
{'-' * 46}
{thread_block}

{'-' * 46}
REDDIT  (stands on its own — the only link is the player's own page)
{'-' * 46}
{draft_player_reddit(post)}

{'-' * 46}
DISCORD
{'-' * 46}
{draft_player_discord(post)}
"""

    path = os.path.join(players_dir(), f"{day}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def read_player_drafts(day):
    """One night's drafts, or None."""
    # The date goes into a path, so it is validated rather than trusted - this
    # is reachable from a URL. Anything that isn't YYYY-MM-DD is not a date and
    # is certainly not a filename.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
        return None
    path = os.path.join(players_dir(), f"{day}.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (FileNotFoundError, OSError):
        return None


def list_player_drafts():
    """Dates that have a player drafts file, newest first."""
    try:
        names = os.listdir(players_dir())
    except OSError:
        return []
    days = [m.group(1) for n in names
            for m in [re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.txt", n)] if m]
    return sorted(days, reverse=True)


def delete_player_drafts(days):
    """Remove the draft files for these dates. Returns how many went.

    Missing files are not an error: retention deletes the DB rows and the files
    together, and a file removed by hand should not make the job look failed.
    """
    removed = 0
    for day in days:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
            continue
        try:
            os.remove(os.path.join(players_dir(), f"{day}.txt"))
            removed += 1
        except (FileNotFoundError, OSError):
            continue
    return removed


# ---------------------------------------------------------------------------
#  Discord channel messages
#
#  Distinct from the drafts above, which are files written for a person to open
#  and copy from. These are pushed into a channel at the moment they become
#  relevant, and are written to be read on a phone.
#
#  Every one of them carries the phone URL to the full drafts rather than the
#  drafts themselves. Discord caps a message at 2000 characters and a Reddit
#  draft is routinely longer, so pushing the lot would mean truncating an
#  argument mid-sentence - the exact failure the drafts were rewritten to stop
#  doing.
# ---------------------------------------------------------------------------

def _drafts_link(path):
    """The capability URL for the full drafts, if one is configured.

    Rendered from FPL_SOCIAL_KEY at send time rather than stored. The key is a
    password and this string ends up in a Discord channel - which is a private
    channel you control, and is exactly what the key is for, but it is worth
    being deliberate about rather than incidental."""
    key = os.environ.get("FPL_SOCIAL_KEY", "").strip()
    return f"{_site_url()}/api/social/{key}{path}" if key else None


def channel_player_post(post):
    """Tonight's player write-up, for the drafts channel.

    The reason this feature works at all. Written to a file and left there, it
    depends on remembering to open a URL every morning; pushed to a channel it
    is a notification you read, copy and post. The Discord draft is already
    sized for this."""
    lines = [f"📝 **Tonight's player write-up** — {post['angle_label']}", "",
             draft_player_discord(post)]
    link = _drafts_link("/player/latest")
    if link:
        lines += ["", f"Full drafts (X thread, Reddit, Discord): <{link}>"]
    return "\n".join(lines)


def channel_quiet_night(day, gameweek, reason):
    """Said out loud when there is no write-up tonight.

    A silent channel and a broken cron look identical from a phone, and that is
    not a hypothetical: the nightly job ran for a week producing nothing, said
    so only in a log file on the host, and the first anyone knew of it was the
    write-up URL answering 404. One line a night costs nothing and closes that
    gap permanently.

    Deliberately not routed to `alerts`. Nothing has failed - the job worked and
    the pool was quiet - and putting a working night in the channel reserved for
    things that are broken is how that channel stops being read.
    """
    return "\n".join([
        f"🌙 **No player write-up for {day}**",
        "",
        reason,
        "",
        f"The Gameweek {gameweek} briefing is unaffected, and the job itself "
        "ran fine — this is the pool being quiet, not something being broken.",
    ])


def channel_briefing_ready(report):
    """The briefing, at the moment it becomes postable.

    Sent on the transition to `preview` and never again. That moment is about a
    day before the deadline, which is when managers are actually making
    transfers - the whole reason the preview stage exists."""
    gw = report["gameweek"]
    lines = [f"📣 **The Gameweek {gw} briefing is ready to post**"]
    if report.get("deadline_label"):
        lines.append(f"Deadline {report['deadline_label']} — about a day away, "
                     "which is when people are actually deciding.")
    lines += ["", draft_discord(report, stage="preview")]
    link = _drafts_link(f"/{gw}")
    if link:
        lines += ["", f"Full drafts: <{link}>"]
    return "\n".join(lines)


def channel_roundup_ready(roundup):
    """The roundup, when it is written. Always postable - it is built from
    settled results and never changes."""
    gw = roundup["gameweek"]
    lines = [f"📋 **The Gameweek {gw} roundup is up**", "",
             draft_roundup_discord(roundup)]
    link = _drafts_link("/roundup")
    if link:
        lines += ["", f"Full drafts: <{link}>"]
    return "\n".join(lines)


def channel_deadline_reminder(kind, gameweek, deadline_label, hours_left,
                              report=None):
    """A deadline reminder, for the gameweek channel.

    Two kinds doing two different jobs. The day-out one is a prompt to post the
    briefing; the final one is a prompt to check your own team, and says so -
    a reminder that doesn't tell you what to do with it is a notification you
    learn to swipe away.
    """
    when = deadline_label or f"in about {hours_left:.0f}h"
    if kind == "day":
        lines = [f"⏳ **Gameweek {gameweek} deadline is about a day away** — {when}",
                 "",
                 "The briefing is published and postable. This is the window "
                 "where a post actually reaches people before they've decided."]
        if report and report.get("captains"):
            c = report["captains"][0]
            lines += ["", f"Model's top captain pick: **{c['name']}** ({c['headline']})"]
    else:
        lines = [f"🚨 **Gameweek {gameweek} deadline in ~{hours_left:.0f}h** — {when}",
                 "",
                 "Last call for your own team. The AI squads commit inside the "
                 "final 100 minutes, on the latest team news."]
        if report and report.get("news"):
            flagged = ", ".join(p["name"] for p in report["news"][:5])
            lines += ["", f"🚑 Carrying a flag: {flagged}"]
    return "\n".join(lines)


# FPL's own chip codes, as the names a reader recognises. The codes are what
# the API uses and what ai_manager works in; "Chip played: bboost" is a line
# only somebody who has read this code can parse.
CHIP_NAMES = {
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
    "freehit": "Free Hit",
    "wildcard": "Wildcard",
}


def chip_name(chip):
    """A chip's display name, falling back to whatever FPL called it. A new
    chip - they have added them before - reads as its code rather than
    disappearing."""
    return CHIP_NAMES.get(chip, chip)


def channel_ai_squad(gameweek, manager=None, best_xi=None):
    """What the AI Manager did with its squad this week.

    The most interesting thing the site does and the only part of it that was
    never surfaced anywhere - the transfer reasoning went into a log file
    nobody reads. A bot that takes a -4 hit and says why is worth watching;
    one that silently produces a squad is not.
    """
    lines = [f"🤖 **AI squads committed for Gameweek {gameweek}**"]

    if manager:
        transfers = manager.get("transfers") or []
        if transfers:
            lines += ["", "**AI Manager transfers**"]
            for t in transfers:
                cost = "free" if t.get("free") else "−4 hit"
                lines.append(f"• {t['out']} → {t['in']} "
                             f"(+{t['gain']} projected, {cost})")
        else:
            lines += ["", "**AI Manager:** no transfers this week."]
        if manager.get("chip"):
            lines.append(f"🃏 Chip played: **{chip_name(manager['chip'])}**")
        if manager.get("predicted_points") is not None:
            lines.append(f"Projected: {manager['predicted_points']} "
                         f"({manager.get('formation', '')})")

    if best_xi and best_xi.get("predicted_points") is not None:
        lines += ["", f"**AI Best XI:** {best_xi['predicted_points']} projected, "
                      f"£{best_xi.get('squad_cost', 0)}m, "
                      f"{best_xi.get('formation', '')}"]

    lines += ["", f"<{_site_url()}/ai-teams>"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Ko-fi
# ---------------------------------------------------------------------------

# The longest donor name and message that get repeated into a channel. Both
# fields are typed by a stranger on a public form, so both are bounded here
# rather than trusted to be sensible.
KOFI_MAX_NAME = 80
KOFI_MAX_MESSAGE = 500


def _safe_text(value, limit):
    """Someone else's text, made safe to put in a Discord message.

    Three things happen here, and each one is a real case rather than a
    precaution:

      * Backticks and asterisks are stripped, because a donor message
        containing them breaks out of the quote block and can restyle the rest
        of the post.
      * Newlines are collapsed, so a message of forty blank lines cannot push
        everything else out of view.
      * It is truncated.

    Mentions are handled separately and more thoroughly - `ops._payload_for`
    sends `allowed_mentions: {"parse": []}` on every Discord message, so a
    donor calling themselves "@everyone" produces the text and no ping.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = " ".join(text.split())
    text = text.replace("`", "'").replace("*", "").replace("_", "")
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def channel_kofi(payload):
    """A Ko-fi donation, for the kofi channel.

    Deliberately does NOT include the donor's email address, which Ko-fi sends
    in every payload. It is personal data with no reason to be in a chat
    channel, the privacy policy makes promises about what this site stores, and
    a Discord message is a copy nobody is tracking the retention of.

    A message the donor marked private is not quoted. `is_public` is them
    saying whether it can be shown, and a private channel is still somewhere
    they didn't agree to - the donation is reported either way.
    """
    kind = str(payload.get("type") or "Donation")
    amount = _n(payload.get("amount"), 2)
    currency = str(payload.get("currency") or "").strip()
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    money = (f"{symbol}{amount}" if symbol and amount
             else f"{amount} {currency}".strip() if amount else "")

    name = _safe_text(payload.get("from_name"), KOFI_MAX_NAME) or "Someone"

    if payload.get("is_subscription_payment"):
        first = payload.get("is_first_subscription_payment")
        headline = ("☕ **New monthly supporter**" if first
                    else "☕ **Monthly support received**")
    elif kind.lower() == "shop order":
        headline = "🛍️ **Ko-fi shop order**"
    else:
        headline = "☕ **New Ko-fi donation**"
    if money:
        headline += f" — {money}"

    lines = [headline, f"From **{name}**"]

    note = _safe_text(payload.get("message"), KOFI_MAX_MESSAGE)
    if note and payload.get("is_public"):
        lines.append(f"> {note}")
    elif note:
        lines.append("_(they left a private message)_")

    url = str(payload.get("url") or "").strip()
    # Only Ko-fi's own domain. The URL arrives inside a payload from the
    # internet, and the token proves it came from Ko-fi rather than proving
    # every field in it is somewhere you want to be sent.
    if url.startswith("https://ko-fi.com/"):
        lines.append(f"<{url}>")

    return "\n".join(lines)


# The three points in an edition's life at which drafts get written, and what
# the header should tell you about posting them.
#
# The middle one is the one worth having. A briefing that is only postable once
# it's frozen is postable roughly an hour before kickoff, by which time the
# people it's for have already made their transfers - the day before a deadline
# is when they're deciding. So the edition is declared postable a day out, on a
# full rebuild against the freshest data, and then again as a final version
# after the last team news lands.
#
# The stage only ever moves forward: draft -> preview -> final. That matters
# because the nightly rebuild keeps running right up to the deadline, and
# without it Saturday's 03:15 run would quietly demote Friday's preview back to
# "don't post this yet" after you'd already posted it.
STAGES = ("draft", "preview", "final")

_STAGE_STATUS = {
    "draft": ("DRAFT — this edition is still being rebuilt nightly and will "
              "change before the deadline. Wait for the preview version, which "
              "lands about a day out."),
    "preview": ("PREVIEW — safe to post now. The deadline is about a day away, "
                "which is when most managers are actually making their "
                "transfers. The page can still pick up late team news, and a "
                "FINAL version of these drafts is written an hour or two before "
                "the deadline if you'd rather post once."),
    "final": ("FINAL — the edition is frozen on the last team news before the "
              "deadline and will not change again. Safe to post."),
}


def stage_of(report):
    """An edition's stage, defaulting to draft for anything written before this
    existed."""
    stage = (report or {}).get("stage")
    return stage if stage in STAGES else "draft"


def advance_stage(current, target):
    """The later of two stages. The one rule the pipeline needs: a rebuild can
    promote an edition but never demote one."""
    current = current if current in STAGES else "draft"
    target = target if target in STAGES else "draft"
    return current if STAGES.index(current) >= STAGES.index(target) else target


def write_drafts(report, stage="draft"):
    """One file per gameweek, holding all three drafts.

    Rewritten on every nightly rebuild, because a draft describing yesterday's
    version of the page is worse than no draft. The header states which stage
    the edition has reached, since posting a link to a page that will change
    out from under it is the one genuine mistake available here."""
    gw = report["gameweek"]
    stage = stage if stage in STAGES else "draft"
    status = _STAGE_STATUS[stage]
    deadline = report.get("deadline_label") or ""

    single = draft_x(report, stage)
    thread = draft_x_thread(report, stage)

    # Each post prints its own X-counted length, so an over-long one is visible
    # before it's pasted rather than after. `x_length` is what X shows, which is
    # not len() whenever the post carries a URL.
    thread_block = "\n\n".join(
        f"--- post {i} of {len(thread)}  ({x_length(p)} chars) ---\n{p}"
        for i, p in enumerate(thread, start=1))

    body = f"""FPL Buddy — Gameweek {gw} social drafts
{'=' * 46}

STATUS: {status}
{f'DEADLINE: {deadline}' if deadline else ''}

Edit anything that reads badly before posting. These are generated from
thresholds, so they are accurate but not always well-phrased.

{'-' * 46}
X / TWITTER — THREAD  (post these as replies to each other; the link is
              in the last one on purpose, see social.draft_x_thread)
{'-' * 46}
{thread_block}

{'-' * 46}
X / TWITTER — SINGLE POST  ({x_length(single)} chars, limit 280)
              (for a week where the thread is more than it deserves)
{'-' * 46}
{single}

{'-' * 46}
REDDIT  (r/FantasyPL — check the rules first; self-promo needs to be
         useful on its own, which is why the numbers are in the post)
{'-' * 46}
{draft_reddit(report, stage)}

{'-' * 46}
DISCORD  (paste into any FPL server you're actually a member of)
{'-' * 46}
{draft_discord(report, stage)}
"""

    path = os.path.join(social_dir(), f"gw{gw:02d}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def read_drafts(gameweek):
    """The drafts file for a gameweek, or None. Used by the /api/social route
    so you can read them without shelling into the box."""
    path = os.path.join(social_dir(), f"gw{int(gameweek):02d}.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (FileNotFoundError, OSError):
        return None


def list_drafts():
    """Gameweek numbers that have a drafts file, newest first."""
    try:
        names = os.listdir(social_dir())
    except OSError:
        return []
    gws = [int(m.group(1)) for n in names
           for m in [re.fullmatch(r"gw(\d+)\.txt", n)] if m]
    return sorted(gws, reverse=True)


# ---------------------------------------------------------------------------
#  Outreach
#
#  The weekly email to a handful of FPL writers. Same rule as everything else
#  in this file and for the same reason: the machine writes it, a person sends
#  it. Sending is left manual deliberately, and not out of caution -
#
#    * the personal read IS the mechanism. A merge field is visible from orbit,
#      and an obviously-generated round-robin is worth less than no email.
#    * deliverability. This domain also sends nothing else, and it is the one
#      serving the site; twenty near-identical messages a week from a young
#      domain is how it ends up spam-foldered, taking anything else with it.
#    * UK PECR treats sole traders and individuals as individuals, and most FPL
#      creators are one or the other. Fifteen hand-sent emails is a different
#      thing from an automated list, legally as well as practically.
#    * the recipient list is personal data. Keeping it in a text file the
#      author maintains, rather than in this app's database, keeps it out of
#      scope of everything the privacy policy has to describe.
#
#  So there is no send() here and there should not be one.
# ---------------------------------------------------------------------------

# Two picks, because three is a newsletter and one is a tip. Two reads as
# "here are the interesting bits", which is what it is.
OUTREACH_PICKS = 2


def outreach_picks(report, limit=OUTREACH_PICKS):
    """The players worth another writer's attention this week.

    Differentials first, then the armband, then form. That order is the whole
    editorial judgement in this function: a well-rated name at 3% ownership is
    something the recipient probably hasn't got, and the week's obvious captain
    is something they have already written about themselves. Sending someone
    their own headline back is how a "useful data" email reads as filler.

    Deduplicated on name, because a player can legitimately be both a
    differential and in form, and the same name twice in a three-bullet email
    looks like the generator ran twice."""
    out, seen = [], set()
    for section in ("differentials", "captains", "in_form"):
        for p in report.get(section) or []:
            name = p.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({**p, "section": section})
            if len(out) >= limit:
                return out
    return out


def outreach_miss(snapshot):
    """The frozen prediction that was most wrong last gameweek.

    This is the bullet that does the work. Two picks and a link is marketing;
    two picks and "it was badly wrong about this one, here is the number" is a
    person showing their working, and it is the only part of this email that a
    recipient could not have generated themselves from any other FPL site.

    Reads the SNAPSHOT, never today's ratings - the whole point is the number
    that was committed before kickoff, not what the model would say now that it
    has seen the result.

    Returns None when there is nothing honest to say: preseason, a gameweek
    that was never snapshotted, or one whose actuals haven't been backfilled
    yet. A missing bullet is fine; an invented one is not."""
    if not snapshot:
        return None
    worst, worst_gap = None, 0.0
    for p in snapshot.get("squad") or []:
        # Starters only. A bench player scoring 2 against a projection of 4 is
        # not a miss - the model never claimed he would play.
        if not p.get("starting"):
            continue
        predicted, actual = p.get("predicted"), p.get("actual_points")
        if predicted is None or actual is None:
            continue
        gap = float(predicted) - float(actual)
        if gap > worst_gap:
            worst, worst_gap = p, gap
    if worst is None:
        return None
    return {
        "gameweek": snapshot.get("gameweek"),
        "name": worst.get("web_name"),
        "predicted": float(worst["predicted"]),
        "actual": float(worst["actual_points"]),
        "gap": round(worst_gap, 1),
    }


def draft_outreach(report, snapshot=None):
    """The body of the weekly email, ready to paste.

    Deliberately has no greeting and no sign-off. Those are the two lines that
    have to be written by hand for each recipient, and generating a "Hi [name],"
    is an invitation to send it with the bracket still in it."""
    gw = report["gameweek"]
    url = _site_url()
    picks = outreach_picks(report)
    miss = outreach_miss(snapshot)

    # Counted, not assumed. This said "Three things" unconditionally, which was
    # wrong every week the miss bullet is absent - and wrong in the direction
    # that makes a generated email obvious, since the reader can see two.
    total = len(picks) + (1 if miss else 0)

    # A week with nothing in it is a week not to send. Saying so beats emitting
    # "0 things from this week's numbers" and leaving the sender to notice -
    # this happens for real in preseason, when there is no form to report and
    # the armband is deliberately withheld.
    if total == 0:
        return (f"[Nothing worth sending for GW{gw}. No differentials, no "
                f"armband, no form, and no settled gameweek to report a miss "
                f"from. This is normal in preseason. Skip this week - an email "
                f"that says nothing costs more than no email.]")

    words = {1: "One thing", 2: "Two things", 3: "Three things", 4: "Four things"}
    lines = [f"{words.get(total, f'{total} things')} from this week's "
             f"numbers, GW{gw}:", ""]

    n = 0
    for p in picks:
        n += 1
        facts = player_facts(p, include=("predicted", "owned", "cost"))
        # `reason` is the sentence the section already wrote for the page, so
        # the email and the site cannot disagree about why a player is listed.
        # Capitalised here because those are sentence FRAGMENTS written to sit
        # mid-sentence on the page ("projects above every midfielder..."), and
        # dropped after a full stop they read as a typo.
        why = (p.get("reason") or "").strip().rstrip(".")
        why = f" {why[0].upper()}{why[1:]}." if why else ""
        lines.append(f"{n}. {p['name']} ({p.get('pos', '')}, {p.get('team_name', '')})"
                     f" — {facts}.{why}")

    if miss:
        n += 1
        lines.append(f"{n}. Last week it was wrong about {miss['name']}: "
                     f"projected {miss['predicted']:.1f}, returned "
                     f"{miss['actual']:.0f}. That is the biggest miss in the "
                     f"GW{miss['gameweek']} squad, which was committed before "
                     f"the deadline and hasn't been edited since.")
    else:
        # Said out loud rather than silently dropped, so the sender notices the
        # email is a bullet short and knows why - rather than wondering whether
        # the generator broke.
        lines.append("")
        lines.append("[No miss to report yet - no settled gameweek with "
                     "backfilled actuals. Delete this line before sending, and "
                     "consider whether a two-bullet email is worth sending at "
                     "all this week.]")

    lines += ["", f"Full numbers: {url}", "",
              "Use any of it, no credit needed. Reply \"stop\" and I'll take "
              "you off this."]
    return "\n".join(lines)


def write_outreach_draft(report, snapshot=None):
    """One file per gameweek, beside the social drafts.

    Its own file rather than a fifth section inside gwNN.txt: that file is
    something you open on the way to posting publicly, and this is something
    you open on the way to emailing fifteen people. Different jobs, different
    weeks sometimes, and a reminder about the recipient list belongs next to
    the text it applies to."""
    gw = report["gameweek"]
    body = f"""FPL Buddy — Gameweek {gw} outreach email
{'=' * 46}

SEND BY HAND, to a list you maintain yourself. There is no send() in
social.py and there should not be one - see the comment above
draft_outreach() for the four reasons.

Before sending:
  * write the greeting and the first line per recipient, referencing
    something they actually published. That line is the difference
    between this and spam, and it is the one part no generator can write.
  * check the miss bullet is present. An email with only picks in it is
    a press release.
  * do not send in the 72 hours before a deadline unless the content is
    genuinely about that deadline - that is their busiest window.

{'-' * 46}
SUBJECT
{'-' * 46}
GW{gw} — model's picks, and last week's misses

{'-' * 46}
BODY  (paste under your own greeting)
{'-' * 46}
{draft_outreach(report, snapshot)}
"""
    path = os.path.join(social_dir(), f"outreach-gw{gw:02d}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path
