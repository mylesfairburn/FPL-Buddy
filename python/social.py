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

# Kept well inside X's 280 so an edited draft doesn't silently overflow. The
# URL counts as 23 characters there no matter how long it really is.
X_LIMIT = 240


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


def _truncate(text, limit):
    """Cut at a word boundary and mark it, so a trimmed draft never ends
    mid-word and it's obvious something was removed."""
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0] + "…"


def draft_x(report):
    """Short, one link, no hashtag spam. Two or three tags is what actually
    circulates in FPL; a wall of them reads as automated because it is."""
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    lines = [f"⚽ FPL Gameweek {gw} briefing"]

    if report.get("in_form"):
        names = ", ".join(p["name"] for p in report["in_form"][:3])
        lines.append(f"📈 In form: {names}")
    if report.get("differentials"):
        d = report["differentials"][0]
        lines.append(f"💎 Differential: {d['name']} ({d['headline']})")
    if report.get("attack_runs"):
        lines.append(f"📅 Best fixtures: {report['attack_runs'][0]['team_name']}")
    if report.get("news"):
        lines.append(f"🚑 {len(report['news'])} big-ownership fitness flags")

    body = "\n".join(lines)
    return f"{_truncate(body, X_LIMIT)}\n\n{url}\n\n#FPL #FantasyPL"


def draft_reddit(report):
    """Markdown, and written to stand on its own.

    The rule that gets link posts removed is that they send people away without
    saying anything. So the numbers are in the comment itself - someone who
    never clicks still got the information, and the link is a source rather
    than a toll gate."""
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    out = [f"**Gameweek {gw}: form, differentials and fixture swings**", ""]

    if report.get("in_form"):
        out.append("**In form**")
        for p in report["in_form"]:
            team = f", {p['team_name']}" if p.get("team_name") else ""
            out.append(f"- {p['name']} ({p['pos']}{team}) — {p['why']}")
        out.append("")
    if report.get("differentials"):
        out.append(f"**Differentials (under {5}% owned)**")
        for p in report["differentials"]:
            out.append(f"- {p['name']} ({p['pos']}) — {p['why']}")
        out.append("")
    if report.get("attack_runs"):
        out.append("**Kindest attacking fixtures**")
        for r in report["attack_runs"]:
            games = ", ".join(f["label"] for f in r["fixtures"][:4])
            out.append(f"- {r['team_name']}: {games}")
        out.append("")
    if report.get("defence_runs"):
        out.append("**Kindest defensive fixtures**")
        for r in report["defence_runs"]:
            games = ", ".join(f["label"] for f in r["fixtures"][:4])
            out.append(f"- {r['team_name']}: {games}")
        out.append("")
    if report.get("news"):
        out.append("**Fitness flags on widely-owned players**")
        for p in report["news"]:
            owned = f" ({p['owned']:.0f}% owned)" if p.get("owned") else ""
            out.append(f"- {p['name']}{owned} — {p['why']}")
        out.append("")

    out.append(f"Full page with the underlying numbers: {url}")
    out.append("")
    out.append("Ratings come from a points model trained on per-gameweek data. "
               "Happy to explain any of the numbers.")
    return "\n".join(out)


def draft_discord(report):
    """Between the two: short enough to read in a channel, no link-post rules
    to satisfy, and Discord renders the same Markdown."""
    gw = report["gameweek"]
    url = f"{_site_url()}/gameweek/{gw}"
    out = [f"**⚽ FPL Gameweek {gw} briefing**", ""]
    if report.get("in_form"):
        out.append("**📈 In form:** " + ", ".join(
            f"{p['name']} ({p['headline']})" for p in report["in_form"]))
    if report.get("differentials"):
        out.append("**💎 Differentials:** " + ", ".join(
            f"{p['name']} ({p['headline']})" for p in report["differentials"]))
    if report.get("attack_runs"):
        out.append("**📅 Best attacking fixtures:** " + ", ".join(
            r["team_name"] for r in report["attack_runs"]))
    if report.get("news"):
        out.append("**🚑 Flagged:** " + ", ".join(p["name"] for p in report["news"]))
    out.append("")
    out.append(f"<{url}>")
    return "\n".join(out)


def write_drafts(report, frozen=False):
    """One file per gameweek, holding all three drafts.

    Rewritten on every nightly rebuild, because a draft describing yesterday's
    version of the page is worse than no draft. The header states whether the
    edition is final, since posting a link to a page that will change before
    the deadline is the one genuine mistake available here."""
    gw = report["gameweek"]
    status = ("FINAL — the deadline has passed and this page will not change. "
              "Safe to post." if frozen else
              "DRAFT — this edition is still being rebuilt nightly and will "
              "change before the deadline. Wait for the frozen version before "
              "posting, unless you're posting deliberately early.")

    body = f"""FPL Buddy — Gameweek {gw} social drafts
{'=' * 46}

STATUS: {status}

Edit anything that reads badly before posting. These are generated from
thresholds, so they are accurate but not always well-phrased.

{'-' * 46}
X / TWITTER  ({len(draft_x(report))} chars, limit 280)
{'-' * 46}
{draft_x(report)}

{'-' * 46}
REDDIT  (r/FantasyPL — check the rules first; self-promo needs to be
         useful on its own, which is why the numbers are in the post)
{'-' * 46}
{draft_reddit(report)}

{'-' * 46}
DISCORD  (paste into any FPL server you're actually a member of)
{'-' * 46}
{draft_discord(report)}
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
