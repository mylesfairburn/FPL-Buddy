"""Pure-function unit tests.

Everything here runs without the app, a network call or a database - these are
the functions whose output ends up in a URL, a page title or a sentence a
reader sees, so they're worth pinning down precisely.
"""

import json
import os
import re
import tempfile
from datetime import date

import joblib
import numpy
import pandas as pd

import ai_manager
import ai_team
import chip_model
import drafts
import gw_report as gwr
import gw_roundup as gwru
import kits
import player_pages as pp
import player_spotlight as ps
import fixture_rotator
import fixture_structure
import rating_model
import seo_tables
import seasons
import social
import team_service
import train_model as tm
from harness import check, expect, group, safe


def test_slugify():
    group("slugify", "high")

    cases = [
        # (input, expected slug, why it matters)
        ("Bukayo Saka", "bukayo-saka", "the ordinary case"),
        ("Gabriel Jesus", "gabriel-jesus", "two words"),
        ("Guéhi", "guehi", "acute accent folds to ASCII"),
        ("Ødegaard", "odegaard", "Ø has no NFKD decomposition"),
        ("Højbjerg", "hojbjerg", "ø mid-word"),
        ("Weiß", "weiss", "ß expands to two letters"),
        ("Łukasz", "lukasz", "Ł has no decomposition"),
        ("Đorđe", "dorde", "Đ has no decomposition"),
        ("Æneas", "aeneas", "Æ expands"),
        ("N'Golo Kanté", "n-golo-kante", "apostrophe becomes a separator"),
        ("Alexander-Arnold", "alexander-arnold", "existing hyphen survives"),
        ("  spaced  out  ", "spaced-out", "leading/trailing space stripped"),
        ("O'Brien-Smith Jr.", "o-brien-smith-jr", "trailing punctuation trimmed"),
        ("...", "", "punctuation only collapses to empty"),
        ("", "", "empty string"),
        (None, "", "None must not raise"),
        ("ALL CAPS", "all-caps", "lowercased"),
        ("van Dijk", "van-dijk", "lowercase particle"),
        ("Sánchez", "sanchez", "acute on a vowel"),
        ("Müller", "muller", "umlaut drops rather than expanding to ue"),
        ("a" * 200, "a" * 200, "long name is not truncated"),
        ("Player 7", "player-7", "digits survive"),
        ("<script>alert(1)</script>", "script-alert-1-script", "HTML cannot survive slugify"),
        ("../../etc/passwd", "etc-passwd", "traversal cannot survive slugify"),
    ]
    for text, want, why in cases:
        expect(f"slugify({text!r}) — {why}", repr(text), want, pp.slugify(text))

    # The property that actually protects the URL space, stated directly.
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
    for text, _, _ in cases:
        out = pp.slugify(text)
        check(f"slugify output charset for {text!r}", repr(text),
              "only [a-z0-9-]", out,
              lambda s: all(c in alphabet for c in s), severity="high")


def test_article():
    group("prose helpers", "low")
    for word, want in [("Arsenal", "an"), ("Everton", "an"), ("Ipswich", "an"),
                       ("Aston Villa", "an"), ("Chelsea", "a"), ("Liverpool", "a"),
                       ("Wolves", "a"), ("", "a"), (None, "a")]:
        expect(f"_article({word!r})", repr(word), want, pp._article(word))


def test_fmt_and_plural():
    group("prose helpers", "low")
    expect("_fmt(3.0, 0)", "3.0, dp=0", "3", pp._fmt(3.0, 0))
    expect("_fmt(3.456, 1)", "3.456, dp=1", "3.5", pp._fmt(3.456, 1))
    expect("_fmt(1234.0, 0) separates thousands", "1234.0, dp=0", "1,234",
           pp._fmt(1234.0, 0))
    check("_fmt(None) is falsy or dash", "None", "no crash, no 'nan'",
          safe(pp._fmt, None), lambda v: "nan" not in str(v).lower())
    check("_fmt(nan) degrades gracefully", "float('nan')",
          "None or a dash — never a crash, never the text 'nan'",
          safe(pp._fmt, float("nan")),
          lambda v: not str(v).startswith("ValueError")
          and "nan" not in str(v).lower(),
          severity="medium",
          note="225 of the pool's predicted_points are NaN, so a NaN reaching "
               "prose formatting is not hypothetical")
    check("_fmt(nan, 1) degrades gracefully", "float('nan'), dp=1",
          "no 'nan' in output", safe(pp._fmt, float("nan"), 1),
          lambda v: "nan" not in str(v).lower(), severity="medium")
    expect("_plural(1, 'goal')", "1", "goal", pp._plural(1, "goal"))
    expect("_plural(2, 'goal')", "2", "goals", pp._plural(2, "goal"))
    expect("_plural(0, 'goal')", "0", "goals", pp._plural(0, "goal"))


def test_fixture_label():
    group("prose helpers", "low")
    check("fixture_label returns a string for a normal fixture",
          "{'opponent': 'ARS', 'is_home': True, 'difficulty': 3}",
          "non-empty string",
          pp.fixture_label({"opponent": "ARS", "is_home": True, "difficulty": 3}),
          lambda v: isinstance(v, str) and len(v) > 0)
    check("fixture_label tolerates a blank fixture", "{}", "no exception",
          pp.fixture_label({}), lambda v: isinstance(v, str))


def test_describe_zero_minutes():
    """A player with no minutes, before and after the first deadline.

    These are two different facts and the page shipped one sentence for both,
    which was correct all summer and then wrong on several hundred pages from
    the moment GW1 kicked off. Nothing caught it because nothing asserted on
    the sentence - only that the prose contained no 'nan'. So: assert on the
    claim itself, in both directions."""
    group("player page prose", "high")

    def rec(prev_minutes=0):
        return {
            "full_name": "Illan Meslier", "web_name": "Meslier",
            "team_name": "Leeds", "pos_name": "goalkeeper", "cost": 5.0,
            "stats": {"minutes": 0, "total_points": 0,
                      "selected_by_percent": 0.0},
            "prev_season": ({"minutes": prev_minutes, "goals_scored": 0}
                            if prev_minutes else {}),
            "next_gameweeks": [],
        }

    for prev, why in ((2000, "with last season to fall back on"),
                      (0, "with no history at all")):
        started = " ".join(pp.describe(rec(prev), "2026-27", "2026-27", True))
        check(f"season under way, no minutes {why} — does not claim the "
              "season is unstarted", f"prev_minutes={prev}, season_started=True",
              "says the player has not featured, not that the season has not begun",
              started,
              lambda s: "season hasn't started" not in s
              and "has not played a minute" in s,
              severity="high",
              note="wrong from the first deadline onwards, on every player who "
                   "has yet to appear - roughly half the pool")

        preseason = " ".join(pp.describe(rec(prev), "2026-27", "2026-27", False))
        check(f"preseason, no minutes {why} — still says so",
              f"prev_minutes={prev}, season_started=False",
              "the preseason sentence is unchanged", preseason,
              lambda s: "season hasn't started" in s,
              severity="medium")

    # The fallback tail is the same either side of the deadline, and it is the
    # only place last season's numbers appear for a player with no minutes.
    with_prev = " ".join(pp.describe(rec(2000), "2026-27", "2026-27", True))
    check("last season's totals survive the rewrite", "prev_minutes=2000",
          "'Last season: 2,000 minutes' appears", with_prev,
          lambda s: "Last season: 2,000 minutes" in s, severity="medium")


def test_a_to_z_grouping():
    group("A-Z index", "medium")
    def rec(code, web_name, full_name, slug, pos):
        return {"code": code, "web_name": web_name, "full_name": full_name,
                "name": full_name, "slug": slug, "path": f"/player/{slug}",
                "pos": pos, "team_name": "Arsenal", "cost": 5.0}

    pages = {
        1: rec(1, "Saka", "Bukayo Saka", "bukayo-saka-1", "MID"),
        2: rec(2, "Ødegaard", "Martin Ødegaard", "martin-odegaard-2", "MID"),
        3: rec(3, "Alexander-Arnold", "Trent Alexander-Arnold",
               "trent-alexander-arnold-3", "DEF"),
    }
    groups = pp.a_to_z(pages)
    letters = [g[0] if isinstance(g, (list, tuple)) else g.get("letter") for g in groups]
    check("every player lands in a group", str(list(pages)),
          "3 players across the groups", groups,
          lambda gs: sum(len(g[1] if isinstance(g, (list, tuple)) else g["players"])
                         for g in gs) == 3)
    check("group keys are single A-Z letters", str(letters),
          "each key is one letter A-Z", letters,
          lambda ls: all(isinstance(x, str) and len(x) == 1 and x.isalpha() for x in ls))
    check("accented surname files under the folded letter",
          "Ødegaard", "filed under O not Ø", letters,
          lambda ls: "O" in [x.upper() for x in ls])


def test_draft_validation():
    """The only endpoint that writes user-supplied data. Its validator is the
    whole of the trust boundary, so it gets tested hard."""
    group("draft validation", "high")

    def squad(n=15, **over):
        picks = [{"element_id": 100 + i, "position": i + 1} for i in range(n)]
        for i, extra in over.get("patch", {}).items():
            picks[i].update(extra)
        return picks

    ok_picks = squad()
    check("a valid 15-man squad is accepted", "15 unique players, positions 1-15",
          "returns 15 cleaned picks", drafts.validate_picks(ok_picks),
          lambda v: isinstance(v, list) and len(v) == 15)

    rejects = [
        ("picks is not a list", "'nope'", "nope"),
        ("picks is None", "None", None),
        ("picks is a dict", "{}", {}),
        ("14 players", "14 picks", squad(14)),
        ("16 players", "16 picks", squad(16)),
        ("0 players", "[]", []),
        ("a pick is not an object", "['x'] * 15", ["x"] * 15),
        ("a pick has no position", "15 picks, one missing position",
         [{"element_id": 1}] * 15),
        ("position 0", "position 0 present",
         squad(patch={0: {"position": 0}})),
        ("position 16", "position 16 present",
         squad(patch={0: {"position": 16}})),
        ("negative position", "position -1",
         squad(patch={0: {"position": -1}})),
        ("duplicate position", "two picks in position 1",
         squad(patch={1: {"position": 1}})),
        ("duplicate player", "same element_id twice",
         squad(patch={1: {"element_id": 100}})),
        ("non-numeric element id", "element_id 'abc'",
         squad(patch={0: {"element_id": "abc"}})),
        ("two captains", "is_captain on two picks",
         squad(patch={0: {"is_captain": True}, 1: {"is_captain": True}})),
        ("two vice-captains", "is_vice_captain on two picks",
         squad(patch={0: {"is_vice_captain": True}, 1: {"is_vice_captain": True}})),
        ("oversized payload", "20,000 picks", squad(20000)),
    ]
    for name, desc, payload in rejects:
        try:
            drafts.validate_picks(payload)
            got, ok = "accepted", False
        except drafts.DraftError as e:
            got, ok = f"DraftError: {e}", True
        except Exception as e:
            # A non-DraftError escapes as a 500 rather than a 400, so the client
            # gets a server error for what is really a bad request.
            got, ok = f"{type(e).__name__}: {e} (not a DraftError)", False
        check(f"rejects: {name}", desc, "DraftError", got, lambda _: ok,
              severity="high")

    # Coercion, not rejection: these are valid but sloppy, and should be cleaned.
    cleaned = drafts.validate_picks(squad(patch={0: {"is_captain": 1, "cost": "5.5"}}))
    check("captain flag normalised to 0/1", "is_captain=1",
          "int 0 or 1", cleaned[0]["is_captain"], lambda v: v in (0, 1))
    check("cost coerced to float", "cost='5.5'", "float 5.5",
          cleaned[0]["cost"], lambda v: isinstance(v, float) and abs(v - 5.5) < 1e-9)
    check("missing cost becomes None rather than raising", "no cost key",
          "None", cleaned[1]["cost"], lambda v: v is None)

    # SQL metacharacters in a field that reaches the database.
    inj = squad(patch={0: {"element_id": "1); DROP TABLE manager_draft;--"}})
    try:
        drafts.validate_picks(inj)
        got, ok = "accepted — string reached the DB layer", False
    except drafts.DraftError:
        got, ok = "DraftError", True
    except Exception as e:
        got, ok = f"{type(e).__name__}: {e}", False
    check("SQL in element_id is rejected at the validator",
          "element_id = \"1); DROP TABLE manager_draft;--\"",
          "DraftError (int() refuses it)", got, lambda _: ok, severity="critical")


def test_storage_kind():
    """The mount classifier behind /api/ai/status.

    The real /proc/self/mountinfo only exists on Linux, and the case that
    matters - an anonymous Docker volume - can't be produced on a dev machine
    at all. So the classification is exercised against captured mountinfo
    lines, which is the part that would actually be got wrong.
    """
    group("storage detection", "high")
    import db

    # Field 4 is the host-side root, field 5 the mount point. Real lines,
    # trimmed to the fields the parser reads.
    ANON = "a" * 64
    lines = {
        "bind mount from an explicit -v":
            (f"421 380 259:2 /srv/fpl-companion/state /app/state rw,relatime "
             f"- ext4 /dev/nvme0n1p2 rw", "bind"),
        "named docker volume":
            ("421 380 259:2 /var/lib/docker/volumes/fpl_state/_data /app/state "
             "rw,relatime - ext4 /dev/nvme0n1p2 rw", "volume"),
        "anonymous docker volume":
            (f"421 380 259:2 /var/lib/docker/volumes/{ANON}/_data /app/state "
             f"rw,relatime - ext4 /dev/nvme0n1p2 rw", "anon"),
    }
    for label, (line, want) in lines.items():
        fields = line.split()
        source = fields[3]
        if db._ANON_VOLUME.search(source):
            got = "anon"
        else:
            got = "volume" if "/volumes/" in source else "bind"
        expect(f"classifies {label}", source, want, got, severity="high",
               note="'anon' is the silent data-loss case: a fresh volume on "
                    "every docker run, indistinguishable from a correct mount "
                    "by path alone")

    # The id length is the whole distinction between anon and named, so pin it.
    check("a 63-char id is not treated as anonymous", "63 hex chars",
          "no match", db._ANON_VOLUME.search(f"/volumes/{'a' * 63}/_data"),
          lambda m: m is None)
    check("a 64-char id is treated as anonymous", "64 hex chars", "match",
          db._ANON_VOLUME.search(f"/volumes/{'a' * 64}/_data"),
          lambda m: m is not None)
    check("a readable name is not anonymous", "/volumes/fpl_state/_data",
          "no match", db._ANON_VOLUME.search("/volumes/fpl_state/_data"),
          lambda m: m is None)

    # Never raises, whatever it is handed - it sits inside a health endpoint.
    for arg in [None, "/nonexistent/path/x.db", "", "relative.db"]:
        check(f"storage_kind({arg!r}) never raises", repr(arg),
              "a verdict string, not an exception", safe(db.storage_kind, arg),
              lambda v: v in ("bind", "volume", "anon", "image", "unknown"),
              severity="high")


def test_horizon_points():
    """Returns the total predicted points over the next n gameweeks, or None."""
    group("horizon points", "medium")

    ten = {"next_gameweeks": [{"event": g, "points": 4.0} for g in range(1, 11)]}
    expect("sums only the first n gameweeks", "10 gameweeks of 4.0 points, n=8",
           32.0, safe(pp.horizon_points, ten, 8))
    expect("n larger than the fixture list", "10 gameweeks of 4.0, n=20",
           40.0, safe(pp.horizon_points, ten, 20))
    expect("empty fixture list gives None", "{'next_gameweeks': []}", None,
           safe(pp.horizon_points, {"next_gameweeks": []}))
    expect("all-None points gives None", "3 gameweeks with points=None", None,
           safe(pp.horizon_points, {"next_gameweeks": [{"points": None}] * 3}))
    check("a record with no fixture key degrades gracefully", "{}",
          "None rather than KeyError", safe(pp.horizon_points, {}),
          lambda v: not str(v).startswith("KeyError"), severity="low",
          note="every record from build_index has the key, so this is "
               "defensive rather than a live fault")




def _fake_player(**over):
    """A page record shaped like the ones build_index produces.

    Written out rather than pulled from a fixture so each test can state the
    one field it cares about and the defaults stay obviously boring."""
    rec = {
        "code": 1, "web_name": "Player", "full_name": "A Player",
        "path": "/player/a-player-1", "pos": "MID", "team_code": 3,
        "team_name": "Arsenal", "team_short": "ARS", "cost": 7.0,
        "status": "a", "chance_of_playing_next_round": None, "news": "",
        "next_gameweeks": [{"event": 5, "points": 5.0, "opponent": "CHE",
                            "was_home": True}],
        "stats": {"minutes": 900, "form": 6.0, "selected_by_percent": 2.0,
                  "expected_goal_involvements": 4.0, "goals_scored": 3,
                  "assists": 2},
    }
    stats = over.pop("stats", None)
    rec.update(over)
    if stats:
        rec["stats"] = {**rec["stats"], **stats}
    return rec


def test_gw_report_predicted_for():
    group("gw_report projections", "high")

    # The key is `points`. Reading `predicted_points` here - the name the same
    # number goes by in the API payload - returns None for everyone and empties
    # two sections without raising, so this pins the field name down.
    rec = _fake_player()
    expect("reads the `points` key", "next_gameweeks[{event:5, points:5.0}]",
           5.0, safe(gwr._predicted_for, rec, 5), severity="high")

    # Matching on `event` rather than taking the first entry. A report built
    # after a round completes would otherwise print next week's projection.
    multi = _fake_player(next_gameweeks=[
        {"event": 6, "points": 9.9}, {"event": 7, "points": 1.1}])
    expect("matches the requested gameweek, not the first entry",
           "events 6 and 7, asking for 7", 1.1,
           safe(gwr._predicted_for, multi, 7), severity="critical",
           note="the bug the AI squads already had to fix once")
    expect("a gameweek with no fixture gives None", "events 6 and 7, asking for 9",
           None, safe(gwr._predicted_for, multi, 9))
    expect("an empty fixture list gives None", "next_gameweeks=[]", None,
           safe(gwr._predicted_for, _fake_player(next_gameweeks=[]), 5))


def test_gw_report_availability():
    group("gw_report availability", "high")

    expect("a fit player is recommendable", "status='a', chance=None",
           True, safe(gwr._is_available, _fake_player()))
    expect("an injured player is not", "status='i'", False,
           safe(gwr._is_available, _fake_player(status="i")), severity="high")
    expect("a doubt is not", "status='a', chance=75", False,
           safe(gwr._is_available, _fake_player(chance_of_playing_next_round=75)),
           severity="high",
           note="a recommendation carrying an asterisk is worse than none")
    expect("100% chance is still recommendable", "status='a', chance=100",
           True, safe(gwr._is_available, _fake_player(chance_of_playing_next_round=100)))


def test_gw_report_sections():
    group("gw_report sections", "high")

    # Preseason FPL reports every player at 0.0 form. Without the >0 guard the
    # In Form section ranks a field of ties and prints "on 0.0 form".
    flat = {i: _fake_player(code=i, stats={"form": 0.0}) for i in range(5)}
    expect("zero form is not 'in form'", "5 players all on 0.0 form",
           0, len(safe(gwr.in_form, flat, 5) or []), severity="high")

    ranked = {1: _fake_player(code=1, web_name="Low", stats={"form": 2.0}),
              2: _fake_player(code=2, web_name="High", stats={"form": 8.0}),
              3: _fake_player(code=3, web_name="Mid", stats={"form": 5.0})}
    names = [p["name"] for p in (safe(gwr.in_form, ranked, 5) or [])]
    expect("in form is ranked by form, highest first", "forms 2.0/8.0/5.0",
           ["High", "Mid", "Low"], names, severity="high")

    thin = {1: _fake_player(code=1, stats={"minutes": 20, "form": 9.0})}
    expect("a player with almost no minutes is excluded", "20 minutes, 9.0 form",
           0, len(safe(gwr.in_form, thin, 5) or []),
           note="two substitute appearances shouldn't set the form table")

    # Differentials: the ownership ceiling and the projection floor both bite.
    owned = {1: _fake_player(code=1, stats={"selected_by_percent": 40.0})}
    expect("a widely-owned player is not a differential", "40% owned", 0,
           len(safe(gwr.differentials, owned, 5) or []), severity="high")
    poor = {1: _fake_player(code=1, stats={"selected_by_percent": 1.0},
                            next_gameweeks=[{"event": 5, "points": 0.4}])}
    expect("a low-owned player the model doesn't rate is not a differential",
           "1% owned, 0.4 projected", 0,
           len(safe(gwr.differentials, poor, 5) or []),
           note="otherwise this section is just the cheapest reserves in the league")
    good = {1: _fake_player(code=1, stats={"selected_by_percent": 1.0})}
    expect("a low-owned player the model rates is", "1% owned, 5.0 projected",
           1, len(safe(gwr.differentials, good, 5) or []), severity="high")

    # Team news is filtered by ownership, not by severity.
    quiet = {1: _fake_player(code=1, status="i",
                             stats={"selected_by_percent": 1.0})}
    expect("an injury to a barely-owned player is not news", "1% owned, injured",
           0, len(safe(gwr.team_news, quiet, 5) or []))
    loud = {1: _fake_player(code=1, status="i",
                            stats={"selected_by_percent": 45.0})}
    expect("an injury to a widely-owned player is", "45% owned, injured", 1,
           len(safe(gwr.team_news, loud, 5) or []), severity="high")


def test_gw_report_captains():
    group("gw_report armband", "high")

    pool = {1: _fake_player(code=1, web_name="Top",
                            next_gameweeks=[{"event": 5, "points": 8.0}]),
            2: _fake_player(code=2, web_name="Mid",
                            next_gameweeks=[{"event": 5, "points": 6.0}]),
            3: _fake_player(code=3, web_name="Low",
                            next_gameweeks=[{"event": 5, "points": 2.0}])}
    picks = safe(gwr.captain_picks, pool, 5) or []
    expect("the armband ranks on projection, highest first",
           "projections 8.0/6.0/2.0", ["Top", "Mid", "Low"],
           [p["name"] for p in picks], severity="high")
    expect("the leader is 0.0 behind itself", "top pick", 0.0,
           picks[0]["behind_leader"] if picks else None)
    expect("the gap to the leader is stated", "6.0 against a leading 8.0", 2.0,
           picks[1]["behind_leader"] if len(picks) > 1 else None,
           severity="high",
           note="the gap is what tells a reader whether it's a ranking or a tie")

    # Unlike every other section, ownership breaks ties DOWNWARDS in rank -
    # the more-owned of two equal projections is the safer armband.
    tied = {1: _fake_player(code=1, web_name="Rare",
                            stats={"selected_by_percent": 2.0},
                            next_gameweeks=[{"event": 5, "points": 7.0}]),
            2: _fake_player(code=2, web_name="Popular",
                            stats={"selected_by_percent": 50.0},
                            next_gameweeks=[{"event": 5, "points": 7.0}])}
    expect("ownership breaks a tie towards the safer pick",
           "both projected 7.0, owned 2% and 50%", "Popular",
           (safe(gwr.captain_picks, tied, 5) or [{}])[0].get("name"),
           severity="medium")

    # A flagged player must never be offered as a captain pick - it is the
    # single most expensive recommendation on the page to get wrong.
    hurt = {1: _fake_player(code=1, web_name="Hurt", status="i",
                            next_gameweeks=[{"event": 5, "points": 9.9}])}
    expect("a flagged player is never a captain pick", "injured, 9.9 projected",
           0, len(safe(gwr.captain_picks, hurt, 5) or []), severity="critical")


def test_gw_report_build():
    group("gw_report assembly", "high")

    pages = {1: _fake_player(code=1)}
    report = safe(gwr.build, pages, 5)
    check("build returns every expected section", "one fit, in-form player",
          "all six section keys present", report,
          lambda r: isinstance(r, dict) and all(
              k in r for k in ("gameweek", "captains", "in_form", "differentials",
                               "attack_runs", "defence_runs", "news", "summary")),
          severity="high")

    # The whole edition is stored as JSON, so anything not serialisable - a
    # numpy float from pandas, a Timestamp - breaks the save rather than the
    # page, and does it in a cron job nobody is watching.
    check("the report is JSON-serialisable", "build() output",
          "json.dumps succeeds", report,
          lambda r: isinstance(json.dumps(r), str), severity="critical",
          note="it is written to SQLite as a JSON string by a nightly job")

    empty = safe(gwr.build, {}, 5)
    check("an empty pool still produces a usable edition", "no players at all",
          "a summary explaining the page is thin", empty,
          lambda r: isinstance(r, dict) and len(r.get("summary", "")) > 40,
          note="normal preseason; must not look like a failed job")


def _roundup_pages(*specs):
    """A page index keyed on `code`, as player_pages.build_index returns."""
    pages = {}
    for element_id, code, name, pos, owned in specs:
        pages[code] = {
            "id": element_id, "code": code, "web_name": name, "full_name": name,
            "path": f"/player/{name.lower()}-{code}", "pos": pos, "team": 1,
            "team_code": 3, "team_name": "Arsenal", "team_short": "ARS",
            "cost": 7.5, "stats": {"selected_by_percent": owned},
        }
    return pages


def _live(points, minutes=90, **over):
    stats = {"total_points": points, "minutes": minutes, "goals_scored": 0,
             "assists": 0, "bonus": 0, "bps": 10, "clean_sheets": 0,
             "saves": 0, "expected_goals": 0.0, "expected_assists": 0.0,
             "expected_goal_involvements": 0.0}
    stats.update(over)
    return stats


def _fixture_rows(results):
    """{event: [(home, away, home_score, away_score), ...]} -> a fixtures frame."""
    import pandas as pd
    rows, fid = [], 0
    for event, games in sorted(results.items()):
        for home, away, hs, as_ in games:
            fid += 1
            rows.append({"id": fid, "event": event, "finished": True,
                         "team_h": home, "team_a": away,
                         "team_h_score": hs, "team_a_score": as_})
    return pd.DataFrame(rows)


def test_roundup_players():
    group("gw_roundup player sections", "high")

    pages = _roundup_pages((1, 101, "Top", "FWD", 50.0),
                           (2, 102, "Next", "MID", 20.0),
                           (3, 103, "Cameo", "MID", 40.0),
                           (4, 104, "Cheap", "DEF", 1.0))
    by_id = gwru.by_id(pages)
    expect("the index is re-keyed on FPL id, not code", "by_id()",
           [1, 2, 3, 4], sorted(by_id), severity="high",
           note="the live endpoint reports on id; the pages are keyed on code")

    live = {1: _live(16, goals_scored=2, assists=1, bonus=3),
            2: _live(9, goals_scored=1),
            3: _live(1, minutes=12),
            4: _live(2)}
    scorers = safe(gwru.top_scorers, by_id, live) or []
    expect("top scorers rank on points", "16/9/2/1 points",
           ["Top", "Next", "Cheap"], [p["name"] for p in scorers],
           severity="high")
    check("the top scorer's sentence restates its own numbers",
          "top_scorers()[0].why", "names goals, assists, bonus and minutes",
          scorers[0]["why"] if scorers else "",
          lambda s: "2 goals" in s and "1 assist" in s and "3 bonus" in s
                    and "90 minutes" in s, severity="high")

    # A blank only counts if he was on the pitch long enough for it to be his,
    # and only if enough people own him for it to be news.
    blanks = safe(gwru.underperformers, by_id, live) or []
    expect("a 12-minute cameo is not an underperformer", "1 point, 12 minutes",
           False, any(p["name"] == "Cameo" for p in blanks), severity="high")
    expect("a barely-owned blank is not news", "2 points, 1% owned", False,
           any(p["name"] == "Cheap" for p in blanks))

    hurt = {2: _live(1, expected_goal_involvements=0.93)}
    unlucky = safe(gwru.underperformers, by_id, hurt) or []
    check("a blank with good underlying numbers is called unlucky",
          "1 point from 0.93 xGI", "the sentence says the underlying was better",
          unlucky[0]["why"] if unlucky else "",
          lambda s: "better than the score" in s and "0.93" in s,
          severity="high",
          note="'blanked' and 'blanked from 0.9 xGI' lead to opposite decisions")
    flat = {2: _live(1, expected_goal_involvements=0.02)}
    check("a blank with nothing behind it is not", "1 point from 0.02 xGI",
          "the sentence says there was little behind it",
          (safe(gwru.underperformers, by_id, flat) or [{}])[0].get("why", ""),
          lambda s: "little behind it" in s, severity="medium")


def test_roundup_table_and_shocks():
    group("gw_roundup table and shocks", "high")

    # Six rounds between four clubs, ordered 1 > 2 > 3 > 4 by design.
    results = {1: [(1, 2, 3, 0), (3, 4, 2, 1)], 2: [(2, 3, 1, 0), (4, 1, 0, 2)],
               3: [(1, 3, 2, 0), (2, 4, 3, 1)], 4: [(3, 1, 0, 1), (4, 2, 1, 1)],
               5: [(1, 4, 4, 0), (2, 3, 2, 2)]}
    fixtures = _fixture_rows(results)
    table = safe(gwru.league_table, fixtures) or {}
    expect("three points for a win", "club 1: 5 wins from 5", 15,
           table.get(1, {}).get("points"), severity="high")
    expect("goal difference is goals for minus against", "club 1",
           table.get(1, {}).get("gf", 0) - table.get(1, {}).get("ga", 0),
           table.get(1, {}).get("gd"))
    expect("the table is ordered on points then GD", "four clubs", 1,
           table.get(1, {}).get("position"), severity="high")

    # up_to_event is what stops the section being circular: the win being
    # judged has already moved both clubs by the time the round is over.
    partial = safe(gwru.league_table, fixtures, 2) or {}
    expect("up_to_event bounds the table", "rounds 1-2 only", 2,
           partial.get(1, {}).get("played"), severity="high")

    names = {1: "Alpha", 2: "Beta", 3: "Gamma", 4: "Delta"}
    # A five-round table is thin but present; the gap threshold is what bites
    # in a four-club league, so this checks the guard rather than a real shock.
    young = _fixture_rows({1: [(1, 2, 3, 0)], 2: [(2, 1, 0, 1)]})
    expect("a table with too little history claims no shocks",
           "two completed rounds", 0,
           len(safe(gwru.shock_results, young, 2, names) or []),
           severity="high",
           note="a table five games old cannot support the claim")
    expect("completed_rounds counts distinct finished events", "2 rounds", 2,
           safe(gwru.completed_rounds, young))

    expect("ordinals handle the teens", "11, 12, 13",
           ["11th", "12th", "13th"], [gwru._ordinal(n) for n in (11, 12, 13)])
    expect("ordinals handle the rest", "1, 2, 3, 21",
           ["1st", "2nd", "3rd", "21st"],
           [gwru._ordinal(n) for n in (1, 2, 3, 21)])


def test_roundup_momentum():
    group("gw_roundup momentum", "medium")

    names = {1: "Alpha", 2: "Beta"}
    # Club 1 wins rounds 1-3; club 2 loses all three.
    wins = _fixture_rows({1: [(1, 2, 1, 0)], 2: [(2, 1, 0, 1)],
                          3: [(1, 2, 2, 0)]})
    runs = safe(gwru.momentum, wins, 3, names) or []
    expect("a three-win run is reported", "club 1 won rounds 1-3", "Alpha",
           runs[0]["team_name"] if runs else None, severity="medium")
    expect("the run counts the wins", "three in a row", 3,
           runs[0]["count"] if runs else None)

    # A run that ended is not a run. Reporting it would be describing history
    # under a heading that says form.
    broken = _fixture_rows({1: [(1, 2, 1, 0)], 2: [(2, 1, 0, 1)],
                            3: [(1, 2, 2, 0)], 4: [(2, 1, 3, 0)]})
    expect("a run that ended last round is not reported", "club 1 lost round 4",
           False, any(r["team_name"] == "Alpha"
                      for r in (safe(gwru.momentum, broken, 4, names) or [])),
           severity="medium")

    # Two of anything is a coincidence.
    two = _fixture_rows({1: [(1, 2, 1, 0)], 2: [(2, 1, 0, 1)]})
    expect("two wins is not a run", "club 1 won rounds 1-2", 0,
           len(safe(gwru.momentum, two, 2, names) or []))


def test_roundup_build():
    group("gw_roundup assembly", "high")

    pages = _roundup_pages((1, 101, "Top", "FWD", 50.0))
    roundup = safe(gwru.build, pages, 5, {1: _live(12, goals_scored=2)},
                   season_label="2026-27")
    check("build returns every expected section", "one scorer, no fixtures",
          "all section keys present", roundup,
          lambda r: isinstance(r, dict) and all(
              k in r for k in ("gameweek", "top_scorers", "underperformers",
                               "shocks", "momentum", "scorecard", "summary")),
          severity="high")
    check("the roundup is JSON-serialisable", "build() output",
          "json.dumps succeeds", roundup,
          lambda r: isinstance(json.dumps(r), str), severity="critical",
          note="it is written to SQLite as a JSON string by a nightly job")

    # The API being down, or a blank gameweek. Both must produce a page rather
    # than an exception inside a cron job nobody is watching.
    empty = safe(gwru.build, {}, 5, {})
    check("no data still produces a usable roundup", "no players, no fixtures",
          "a summary explaining the page is thin", empty,
          lambda r: isinstance(r, dict) and len(r.get("summary", "")) > 40,
          severity="high")


def _fake_card(name, **over):
    """A section card as gw_report._base_card builds one."""
    card = {"code": 1, "name": name, "full_name": name, "path": "/player/x-1",
            "pos": "MID", "team_code": 1, "team_name": "Testville",
            "cost": 7.5, "form": 6.0, "owned": 4.0, "predicted": 5.5,
            "headline": "5.5 projected", "why": f"{name} is worth a look.",
            "fixtures": [{"event": 5, "label": "BUR (H)"}]}
    card.update(over)
    return card


def _fake_report(**over):
    r = {"gameweek": 5, "season": "2026-27", "stage": "preview",
         "deadline_label": "Sat 12 Sep, 11:00 UK",
         "captains": [_fake_card("Alpha", predicted=7.4, owned=42.1, rank=1,
                                 behind_leader=0.0),
                      _fake_card("Bravo", predicted=7.2, owned=55.3, rank=2,
                                 behind_leader=0.2)],
         "in_form": [_fake_card("Charlie", form=8.4)],
         "differentials": [_fake_card("Delta", predicted=5.4, owned=3.2)],
         "attack_runs": [{"team_name": "Arsenal", "team_short": "ARS",
                          "team_code": 3, "ease": 8.6, "players": [],
                          "fixtures": [{"event": 5, "label": "BUR (H)"}]}],
         "defence_runs": [],
         "news": [_fake_card("Echo", owned=31.2, status="d")],
         "summary": "Gameweek 5 summary."}
    r.update(over)
    return r


def test_social_x_limits():
    group("social X drafts", "high")

    report = _fake_report()

    # The whole reason the thread exists. Every post has to be postable on its
    # own, and a draft that silently exceeds 280 is one you find out about in
    # the compose box after you've already decided to post.
    thread = safe(social.draft_x_thread, report, "preview") or []
    check("every post in the thread fits X's limit", "draft_x_thread()",
          "each <= 280 by X's own count", [social.x_length(p) for p in thread],
          lambda lens: bool(lens) and all(n <= 280 for n in lens),
          severity="critical")
    expect("the thread is three posts", "draft_x_thread()", 3, len(thread))

    single = safe(social.draft_x, report, "preview") or ""
    check("the single post fits too", "draft_x()", "<= 280",
          social.x_length(single), lambda n: n <= 280, severity="critical")

    # A URL costs 23 characters on X however long it is, so a raw len() would
    # under-report a draft carrying a long one and the printed count would
    # disagree with what X shows.
    long_url = "https://example.com/" + "a" * 200
    expect("a URL counts as 23 characters", "a 220-character URL", 23,
           safe(social.x_length, long_url))

    # The link belongs in the last post, not the first - the opening post has
    # to earn the impressions for the other two.
    check("only the last post carries the link", "draft_x_thread()",
          "no http in posts 1-2", thread,
          lambda ps: len(ps) > 1 and not any("http" in p for p in ps[:-1])
                     and "http" in ps[-1], severity="medium")


def test_social_fit_and_facts():
    group("social draft assembly", "high")

    # _fit drops whole lines rather than truncating mid-sentence. The old
    # behaviour cut the string, which always removed the tail of the briefing
    # and left a post ending in an ellipsis.
    out = safe(social._fit, ["keep"], ["a" * 500, "short"], 40)
    check("an over-long optional line is dropped, not cut", "_fit(limit=40)",
          "no ellipsis, the short line survives", out,
          lambda s: isinstance(s, str) and "…" not in s and "short" in s
                    and "aaaa" not in s, severity="high")

    # "" is a deliberate blank separator; None is an absent line. Conflating
    # them cost every blank line between blocks in the thread.
    expect("a blank separator survives", "_fit(['a'], ['', 'b'], 99)",
           "a\n\nb", safe(social._fit, ["a"], ["", "b"], 99))
    expect("an absent line is skipped", "_fit(['a'], [None, 'b'], 99)",
           "a\nb", safe(social._fit, ["a"], [None, "b"], 99))
    expect("a trailing separator is trimmed", "_fit(['a'], ['', 'b' * 99], 10)",
           "a", safe(social._fit, ["a"], ["", "b" * 99], 10))

    # Every claim in a post has to carry its number. That is the entire
    # difference between this and a bot posting a list of names.
    line = safe(social.player_line, _fake_card("Alpha", predicted=6.8,
                                               owned=4.1, cost=7.5))
    expect("a player line carries its numbers", "player_line()",
           "Alpha (6.8 pts, 4.1% owned, £7.5m)", line, severity="high")
    expect("a missing stat is left out rather than printed as None",
           "player_line() with no projection",
           "Alpha (4.1% owned, £7.5m)",
           safe(social.player_line, _fake_card("Alpha", predicted=None,
                                               owned=4.1, cost=7.5)))
    expect("NaN reads as absent", "player_line() with NaN cost",
           "Alpha (4.1% owned)",
           safe(social.player_line, _fake_card("Alpha", predicted=None,
                                               owned=4.1, cost=float("nan"))),
           note="a NaN formats as the literal word 'nan' if it isn't caught")


def test_social_hook():
    group("social hook line", "medium")

    # A well-rated name nobody owns outranks a captaincy pick, because it is
    # the one thing on the page no other FPL account is posting.
    diff_led = _fake_report()
    check("a strong differential leads", "5.4 projected at 3.2% owned",
          "names the differential", safe(social.hook, diff_led),
          lambda s: isinstance(s, str) and "Delta" in s, severity="medium")

    # Below the threshold it isn't a story, and the ladder falls through.
    weak = _fake_report(differentials=[_fake_card("Delta", predicted=2.0,
                                                  owned=3.2)],
                        in_form=[_fake_card("Charlie", form=4.0)])
    check("a weak differential does not lead", "2.0 projected at 3.2% owned",
          "names the captain pick instead", safe(social.hook, weak),
          lambda s: isinstance(s, str) and "Alpha" in s, severity="medium")

    # Preseason: every section empty. The hook must still produce a sentence
    # rather than raising inside a cron job.
    bare = _fake_report(captains=[], in_form=[], differentials=[],
                        attack_runs=[], news=[])
    check("an empty edition still produces a hook", "every section empty",
          "the summary, non-empty", safe(social.hook, bare),
          lambda s: isinstance(s, str) and len(s) > 10, severity="high")


def test_social_drafts_render():
    group("social draft output", "high")

    report = _fake_report()
    for name, fn in (("reddit", social.draft_reddit),
                     ("discord", social.draft_discord)):
        text = safe(fn, report, "preview")
        check(f"the {name} draft renders", f"draft_{name}()",
              "a non-trivial string", text,
              lambda s: isinstance(s, str) and len(s) > 200, severity="high")
        # The failure that matters is a formatting hole rather than a crash:
        # "None" or "nan" in a post is visible to everyone who reads it.
        check(f"the {name} draft prints no placeholder values", f"draft_{name}()",
              "no None/nan in the text", text,
              lambda s: isinstance(s, str) and "None" not in s
                        and "nan" not in s.replace("finance", ""),
              severity="high")

    # A preseason edition with nothing in it must still produce all three
    # drafts - the nightly job writes them whether or not the week is quiet.
    bare = _fake_report(captains=[], in_form=[], differentials=[],
                        attack_runs=[], defence_runs=[], news=[])
    for name, fn in (("x", social.draft_x), ("reddit", social.draft_reddit),
                     ("discord", social.draft_discord)):
        check(f"the {name} draft survives an empty edition", "no sections at all",
              "a string, no exception", safe(fn, bare, "draft"),
              lambda s: isinstance(s, str) and len(s) > 20, severity="high")


def _spot_pages(*specs):
    """A page index for the spotlight detectors, keyed on code."""
    pages = {}
    for eid, code, name, pos, team, owned, predicted in specs:
        pages[code] = {
            "id": eid, "code": code, "web_name": name, "full_name": name,
            "path": f"/player/{name.lower()}-{code}", "pos": pos, "team": team,
            "team_code": team * 3, "team_name": f"Club {team}",
            "team_short": f"C{team}", "cost": 7.5, "status": "a",
            "chance_of_playing_next_round": None,
            "next_gameweeks": [{"event": 10, "points": predicted,
                                "opponent": "BUR", "was_home": True}],
            "stats": {"selected_by_percent": owned},
        }
    return pages


def _spot_history(rows):
    """(player_id, round, minutes, points, goals, assists, xg, xa, gc, xgc)
    tuples -> the frame the detectors read."""
    import pandas as pd
    return pd.DataFrame([
        {"player_id": pid, "round": rnd, "minutes": mins, "total_points": pts,
         "goals_scored": g, "assists": a, "expected_goals": xg,
         "expected_assists": xa, "expected_goal_involvements": xg + xa,
         "goals_conceded": gc, "expected_goals_conceded": xgc,
         "starts": 1 if mins >= 60 else 0, "bps": 20}
        for pid, rnd, mins, pts, g, a, xg, xa, gc, xgc in rows])


def test_spotlight_injury_return():
    group("spotlight injury return", "high")

    pages = _spot_pages((1, 101, "Back", "MID", 1, 6.0, 6.2),
                        (2, 102, "Mate", "DEF", 1, 2.0, 3.9))
    # Five strong rounds, then four out. The club concedes more without him.
    rows = ([(1, r, 90, 7, 1, 0, 0.6, 0.3, 1, 1.1) for r in range(1, 6)]
            + [(1, r, 0, 0, 0, 0, 0.0, 0.0, 0, 0.0) for r in range(6, 10)]
            + [(2, r, 90, 3, 0, 0, 0.0, 0.0, 1, 1.1) for r in range(1, 6)]
            + [(2, r, 90, 1, 0, 0, 0.0, 0.0, 3, 2.6) for r in range(6, 10)])
    found = safe(ps.candidates, pages, 10, _spot_history(rows)) or []
    injury = [c for c in found if c["angle"] == "injury_return"]
    expect("a returning player is found", "5 good rounds then 4 out", 1,
           len(injury), severity="high")

    e = injury[0]["evidence"] if injury else {}
    expect("the absence is measured from the gameweek rows", "rounds 6-9 blank",
           4, e.get("rounds_out"), severity="high",
           note="FPL's status flag only says what is true now")
    expect("form before the injury is per 90", "35 points from 450 minutes",
           7.0, e.get("before_points_per_90"), severity="high")
    expect("the club's defence with him is computed", "1 conceded a game", 1.0,
           e.get("team_conceded_with"), severity="high")
    expect("and without him", "3 conceded a game", 3.0,
           e.get("team_conceded_without"), severity="high",
           note="the before-and-after is the whole point of this angle")

    # A two-round knock is not an absence, and a player who was poor before it
    # is not a returning asset.
    short_gap = ([(1, r, 90, 7, 1, 0, 0.6, 0.3, 1, 1.1) for r in range(1, 6)]
                 + [(1, r, 0, 0, 0, 0, 0.0, 0.0, 0, 0.0) for r in (6, 7)])
    expect("a two-gameweek knock is not a return", "2 rounds out", 0,
           len([c for c in (safe(ps.candidates, pages, 10,
                                 _spot_history(short_gap)) or [])
                if c["angle"] == "injury_return"]), severity="medium")

    poor = ([(1, r, 90, 1, 0, 0, 0.0, 0.0, 1, 1.1) for r in range(1, 6)]
            + [(1, r, 0, 0, 0, 0, 0.0, 0.0, 0, 0.0) for r in range(6, 10)])
    expect("a player who was poor before the injury is not a story",
           "1.0 points per 90 before", 0,
           len([c for c in (safe(ps.candidates, pages, 10,
                                 _spot_history(poor)) or [])
                if c["angle"] == "injury_return"]), severity="medium")


def test_spotlight_underlying():
    group("spotlight underlying stats", "high")

    pages = _spot_pages((1, 101, "Due", "FWD", 1, 8.0, 5.6),
                        (2, 102, "Hot", "MID", 2, 25.0, 5.1))
    rows = ([(1, r, 90, 2, 0, 0, 0.7, 0.3, 1, 1.2) for r in range(5, 10)]
            + [(2, r, 90, 9, 1, 0, 0.15, 0.05, 1, 1.2) for r in range(5, 10)])
    found = safe(ps.candidates, pages, 10, _spot_history(rows)) or []

    unlucky = [c for c in found if c["angle"] == "unlucky"]
    expect("a player creating and not scoring is found", "5.0 xGI, 0 returns",
           1, len(unlucky), severity="high")
    regression = [c for c in found if c["angle"] == "regression"]
    expect("a player scoring beyond the chances is found",
           "5 goals from 1.0 xGI", 1, len(regression), severity="high",
           note="a site that only publishes reasons to buy is worth nothing")

    # The two angles must never fire on the same player, which is what the
    # shared sign-flipped body guarantees.
    check("the two underlying angles pick different players",
          "one unlucky, one hot", "different codes", (unlucky, regression),
          lambda pair: bool(pair[0]) and bool(pair[1])
                       and pair[0][0]["code"] != pair[1][0]["code"],
          severity="high")

    # Below the threshold it is noise rather than a signal. Five rounds at
    # 0.30 xGI is 1.50 expected against a single actual return - a gap of 0.5,
    # which is the ordinary spread rather than anything to write about.
    quiet = ([(1, 5, 90, 6, 1, 0, 0.30, 0.0, 1, 1.2)]
             + [(1, r, 90, 2, 0, 0, 0.30, 0.0, 1, 1.2) for r in range(6, 10)])
    expect("a small gap is not written up", "1.5 xGI against 1 return", 0,
           len([c for c in (safe(ps.candidates, pages, 10,
                                 _spot_history(quiet)) or [])
                if c["angle"] in ("unlucky", "regression")]),
           severity="medium")

    # Ratios off 90 minutes of football describe nothing.
    thin = [(1, r, 15, 0, 0, 0, 0.5, 0.3, 1, 1.2) for r in range(5, 10)]
    expect("a player with almost no minutes is excluded", "75 minutes total", 0,
           len([c for c in (safe(ps.candidates, pages, 10,
                                 _spot_history(thin)) or [])
                if c["angle"] in ("unlucky", "regression")]), severity="high")


def test_spotlight_minutes_and_fixtures():
    group("spotlight minutes and fixtures", "medium")

    pages = _spot_pages((1, 101, "Nailed", "DEF", 1, 3.0, 4.4))
    rows = ([(1, r, 10, 0, 0, 0, 0.0, 0.0, 1, 1.2) for r in (4, 5, 6)]
            + [(1, r, 90, 4, 0, 0, 0.0, 0.0, 1, 1.2) for r in (7, 8, 9)])
    nailed = [c for c in (safe(ps.candidates, pages, 10,
                               _spot_history(rows)) or [])
              if c["angle"] == "newly_nailed"]
    expect("a newly-nailed starter is found", "3 cameos then 3 starts", 1,
           len(nailed), severity="medium")
    expect("the change in starts is recorded", "0 before, 3 after",
           (0, 3), (nailed[0]["evidence"]["prior_starts"],
                    nailed[0]["evidence"]["recent_starts"]) if nailed else None,
           severity="medium")

    already = [(1, r, 90, 4, 0, 0, 0.0, 0.0, 1, 1.2) for r in range(4, 10)]
    expect("a player who was always starting is not news", "6 straight starts",
           0, len([c for c in (safe(ps.candidates, pages, 10,
                                    _spot_history(already)) or [])
                   if c["angle"] == "newly_nailed"]), severity="medium")

    # Fixture swing, off FPL's own 1-5 difficulty.
    import pandas as pd
    fx = ([{"id": i, "event": r, "finished": True, "team_h": 1, "team_a": 9,
            "team_h_score": 1, "team_a_score": 1, "team_h_difficulty": 5,
            "team_a_difficulty": 2} for i, r in enumerate(range(1, 10), 1)]
          + [{"id": 100 + i, "event": r, "finished": False, "team_h": 1,
              "team_a": 9, "team_h_score": None, "team_a_score": None,
              "team_h_difficulty": 2, "team_a_difficulty": 4}
             for i, r in enumerate(range(10, 15), 1)])
    fixtures = pd.DataFrame(fx)
    past, future, upcoming = safe(ps.team_difficulty, fixtures, 1, 10) or (None, None, [])
    expect("past difficulty is the last five finished games", "all rated 5",
           5.0, past, severity="medium")
    expect("future difficulty is the next five scheduled", "all rated 2", 2.0,
           future, severity="medium")
    swing = [c for c in (safe(ps.candidates, pages, 10,
                              _spot_history(rows), fixtures) or [])
             if c["angle"] == "fixture_swing"]
    expect("a schedule that turns is found", "5.0 -> 2.0", 1, len(swing),
           severity="medium")


def test_spotlight_choice_and_ledger():
    group("spotlight choice and ledger", "high")

    pages = _spot_pages((1, 101, "Due", "FWD", 1, 8.0, 5.6),
                        (2, 102, "Hot", "MID", 2, 25.0, 5.1))
    history = _spot_history(
        [(1, r, 90, 2, 0, 0, 0.7, 0.3, 1, 1.2) for r in range(5, 10)]
        + [(2, r, 90, 9, 1, 0, 0.15, 0.05, 1, 1.2) for r in range(5, 10)])
    today = date(2026, 9, 15)

    first = safe(ps.choose, pages, 10, history, None, None, None, (), today)
    check("a subject is chosen", "two clear candidates", "a candidate dict",
          first, lambda c: isinstance(c, dict) and "code" in c and "angle" in c,
          severity="high")

    # Rule one: not the same player inside a fortnight.
    ledger = [{"post_date": "2026-09-14", "code": first["code"],
               "angle": first["angle"]}]
    second = safe(ps.choose, pages, 10, history, None, None, None, ledger, today)
    expect("the same player is not written up twice in a fortnight",
           "written about yesterday", False,
           second is not None and second["code"] == first["code"],
           severity="high")

    # ...but the block expires.
    stale = [{"post_date": "2026-08-01", "code": first["code"],
              "angle": "something_else"}]
    third = safe(ps.choose, pages, 10, history, None, None, None, stale, today)
    expect("the block expires after a fortnight", "written about six weeks ago",
           first["code"], third["code"] if third else None, severity="medium")

    # Rule two: not the same angle two nights running.
    same_angle = [{"post_date": "2026-09-14", "code": 999,
                   "angle": first["angle"]}]
    fourth = safe(ps.choose, pages, 10, history, None, None, None,
                  same_angle, today)
    expect("the same angle does not run two nights running",
           "last night used this angle", False,
           fourth is not None and fourth["angle"] == first["angle"],
           severity="high",
           note="the failure mode of a daily generated post is being the same")

    # Nothing worth writing about is a legitimate answer.
    quiet = _spot_history([(1, r, 90, 4, 1, 0, 0.9, 0.1, 1, 1.2)
                           for r in range(5, 10)])
    expect("a quiet pool produces no post", "nothing clears any threshold",
           None, safe(ps.choose, pages, 10, quiet, None, None, None, (), today),
           severity="high",
           note="better than publishing the least uninteresting player in the game")


def test_spotlight_post():
    group("spotlight write-up", "high")

    pages = _spot_pages((1, 101, "Due", "FWD", 1, 8.0, 5.6))
    history = _spot_history([(1, r, 90, 2, 0, 0, 0.7, 0.3, 1, 1.2)
                             for r in range(5, 10)])
    today = date(2026, 9, 15)
    candidate = safe(ps.choose, pages, 10, history, None, None, None, (), today)
    post = safe(ps.build, candidate, pages, 10, today, "2026-27")

    check("the write-up is JSON-serialisable", "build() output",
          "json.dumps succeeds", post,
          lambda p: isinstance(json.dumps(p), str), severity="critical",
          note="it is written to SQLite as a JSON string by a nightly job")
    check("the write-up has a headline and a body", "build() output",
          "headline plus 3+ paragraphs", post,
          lambda p: isinstance(p, dict) and len(p.get("headline", "")) > 10
                    and len(p.get("paragraphs", [])) >= 3, severity="high")
    check("the write-up ends with an explicit verdict", "build() output",
          "the last paragraph names the projection", post,
          lambda p: "projects" in (p.get("paragraphs") or [""])[-1],
          severity="high",
          note="five paragraphs of evidence that decline to conclude is the "
               "most annoying possible version of this page")
    check("the write-up prints no placeholder values", "build() output",
          "no None/nan in the prose", post,
          lambda p: not any("None" in s or "nan" in s
                            for s in (p.get("paragraphs") or [])),
          severity="high")
    check("the stat rows carry the evidence", "build() output",
          "at least three label/value rows", post,
          lambda p: len(p.get("stats", [])) >= 3, severity="medium")

    # Deterministic: the same player on the same date must produce the same
    # prose, or every bug in here is unreproducible.
    again = safe(ps.build, candidate, pages, 10, today, "2026-27")
    expect("the same inputs produce the same post", "build() twice",
           post["headline"], again["headline"], severity="high")
    # ...and a different date picks different phrasings from the variants.
    other = safe(ps.build, candidate, pages, 10, date(2026, 10, 20), "2026-27")
    check("a different date can vary the wording", "build() a month later",
          "the variant helper is date-sensitive",
          (post["headline"], other["headline"]),
          lambda pair: isinstance(pair[1], str) and len(pair[1]) > 10,
          severity="low")


def test_spotlight_drafts():
    group("spotlight drafts", "high")

    post = {
        "date": "2026-09-15", "gameweek": 10, "angle": "unlucky",
        "angle_label": "Due a return", "code": 101, "name": "Due",
        "full_name": "Due", "path": "/player/due-101", "pos": "FWD",
        "team_name": "Club 1", "team_code": 3, "cost": 8.0, "owned": 8.0,
        "predicted": 5.6, "score": 7.2,
        "headline": "Due is creating plenty and scoring none of it",
        "paragraphs": ["Due is a Club 1 forward at £8.0m, owned by 8.0%.",
                       "Over the last 5 gameweeks Due has played 450 minutes.",
                       "The chances behind that were worth more: 5.0 xGI.",
                       "The model projects 5.6 points for Due in gameweek 10."],
        "stats": [{"label": "Expected goal involvements", "value": 5.0},
                  {"label": "Actual returns", "value": 0},
                  {"label": "Gap", "value": 5.0}],
        "evidence": {},
    }

    thread = safe(social.draft_player_x, post) or []
    check("every post in the player thread fits X's limit",
          "draft_player_x()", "each <= 280",
          [social.x_length(p) for p in thread],
          lambda lens: bool(lens) and all(n <= 280 for n in lens),
          severity="critical")
    check("the first post carries a thread marker", "draft_player_x()[0]",
          "'1/3' present", thread[0] if thread else "",
          lambda s: "1/3" in s, severity="medium",
          note="a first post with no marker reads as a stray tweet")
    check("the link is on the last post only", "draft_player_x()",
          "no http before the last", thread,
          lambda ps_: len(ps_) > 1 and not any("http" in p for p in ps_[:-1])
                      and "http" in ps_[-1], severity="medium")

    for name, fn in (("reddit", social.draft_player_reddit),
                     ("discord", social.draft_player_discord)):
        check(f"the player {name} draft renders", f"draft_player_{name}()",
              "a non-trivial string", safe(fn, post),
              lambda s: isinstance(s, str) and len(s) > 150, severity="high")

    # The date goes into a filename and is reachable from a URL.
    for bad in ("../../etc/passwd", "2026-9-1", "latest", "",
                "2026-09-15/../../x"):
        expect(f"a non-date is refused: {bad!r}", "read_player_drafts()", None,
               safe(social.read_player_drafts, bad), severity="critical",
               note="this argument arrives from a URL path segment")


def _fake_roundup(**over):
    r = {"gameweek": 5, "season": "2026-27",
         "top_scorers": [{"name": "Top", "pos": "FWD", "team_name": "Arsenal",
                          "points": 16, "owned": 50.0,
                          "why": "Top scored 16 points."}],
         "underperformers": [{"name": "Blank", "pos": "MID",
                              "team_name": "Chelsea", "points": 1, "owned": 31.2,
                              "why": "Blank returned 1 point."}],
         "shocks": [{"headline": "Luton 2–0 Man City", "why": "18th beat 1st."}],
         "momentum": [{"team_name": "Brentford", "headline": "3 straight wins",
                       "why": "Brentford have won 3 in a row."}],
         "scorecard": {"predicted": 62.4, "actual": 71, "difference": 8.6},
         "summary": "Gameweek 5 roundup."}
    r.update(over)
    return r


def test_social_roundup_drafts():
    group("social roundup drafts", "high")

    roundup = _fake_roundup()

    post = safe(social.draft_roundup_x, roundup) or ""
    check("the roundup X post fits the limit", "draft_roundup_x()", "<= 280",
          social.x_length(post), lambda n: n <= 280, severity="critical")

    # The scorecard outranks everything else in the hook. It is the only line
    # in the post that nobody else could write.
    check("the scorecard leads the roundup hook", "a settled scorecard",
          "the hook names the projection and the score",
          safe(social.roundup_hook, roundup),
          lambda s: isinstance(s, str) and "62.4" in s and "71" in s,
          severity="high")
    check("a positive result is phrased as beating the projection",
          "projected 62.4, scored 71", "'more than projected'",
          safe(social.roundup_hook, roundup),
          lambda s: "more than projected" in s, severity="medium")
    check("a negative result is phrased as falling short",
          "projected 71, scored 62", "'short of it'",
          safe(social.roundup_hook,
               _fake_roundup(scorecard={"predicted": 71.0, "actual": 62,
                                        "difference": -9.0})),
          lambda s: "short of it" in s, severity="medium")

    # A gameweek the box slept through has a snapshot with no actuals, so the
    # scorecard is absent and the hook must fall through rather than raise.
    check("no scorecard falls through to the top scorer", "scorecard=None",
          "the hook names the top scorer",
          safe(social.roundup_hook, _fake_roundup(scorecard=None)),
          lambda s: isinstance(s, str) and "Top" in s, severity="high")

    for name, fn in (("reddit", social.draft_roundup_reddit),
                     ("discord", social.draft_roundup_discord)):
        text = safe(fn, roundup)
        check(f"the roundup {name} draft renders", f"draft_roundup_{name}()",
              "a non-trivial string", text,
              lambda s: isinstance(s, str) and len(s) > 200, severity="high")
        check(f"the roundup {name} draft prints no placeholder values",
              f"draft_roundup_{name}()", "no None/nan", text,
              lambda s: isinstance(s, str) and "None" not in s and "nan" not in s,
              severity="high")

    bare = _fake_roundup(top_scorers=[], underperformers=[], shocks=[],
                         momentum=[], scorecard=None)
    for name, fn in (("x", social.draft_roundup_x),
                     ("reddit", social.draft_roundup_reddit),
                     ("discord", social.draft_roundup_discord)):
        check(f"the roundup {name} draft survives an empty round",
              "every section empty", "a string, no exception", safe(fn, bare),
              lambda s: isinstance(s, str) and len(s) > 20, severity="high")


def test_retention():
    group("retention", "high")

    import os
    import db as _db
    import retention

    social_dir = social.social_dir()
    players_dir = social.players_dir()

    # A briefing draft for each of three gameweeks, a roundup draft for two,
    # and two nightly player posts written for gameweek 7.
    for gw in (5, 6, 7):
        with open(os.path.join(social_dir, f"gw{gw:02d}.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"briefing drafts for GW{gw}")
    for gw in (5, 6):
        with open(os.path.join(social_dir, f"roundup_gw{gw:02d}.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"roundup drafts for GW{gw}")
    for day, code in (("2026-09-01", 901), ("2026-09-02", 902)):
        _db.save_player_post(day, 7, code, "unlucky",
                             {"date": day, "gameweek": 7, "name": "X"},
                             replace=True)
        with open(os.path.join(players_dir, f"{day}.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("player drafts")

    # Nothing is removed until it is provably superseded.
    #
    # Membership rather than equality on `removed`: the suites share one social
    # directory, so other tests' draft files legitimately show up in the sweep
    # and asserting an exact list would make this fail on their behalf.
    dry = safe(retention.purge_briefing_drafts, 7, True)
    expect("a dry run removes nothing", "purge_briefing_drafts(7, dry_run=True)",
           True, os.path.exists(os.path.join(social_dir, "gw05.txt")),
           severity="high")
    check("...but reports what it would remove", "GW5 and GW6 are superseded",
          "both listed, GW7 not", (dry or {}).get("removed"),
          lambda got: got is not None and 5 in got and 6 in got and 7 not in got,
          severity="high")

    result = safe(retention.purge_briefing_drafts, 7)
    check("briefing drafts before the current gameweek go",
          "purge_briefing_drafts(7)", "GW5 and GW6 removed, GW7 kept",
          (result or {}).get("removed"),
          lambda got: got is not None and 5 in got and 6 in got and 7 not in got,
          severity="high")
    expect("the current gameweek's drafts stay", "GW7 is still current", True,
           os.path.exists(os.path.join(social_dir, "gw07.txt")),
           severity="critical",
           note="deleting the drafts you are about to post is the one "
                "genuinely bad outcome here")

    # The published pages are never touched. A draft can be rebuilt from a
    # stored payload; nothing can rebuild a payload.
    _db.save_gw_report(6, {"gameweek": 6, "summary": "kept", "in_form": [],
                           "differentials": [], "attack_runs": [],
                           "defence_runs": [], "news": []})
    safe(retention.purge_briefing_drafts, 7)
    check("the published briefing payload survives its drafts being deleted",
          "save then purge", "the gw_report row is still there",
          _db.get_gw_report(6),
          lambda r: r is not None and r["payload"]["summary"] == "kept",
          severity="critical",
          note="the archive is the fallback the whole rule depends on")

    # A roundup landing clears that gameweek's player posts, rows and files.
    before = len(_db.player_posts_for_gameweek(7))
    expect("the player posts exist first", "two posts for GW7", 2, before)
    saved = safe(retention.on_roundup_saved, 7)
    expect("the roundup clears that gameweek's player posts",
           "on_roundup_saved(7)", 2, (saved or {}).get("player_posts"),
           severity="high")
    expect("the ledger rows go", "player_posts_for_gameweek(7)", 0,
           len(_db.player_posts_for_gameweek(7)), severity="high")
    expect("the draft files go too", "2026-09-01.txt", False,
           os.path.exists(os.path.join(players_dir, "2026-09-01.txt")),
           severity="high")
    expect("earlier roundup drafts go", "GW5 superseded by GW7", False,
           os.path.exists(os.path.join(social_dir, "roundup_gw05.txt")),
           severity="medium")

    # A file with no ledger row is the state a half-finished purge leaves.
    with open(os.path.join(players_dir, "2026-08-15.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("orphan")
    orphans = safe(retention.orphaned_player_drafts)
    expect("an orphaned draft file is swept", "a file with no row", 1,
           (orphans or {}).get("removed"), severity="medium")

    # Every rule is called on jobs that run nightly for the rest of a season,
    # so calling one with nothing to do must be free rather than an error.
    check("running again with nothing to delete is harmless", "on_roundup_saved(7)",
          "no exception, nothing removed", safe(retention.on_roundup_saved, 7),
          lambda r: isinstance(r, dict) and r.get("player_posts") == 0,
          severity="high")
    check("a full sweep runs against an empty state", "retention.sweep()",
          "a dict with a log", safe(retention.sweep),
          lambda r: isinstance(r, dict) and isinstance(r.get("log"), list),
          severity="medium")


def test_ops_heartbeat():
    group("job heartbeat", "high")

    from datetime import datetime, timedelta, timezone
    import ops

    run_id = safe(ops.record_start, "test-job")
    check("a job records that it started", "record_start()", "a row id", run_id,
          lambda v: v is not None, severity="high")
    expect("...and that it finished", "record_finish(ok)", True,
           safe(ops.record_finish, run_id, "ok"), severity="high")

    # The figure that matters is the last SUCCESS, not the last run. A job
    # failing hourly has a very recent last run and has not worked since
    # yesterday.
    failed = safe(ops.record_start, "test-job")
    safe(ops.record_finish, failed, "failed", "boom")
    success = safe(ops.last_success, "test-job")
    check("a later failure does not move the last success",
          "ok then failed", "the successful run's timestamp", success,
          lambda s: s is not None, severity="high")

    runs = {r["job"]: r for r in (safe(ops.last_runs) or [])}
    expect("last_runs reports the most recent attempt", "ok then failed",
           "failed", runs.get("test-job", {}).get("status"), severity="high",
           note="the most recent ATTEMPT, unlike last_success")

    # A job that has never succeeded is stale, not skipped. On a fresh deploy
    # that is expected; a fortnight later it means the cron line was never
    # installed, which is the commonest silent failure of this whole setup.
    stale = safe(ops.stale_jobs, {"never-run-job": 1}) or []
    expect("a job that has never run is reported", "never-run-job", 1,
           len(stale), severity="high")
    expect("...and is flagged as never rather than overdue", "never-run-job",
           True, stale[0]["never"] if stale else None, severity="high")

    # The grace period. A job inside it is fine; past it, it is overdue.
    fresh = safe(ops.stale_jobs, {"test-job": 24})
    expect("a job that succeeded recently is not stale", "24h interval", 0,
           len(fresh or []), severity="high")
    future = datetime.now(timezone.utc) + timedelta(hours=100)
    late = safe(ops.stale_jobs, {"test-job": 1}, ops.STALE_GRACE, future)
    expect("a job well past its interval is stale", "1h interval, 100h later",
           1, len(late or []), severity="high")

    # Nothing in here may raise. It runs in the failure path of a cron job, and
    # a health check that throws turns "one job failed" into "two things failed
    # and the log says nothing useful".
    check("health() never raises", "ops.health()", "a dict", safe(ops.health),
          lambda h: isinstance(h, dict) and "healthy" in h, severity="high")
    expect("alerting is a no-op when no webhook is set", "FPL_ALERT_WEBHOOK unset",
           False, safe(ops.alert, "test message"), severity="medium",
           note="opt-in; a nightly 'no webhook configured' line would be noise")

    # The payload is shaped for whatever is on the other end, detected from the
    # URL - because a second setting describing the first is a setting that
    # ends up wrong.
    discord = safe(ops._payload_for, "https://discord.com/api/webhooks/x", "hi")
    check("a Discord webhook gets Discord's JSON shape", "discord.com URL",
          '{"content": ...}', discord,
          lambda p: "json" in p and "content" in p["json"], severity="medium")
    slack = safe(ops._payload_for, "https://hooks.slack.com/services/x", "hi")
    check("a Slack webhook gets Slack's", "hooks.slack.com URL",
          '{"text": ...}', slack,
          lambda p: "json" in p and "text" in p["json"], severity="medium")
    plain = safe(ops._payload_for, "https://ntfy.sh/mytopic", "hi")
    check("anything else gets a plain body", "ntfy URL", "raw data", plain,
          lambda p: "data" in p and b"hi" in p["data"], severity="medium")

    # Detection must BEAT FPL_ALERT_FORMAT, not defer to it.
    #
    # It deferred once, and the result was that a single stray value in .env
    # made every channel POST a plain body to Discord - which answered all five
    # with 400 "Expected Content-Type header to be one of ...". A Discord
    # webhook URL can only ever want Discord's JSON, so there is no legitimate
    # reason for a setting to be able to overrule that.
    saved_format = os.environ.get("FPL_ALERT_FORMAT")
    try:
        for stray in ("text", "slack", "TEXT"):
            os.environ["FPL_ALERT_FORMAT"] = stray
            payload = safe(ops._payload_for,
                           "https://discord.com/api/webhooks/1/abc", "hi")
            check(f"FPL_ALERT_FORMAT={stray!r} cannot break a Discord webhook",
                  "a discord.com URL", "still Discord JSON", payload,
                  lambda p: "json" in p and "content" in p.get("json", {}),
                  severity="critical")

        # ...but it still applies where detection has nothing to go on.
        os.environ["FPL_ALERT_FORMAT"] = "text"
        check("the override still governs an unrecognised URL", "ntfy URL",
              "raw data", safe(ops._payload_for, "https://ntfy.sh/topic", "hi"),
              lambda p: "data" in p, severity="medium")
    finally:
        if saved_format is None:
            os.environ.pop("FPL_ALERT_FORMAT", None)
        else:
            os.environ["FPL_ALERT_FORMAT"] = saved_format

    # send() reports WHY, which notify() cannot. A quoted value in .env is the
    # failure most likely to be mistaken for a network problem.
    ok, detail = safe(ops.send, "alerts", "x") or (None, "")
    check("send() explains a missing webhook rather than just failing",
          "no webhook configured", "a reason, not a bare False", (ok, detail),
          lambda pair: pair[0] is False and isinstance(pair[1], str)
                       and len(pair[1]) > 10, severity="high")


def test_ops_backups():
    group("database backups", "high")

    import os
    import sqlite3
    import ops

    result = safe(ops.backup_database)
    check("a backup is written", "backup_database()", "a file on disk", result,
          lambda r: isinstance(r, dict) and r.get("written")
                    and os.path.exists(r["path"]), severity="critical",
          note="the database holds the entire published archive")

    # The point of using SQLite's backup API rather than copying the file: the
    # copy has to be a valid, complete database, not a snapshot of a WAL-mode
    # file caught mid-write.
    if result and result.get("written"):
        def readable(path):
            conn = sqlite3.connect(path)
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                return "gw_report" in names and "ai_team_snapshot" in names
            finally:
                conn.close()
        check("the backup is a complete, openable database", result["path"],
              "the schema is present", result["path"], readable,
              severity="critical")

    # Running twice on one day overwrites rather than accumulating - the name
    # is dated, not timestamped.
    again = safe(ops.backup_database)
    check("a second backup the same day is not a second file", "backup twice",
          "still one file for today", again,
          lambda r: isinstance(r, dict) and r.get("written"), severity="low")

    status = safe(ops.backup_status)
    check("backup_status reports what exists", "backup_status()",
          "a count and a newest name", status,
          lambda s: isinstance(s, dict) and s.get("count", 0) >= 1
                    and s.get("latest"), severity="high",
          note="a backup nobody has verified the existence of is not a backup")


def test_notification_channels():
    group("notification channels", "high")

    import os
    import db as _db
    import ops

    saved = {v: os.environ.get(v) for v in ops.CHANNELS.values()}
    try:
        for v in ops.CHANNELS.values():
            os.environ.pop(v, None)

        expect("no webhook means no channel", "nothing set", "off",
               safe(ops.configured_channels).get("drafts"), severity="high")

        # One variable gets you everything in one place. Without the fallback,
        # setting up alerting and then finding three of the four features
        # silently doing nothing is the default experience.
        os.environ["FPL_ALERT_WEBHOOK"] = "https://discord.com/api/webhooks/a/b"
        configured = safe(ops.configured_channels) or {}
        expect("an unset channel falls back to the alerts webhook",
               "only FPL_ALERT_WEBHOOK set", "fallback",
               configured.get("drafts"), severity="high")
        expect("the alerts channel reports its own webhook", "FPL_ALERT_WEBHOOK",
               "own", configured.get("alerts"))

        os.environ["FPL_DRAFTS_WEBHOOK"] = "https://discord.com/api/webhooks/c/d"
        expect("a channel with its own webhook uses it", "FPL_DRAFTS_WEBHOOK",
               "https://discord.com/api/webhooks/c/d",
               safe(ops.webhook_url, "drafts"), severity="high")
        expect("...and the others still fall back", "gameweek unset",
               "https://discord.com/api/webhooks/a/b",
               safe(ops.webhook_url, "gameweek"))
    finally:
        for v, value in saved.items():
            if value is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = value

    # The ledger. Every notification is triggered by a window wider than the
    # poll interval, so this is the only thing stopping four copies.
    expect("the first claim on a notification succeeds", "mark_notified", True,
           safe(_db.mark_notified, "test_kind", "99"), severity="critical")
    expect("the second is refused", "mark_notified again", False,
           safe(_db.mark_notified, "test_kind", "99"), severity="critical",
           note="the hourly watcher hits each window 2-4 times")
    expect("a different ref is independent", "mark_notified other ref", True,
           safe(_db.mark_notified, "test_kind", "100"))
    expect("a different kind is independent", "same ref, other kind", True,
           safe(_db.mark_notified, "other_kind", "99"),
           note="the day-out and final reminders for one gameweek are separate")

    expect("was_notified sees the claim", "was_notified", True,
           safe(_db.was_notified, "test_kind", "99"))
    expect("a claim can be cleared for a resend", "clear_notification", True,
           safe(_db.clear_notification, "test_kind", "99"))
    expect("...and then the claim succeeds again", "mark_notified after clear",
           True, safe(_db.mark_notified, "test_kind", "99"),
           note="for resending by hand after a webhook outage")

    # notify_once must not consume the claim when nothing could be sent -
    # otherwise configuring a webhook later would find every notification
    # already marked as delivered.
    expect("notify_once with no webhook does not consume the claim",
           "no webhook configured", False,
           safe(ops.notify_once, "drafts", "test_kind", "555", "hello"),
           severity="critical")
    expect("...so the claim is still available", "mark_notified", True,
           safe(_db.mark_notified, "test_kind", "555"), severity="critical")


def test_channel_messages():
    group("Discord channel messages", "high")

    import ops

    post = {"date": "2026-09-15", "gameweek": 10, "angle": "unlucky",
            "angle_label": "Due a return", "name": "Due",
            "path": "/player/due-101", "pos": "FWD", "team_name": "Club",
            "cost": 8.0, "owned": 8.0, "predicted": 5.6,
            "headline": "Due is creating plenty and scoring none of it",
            "paragraphs": ["Due is a Club forward at £8.0m.",
                           "Over the last 5 gameweeks Due has played 450 minutes.",
                           "The chances were worth more: 5.0 xGI.",
                           "The model projects 5.6 points for Due."],
            "stats": [{"label": "xGI", "value": 5.0},
                      {"label": "Returns", "value": 0}]}
    roundup = _fake_roundup()
    report = _fake_report()

    messages = {
        "player post": safe(social.channel_player_post, post),
        "briefing": safe(social.channel_briefing_ready, report),
        "roundup": safe(social.channel_roundup_ready, roundup),
        "reminder day": safe(social.channel_deadline_reminder, "day", 10,
                             "Sat 12 Sep, 11:00 UK", 24.0, report),
        "reminder final": safe(social.channel_deadline_reminder, "final", 10,
                               "Sat 12 Sep, 11:00 UK", 2.0, report),
        "ai squad": safe(social.channel_ai_squad, 10,
                         {"transfers": [{"out": "A", "in": "B", "gain": 1.8,
                                         "free": False}],
                          "chip": "bboost", "predicted_points": 64.2,
                          "formation": "3-4-3"},
                         {"predicted_points": 71.5, "squad_cost": 99.5,
                          "formation": "3-5-2"}),
    }

    for name, message in messages.items():
        # Discord rejects a message over 2000 characters outright. A push that
        # is silently dropped is worse than one that was never wired up.
        check(f"the {name} message fits Discord's limit", name,
              f"<= {ops.DISCORD_HARD_LIMIT} characters",
              len(message) if message else None,
              lambda n: n is not None and n <= ops.DISCORD_HARD_LIMIT,
              severity="critical")
        check(f"the {name} message prints no placeholder values", name,
              "no None/nan", message,
              lambda s: isinstance(s, str) and "None" not in s and "nan" not in s,
              severity="high")

    # Chip codes are FPL's internal names. "Chip played: bboost" is a line only
    # somebody who has read this code can parse.
    check("a chip is named, not coded", "channel_ai_squad with bboost",
          "'Bench Boost' not 'bboost'", messages["ai squad"],
          lambda s: "Bench Boost" in s and "bboost" not in s, severity="medium")
    expect("an unknown chip falls back to its code", "chip_name('newchip')",
           "newchip", safe(social.chip_name, "newchip"),
           note="FPL has added chips before")

    # The roundup hook already leads with the scorecard, so printing the track
    # record again below it said the same numbers twice in five lines.
    discord = safe(social.draft_roundup_discord, roundup) or ""
    expect("the roundup states its scorecard once", "draft_roundup_discord()",
           1, discord.count("62.4"), severity="medium")

    # The two reminders do different jobs and must not read the same.
    check("the two deadline reminders say different things",
          "day vs final", "different text",
          (messages["reminder day"], messages["reminder final"]),
          lambda pair: pair[0] != pair[1] and "post" in pair[0].lower()
                       and "your own team" in pair[1].lower(), severity="medium")

    # An empty round still has to produce something sendable - these fire from
    # a cron job with nobody watching.
    bare = _fake_roundup(top_scorers=[], underperformers=[], shocks=[],
                         momentum=[], scorecard=None)
    check("a quiet roundup still produces a message", "every section empty",
          "a non-empty string", safe(social.channel_roundup_ready, bare),
          lambda s: isinstance(s, str) and len(s) > 20, severity="high")
    check("a squad with no transfers still produces a message",
          "no transfers, no chip", "a non-empty string",
          safe(social.channel_ai_squad, 10, {"transfers": [], "chip": None,
                                             "predicted_points": 60.0}, None),
          lambda s: isinstance(s, str) and "no transfers" in s.lower(),
          severity="medium")


def test_kofi_message():
    group("Ko-fi relay", "critical")

    import ops

    def kofi(**over):
        p = {"type": "Donation", "is_public": True, "from_name": "Jo Example",
             "message": "Good luck with the model!", "amount": "3.00",
             "currency": "GBP", "email": "donor@private.example",
             "url": "https://ko-fi.com/Home/CoffeeShop?txid=abc",
             "is_subscription_payment": False}
        p.update(over)
        return p

    message = safe(social.channel_kofi, kofi()) or ""
    check("a donation renders with its amount and donor", "channel_kofi()",
          "the amount, the name and the message", message,
          lambda s: "£3.00" in s and "Jo Example" in s
                    and "Good luck with the model!" in s, severity="high")

    # Ko-fi sends the donor's email in every payload. It is personal data with
    # no reason to be in a chat channel, and the privacy policy makes promises
    # about what this site keeps.
    check("the donor's email is never included", "channel_kofi()",
          "no email address in the message", message,
          lambda s: "donor@private.example" not in s and "@private" not in s,
          severity="critical",
          note="a Discord message is a copy nobody tracks the retention of")

    # is_public is the donor saying whether their note can be shown. A private
    # channel is still somewhere they did not agree to.
    private = safe(social.channel_kofi,
                   kofi(is_public=False, message="please keep this private"))
    check("a private message is not quoted", "is_public=False",
          "the note is withheld, the donation still reported", private,
          lambda s: "keep this private" not in s and "private message" in s,
          severity="high")

    # Donor names and messages are free text typed by a stranger on a public
    # form, and they end up in a Discord post.
    hostile = safe(social.channel_kofi,
                   kofi(from_name="@everyone",
                        message="`**bold**` \n\n\n\n\n spam far below")) or ""
    check("markdown in donor text is defanged", "backticks and asterisks",
          "no ` or * left in the message", hostile,
          lambda s: "`" not in s and "**bold**" not in s, severity="high",
          note="otherwise a donor can break out of the quote and restyle the post")
    check("newline flooding is collapsed", "five consecutive newlines",
          "the message is one line", hostile,
          lambda s: "\n\n\n" not in s, severity="medium",
          note="forty blank lines would push everything else out of view")

    # The real defence against @everyone is at the transport, so it applies to
    # every sender rather than just this one.
    payload = safe(ops._payload_for, "https://discord.com/api/webhooks/x/y",
                   "@everyone hello")
    check("Discord messages disable every mention", "_payload_for()",
          'allowed_mentions {"parse": []}', payload,
          lambda p: p.get("json", {}).get("allowed_mentions") == {"parse": []},
          severity="critical",
          note="a webhook can ping @everyone by default, from a public endpoint")

    # A URL arrives inside a payload from the internet. The token proves it
    # came from Ko-fi, not that every field in it is somewhere to send anyone.
    evil = safe(social.channel_kofi, kofi(url="https://evil.example/phish")) or ""
    check("a non-Ko-fi link is dropped", "url=https://evil.example/phish",
          "the link does not appear", evil,
          lambda s: "evil.example" not in s, severity="critical")

    # Subscriptions read differently from one-off donations.
    sub = safe(social.channel_kofi, kofi(is_subscription_payment=True,
                                         is_first_subscription_payment=True))
    check("a first subscription payment says so", "is_first_subscription_payment",
          "'New monthly supporter'", sub,
          lambda s: "monthly supporter" in s.lower(), severity="low")

    # Ko-fi's payload shape has changed before and every field is optional as
    # far as this code is concerned.
    check("an almost-empty payload still renders", "{} from Ko-fi",
          "a string, no exception", safe(social.channel_kofi, {}),
          lambda s: isinstance(s, str) and len(s) > 5, severity="high")


def test_kofi_route_absent_when_unconfigured():
    group("Ko-fi relay routing", "critical")

    import main

    # The endpoint is publicly reachable by necessity - Ko-fi's servers cannot
    # authenticate - so when no token is configured it must not EXIST rather
    # than exist and refuse. An endpoint that answers is one more thing on the
    # internet to probe, and the same rule already governs the IndexNow and
    # social-key routes.
    paths = {getattr(r, "path", "") for r in main.app.routes}
    expect("no Ko-fi route without FPL_KOFI_TOKEN", "app.routes", False,
           "/api/kofi" in paths, severity="critical",
           note="unset means absent, not present-and-refusing")


def test_declared_dependencies():
    group("dependencies", "high")

    import ast
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent

    def declared(path):
        names = set()
        try:
            text = (root / path).read_text(encoding="utf-8")
        except OSError:
            return names
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            # "uvicorn[standard]", "starlette<1.0.0", "pandas==3.0.0"
            name = re.split(r"[\[<>=!;]", line)[0].strip().lower()
            if name:
                names.add(name)
        return names

    app_reqs = declared("python/requirements.txt")
    test_reqs = declared("tests/requirements.txt")

    # Where the import name and the distribution name differ.
    alias = {"sklearn": "scikit-learn", "yaml": "pyyaml", "PIL": "pillow",
             "google": "google-auth", "dateutil": "python-dateutil",
             "dotenv": "python-dotenv"}

    stdlib = set(sys.stdlib_module_names)
    local = {p.stem for p in (root / "python").glob("*.py")} | \
            {p.stem for p in (root / "tests").glob("*.py")}

    def third_party_imports(folder):
        found = set()
        for path in (root / folder).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        found.add(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
        return {m for m in found
                if m not in stdlib and m not in local and not m.startswith("_")}

    # Every module the application imports directly must be declared directly.
    # markupsafe was imported by main.py and declared by nobody - it happened to
    # be installed as a dependency of jinja2, which is one dependency
    # resolution away from an import-time crash on a fresh install.
    undeclared_app = sorted(m for m in third_party_imports("python")
                            if alias.get(m, m).lower() not in app_reqs)
    expect("every module python/ imports is in python/requirements.txt",
           "ast scan of python/*.py", [], undeclared_app, severity="high",
           note="a direct import satisfied by somebody else's dependency is a "
                "missing module waiting to happen")

    undeclared_tests = sorted(
        m for m in third_party_imports("tests")
        if alias.get(m, m).lower() not in app_reqs | test_reqs)
    expect("every module tests/ imports is declared too",
           "ast scan of tests/*.py", [], undeclared_tests, severity="high")

    # The one that actually broke CI is invisible to the scan above, because
    # nothing imports it by name: fastapi.testclient is built on httpx, and
    # plain fastapi does not install it - only its "standard" extra does. So it
    # is pinned by name here rather than inferred.
    check("httpx is declared for the test client",
          "tests/requirements.txt", "httpx present", test_reqs,
          lambda r: "httpx" in r, severity="critical",
          note="without it the suite dies at import before a single test runs")

    # Every application requirement carries an exact version.
    #
    # The image is rebuilt on every push, so an unpinned line means two builds
    # of the same commit can install different code - and the one that reaches
    # production is whichever happened to be current when watchtower pulled.
    # Dependabot is pointed at this file and the suite gates its PRs, so
    # pinning costs nothing and buys a reproducible build.
    unpinned = []
    for line in (root / "python/requirements.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and "==" not in line:
            unpinned.append(line)
    expect("every app requirement is pinned to an exact version",
           "python/requirements.txt", [], unpinned, severity="medium",
           note="an unpinned line makes the build unreproducible from the commit alone")


def test_kit_colours():
    group("club colours", "medium")

    colours = safe(kits.team_colours)
    check("every club in kits.js is parsed", "static/kits.js",
          "20 clubs", colours,
          lambda c: isinstance(c, dict) and len(c) >= 20, severity="high",
          note="a chip with no colour is the visible symptom")
    check("colours are hex values", "parsed table", "#RRGGBB", colours,
          lambda c: all(v["primary"].startswith("#") and v["secondary"].startswith("#")
                        for v in c.values()) if c else False)
    # Nott'm Forest is written with double quotes in kits.js because its name
    # contains an apostrophe - the one row a naive single-quote regex misses.
    check("a club whose name contains an apostrophe is parsed", "team_code 17",
          "Nott'm Forest present", colours,
          lambda c: 17 in c and "Forest" in c[17]["name"], severity="medium")

    expect("an unknown club falls back rather than raising", "team_code 9999",
           kits.FALLBACK, safe(kits.colours_for, 9999))
    expect("a None code falls back", "team_code None", kits.FALLBACK,
           safe(kits.colours_for, None),
           note="a promoted club not yet in kits.js hits this")


def test_model_leakage_and_scale():
    """The two properties the points model is only correct because of.

    Both are silent when broken: a leaking feature makes the training numbers
    look better, and a mis-scaled one still predicts something. Neither shows up
    as an error anywhere.
    """
    group("model features", "critical")

    # One player, one season, scoring 1, 2, 3, 4 in consecutive gameweeks.
    frame = pd.DataFrame({
        'player_key': [1] * 4, 'season_start': [2025] * 4, 'round': [1, 2, 3, 4],
        'total_points': [1, 2, 3, 4], 'minutes': [90] * 4,
    })
    for col in tm.ROLLING_COLS:
        if col not in frame.columns:
            frame[col] = 0.0
    built = tm.build_rolling_features(frame).sort_values('round')

    # GW1 has nothing before it, so every feature must be NaN. If the shift
    # were missing this would be 1.0 - the gameweek's own result.
    expect("first gameweek has no history to average", "GW1 total_points_roll3",
           True, bool(pd.isna(built['total_points_roll3'].iloc[0])))
    expect("a gameweek never sees its own result", "GW4 total_points_roll3",
           2.0, round(float(built['total_points_roll3'].iloc[3]), 3),
           severity="critical")
    expect("the level is the mean of everything before", "GW4 total_points_level",
           2.0, round(float(built['total_points_level'].iloc[3]), 3))
    expect("games_seen counts earlier rows only", "GW4 games_seen",
           3, int(built['games_seen'].iloc[3]))

    # Rolling must not carry across a season boundary.
    two = pd.concat([frame, frame.assign(season_start=2026, total_points=[9] * 4)])
    across = tm.build_rolling_features(two)
    first_2026 = across[(across.season_start == 2026) & (across['round'] == 1)]
    check("form does not carry across a summer", "GW1 of the following season",
          "NaN, not last season's average",
          "NaN" if bool(pd.isna(first_2026['total_points_roll3'].iloc[0])) else "carried",
          lambda v: v == "NaN", severity="critical",
          note="grouping by player alone would make last May the form for "
               "this August, across a transfer window")


def test_team_strength_ranks_are_scale_free():
    """Strength features are percentile ranks, and that is load-bearing.

    FPL has already changed this scale once: 2025-26's teams.csv carries
    strength_overall_home around 975-1355, and 2026-27's carries the same column
    as 2-5. The old model fed `difficulty * 220` into a scaler fitted on the
    first, which put an easy fixture eight standard deviations outside anything
    it had seen.
    """
    group("team strength scale", "high")

    ranks = tm.team_strength_ranks(seasons.current_season())
    check("the current season has usable strength ranks", "team_strength_ranks()",
          "a table, not None", "table" if ranks is not None else "None",
          lambda v: v == "table", severity="high")
    if ranks is None:
        return

    for column in ('overall_home_rank', 'attack_home_rank', 'defence_away_rank'):
        check(f"{column} is populated", f"team_strength_ranks()[{column!r}]",
              "not all NaN", "populated" if ranks[column].notna().any() else "all NaN",
              lambda v: v == "populated", severity="high",
              note="FPL ships strength_attack_* and strength_defence_* as "
                   "all-zero every preseason; these must fall back to the "
                   "overall rating rather than going missing, or the fixture "
                   "stops affecting the projection at all")

    values = ranks['overall_home_rank'].dropna()
    check("ranks are percentiles", "overall_home_rank range",
          "between 0 and 1", f"{values.min():.2f}-{values.max():.2f}",
          lambda _: bool(values.min() > 0) and bool(values.max() <= 1.0),
          severity="high")


def test_model_bundle_version_guard():
    """A stale pickle must be refused, not used.

    The bundles live on the deployment's mounted volume and ensure_seeded()
    only ever copies files that are MISSING, so a deploy hands new inference
    code the pickle the volume already had. Feeding that model a feature list it
    was never fitted on is the one failure here that produces plausible,
    confident, wrong numbers on every page.
    """
    group("model bundle version", "critical")

    with tempfile.TemporaryDirectory() as directory:
        joblib.dump({'version': tm.MODEL_VERSION - 1, 'kind': 'old'},
                    os.path.join(directory, 'midfielder_model.pkl'))
        raised = safe(rating_model.load_models, directory)
        check("an out-of-date bundle is refused", "load_models() on version N-1",
              "StaleModelError", type(raised).__name__ if isinstance(raised, Exception)
              else str(raised)[:40],
              lambda v: "StaleModel" in str(v), severity="critical")

    check("the bundles on disk are current", "load_models()",
          f"version {tm.MODEL_VERSION}",
          "current" if not isinstance(safe(rating_model.load_models), Exception)
          else "stale", lambda v: v == "current", severity="high",
          note="if this fails locally, run `python train_model.py`")


def test_two_stage_predictions():
    """The blended figure must sit between a cameo and a full start.

    E[points] = P(start) x points-if-he-starts + P(cameo) x cameo points, so it
    can never exceed the if-he-starts number. A blend that came out above it
    would mean the arithmetic had been rearranged wrongly, which is exactly the
    kind of change that still produces believable output.
    """
    group("two-stage blend", "high")

    bundles = safe(rating_model.load_models)
    if isinstance(bundles, Exception) or not bundles:
        check("models available to score", "load_models()", "at least one bundle",
              "none", lambda v: False, severity="high",
              note="run `python train_model.py`")
        return

    position, bundle = sorted(bundles.items())[0]
    rows = pd.DataFrame([{c: 1.0 for c in bundle['feature_cols']} for _ in range(3)])
    expected, if_starts, p_start = tm.predict_bundle(bundle, rows)

    check("start probability is a probability", f"{position} P(start)",
          "0 to 1", f"{p_start.min():.2f}-{p_start.max():.2f}",
          lambda _: bool(p_start.min() >= 0 and p_start.max() <= 1), severity="high")
    check("points if he starts are non-negative", f"{position} if-starts",
          ">= 0", f"{if_starts.min():.2f}",
          lambda _: bool(if_starts.min() >= 0), severity="high")
    check("the blend never exceeds a full start", f"{position} expected vs if-starts",
          "expected <= if_starts", f"{expected.max():.2f} vs {if_starts.max():.2f}",
          lambda _: bool((expected <= if_starts + 1e-9).all()), severity="high")


def test_clubelo_cache_freshness():
    """An unexpired ClubElo cache is used without a network round-trip.

    This sat on the startup path: one unreachable ClubElo cost a 20s timeout on
    every boot and every /api/refresh, which was 20 of the 46 seconds a cold
    start took - more than rating the entire player pool.

    The freshness test has to read the DATES INSIDE the file rather than its
    mtime, because the cache is committed to the repo and therefore baked into
    the image. `ensure_seeded()` copies it onto a fresh volume with today's
    mtime, so an mtime check would treat a months-old file as current.
    """
    group("clubelo cache", "medium")

    import fetch_data

    def frame(to_dates):
        return pd.DataFrame({"Club": ["A"] * len(to_dates),
                             "Elo": [1500.0] * len(to_dates),
                             "To": to_dates})

    today = "2026-08-16"
    check("a cache still in force is current", "To all after today",
          True, fetch_data.clubelo_cache_is_current(
              frame(["2026-08-21", "2026-08-30"]), today),
          lambda v: v is True, severity="medium")
    check("one expired row makes it stale", "To includes a past date",
          False, fetch_data.clubelo_cache_is_current(
              frame(["2026-08-14", "2026-08-30"]), today),
          lambda v: v is False, severity="medium",
          note="that club has played since the file was written, so its rating "
               "has moved")
    check("the fetch day itself counts as current", "To == today",
          True, fetch_data.clubelo_cache_is_current(frame([today]), today),
          lambda v: v is True, severity="medium")
    for label, bad in (("empty frame", pd.DataFrame()),
                       ("no To column", pd.DataFrame({"Club": ["A"]})),
                       ("unparseable dates", frame(["not-a-date"])),
                       ("missing file", None)):
        check(f"{label} is treated as stale", label, False,
              fetch_data.clubelo_cache_is_current(bad, today),
              lambda v: v is False, severity="medium",
              note="refetching costs a request; trusting a cache we can't "
                   "date costs wrong team strengths all season")

    check("the timeout is proportionate to an optional source",
          "fetch_data.CLUBELO_TIMEOUT", "<= 10s", fetch_data.CLUBELO_TIMEOUT,
          lambda v: v <= 10, severity="medium",
          note="it has a working cache behind it and only fills in preseason "
               "strength, so it must not block a boot the way the FPL API may")


def test_dispersion_calibration():
    """The preseason quantile map may reshape, but must never reorder.

    That is the entire licence for doing it. The model's preseason spread is
    about half of what last season actually produced, and mapping onto the
    realised distribution fixes the shape - but only honestly if it adds no
    opinion about who is better than whom. A map that reordered anyone would
    be inventing exactly the information the model has been measured not to
    have.
    """
    group("dispersion calibration", "high")

    model = pd.Series([3.2, 3.4, 3.4, 3.6, 3.9, 4.1, 4.5, 5.0] * 4)
    real = pd.Series([1.5, 2.2, 2.8, 3.1, 3.6, 4.0, 4.8, 7.0] * 4)

    frame = {"Midfielder": pd.DataFrame({"points_if_starts": model})}
    calibrators = rating_model.fit_calibrators(frame, season="__nonexistent__")
    check("no targets means no calibration", "fit_calibrators, unreadable season",
          "empty mapping", calibrators, lambda v: v == {}, severity="high",
          note="a missing previous season must switch this off, not crash a "
               "nightly job or silently map onto nothing")

    # The calibrator shape fit_calibrators produces: the two distributions read
    # at the same quantiles, with ties in the source collapsed.
    qs = [i / 100 for i in range(101)]
    src, keep = numpy.unique(model.quantile(qs).to_numpy(), return_index=True)
    cal = (src, real.quantile(qs).to_numpy()[keep])

    mapped = rating_model.apply_calibration(model, cal)

    check("order is preserved exactly", "apply_calibration",
          "same ranking before and after",
          "checked",
          lambda _: bool(model.rank(method="average")
                         .equals(mapped.rank(method="average"))),
          severity="high",
          note="a reordering here would mean the map invented a preference "
               "between two players the model rated equally")
    check("spread moves onto the target scale", "apply_calibration",
          f"sd near {real.std():.2f}, from {model.std():.2f}",
          f"{mapped.std():.2f}",
          lambda _: abs(mapped.std() - real.std()) < 0.25, severity="high")
    check("nothing is mapped below zero", "apply_calibration",
          ">= 0", f"{mapped.min():.2f}",
          lambda _: bool(mapped.min() >= 0), severity="high")

    passthrough = rating_model.apply_calibration(model, None)
    check("no calibrator is a pass-through", "apply_calibration(x, None)",
          "unchanged", "checked",
          lambda _: bool(passthrough.equals(model)), severity="high",
          note="in season this is the path taken, so it must not touch a "
               "single number")


# ---- Chip scheduling ------------------------------------------------------

def _matrix(spec):
    """{chip: {gw: gain}} -> the (effective, computed, factors) shape."""
    return {c: {gw: (v, v, {}) for gw, v in row.items()} for c, row in spec.items()}


def _flat(chips, gameweeks, value):
    return _matrix({c: {gw: value for gw in gameweeks} for c in chips})


def test_chip_halves():
    group("chip halves", "high")

    for gw, expected in ((1, (1, 19)), (19, (1, 19)), (20, (20, 38)), (38, (20, 38))):
        expect(f"GW{gw} falls in the right half", gw, expected,
               fixture_structure.chip_half(gw),
               note="FPL returns every chip after GW19, so which half a "
                    "gameweek is in decides which chips are still in hand")

    expect("a gameweek past the season still resolves", 44, (20, 38),
           fixture_structure.chip_half(44), severity="medium",
           note="a fixture-list oddity must not raise at a deadline")

    expect("the deadline for a first-half gameweek is GW19", 4, 19,
           fixture_structure.half_deadline(4))
    expect("the deadline for a second-half gameweek is GW38", 25, 38,
           fixture_structure.half_deadline(25))

    played = [(6, "wildcard"), (22, "bboost")]
    expect("a chip played this half counts as used", "GW10, wildcard in GW6",
           ["wildcard"], ai_manager.chips_used_in_half(played, 10))
    expect("a chip played LAST half is back in hand", "GW25, wildcard in GW6",
           ["bboost"], ai_manager.chips_used_in_half(played, 25),
           severity="high",
           note="reading the log flat is what left the second chip set "
                "permanently unused")


def test_chip_schedule_rules():
    group("chip schedule", "high")
    priors = chip_model.DEFAULT_PRIORS
    chips = list(chip_model.CHIP_CODES)

    # Every chip wants the same gameweek. Only one can have it.
    m = _matrix({c: {10: 50.0, 11: 1.0, 12: 1.0, 13: 1.0, 14: 1.0} for c in chips})
    s = chip_model.schedule_chips(chips, 10, 14, m, priors)
    check("never two chips in one gameweek", "4 chips all peaking on GW10",
          "at most one chip per gameweek", s,
          lambda out: len(out) == len(set(out)), severity="critical",
          note="the one rule the feature is not allowed to break")

    # A standout week is taken over a poor one.
    m = _matrix({"bboost": {5: 13.0, 6: 30.0, 7: 13.0}})
    s = chip_model.schedule_chips(["bboost"], 5, 7, m, priors)
    expect("the best week wins", "bboost peaking on GW6", "bboost", s.get(6))

    # Slack plus nothing worth playing: hold everything.
    s = chip_model.schedule_chips(chips, 5, 19, _flat(chips, range(5, 20), 0.5),
                                  priors)
    expect("nothing is scheduled below its floor while there is time", "GW5",
           {}, s, note="spending a chip on a poor week in September is how a "
                       "season's chips get wasted")

    # Weeks run out: play them anyway. A chip unplayed scores nothing.
    s = chip_model.schedule_chips(chips, 16, 19, _flat(chips, range(16, 20), 0.5),
                                  priors)
    check("the deadline forces every chip out", "GW16, 4 chips, 4 weeks left",
          "all four scheduled despite being below the floor", s,
          lambda out: len(out) == 4 and set(out) == {16, 17, 18, 19},
          severity="critical",
          note="the GW18-with-four-chips case this feature exists to prevent")

    check("a chip is played in the forced week, not just planned", "GW16",
          "something lands on GW16", s, lambda out: out.get(16) in chips)

    # Fewer weeks than chips: fill what there is rather than failing.
    s = chip_model.schedule_chips(chips, 18, 19, _flat(chips, (18, 19), 10.0),
                                  priors)
    check("fewer weeks than chips still returns a legal plan",
          "4 chips, 2 gameweeks", "2 scheduled, no gameweek doubled", s,
          lambda out: len(out) == 2 and len(set(out)) == 2, severity="high")

    # One below-floor chip must not block the others.
    m = _matrix({"bboost": {5: 30.0, 6: 30.0}, "wildcard": {5: 0.1, 6: 0.1}})
    s = chip_model.schedule_chips(["bboost", "wildcard"], 5, 6, m, priors)
    check("a worthless chip does not stop a good one", "bboost 30, wildcard 0.1",
          "bboost scheduled", s, lambda out: "bboost" in out.values())

    expect("no chips available is not an error", "[]", {},
           chip_model.schedule_chips([], 5, 19, {}, priors))


def test_chip_schedule_fallback():
    group("chip schedule fallback", "high")
    priors = chip_model.DEFAULT_PRIORS
    chips = list(chip_model.CHIP_CODES)

    m = _matrix({c: {10: 50.0, 11: 1.0, 12: 1.0, 13: 1.0, 14: 1.0} for c in chips})
    s = chip_model._greedy_schedule(chips, [10, 11, 12, 13, 14], m, priors, 1)
    check("the greedy fallback still allows one chip a gameweek",
          "solver unavailable", "no gameweek doubled", s,
          lambda out: len(out) == len(set(out)), severity="critical",
          note="a deadline job must not fail because CBC did, but it must not "
               "produce an illegal plan either")

    forced = chip_model._greedy_schedule(
        chips, [16, 17, 18, 19], _flat(chips, range(16, 20), 0.5), priors, 0)
    expect("the fallback also ignores the floor once weeks run out", "slack 0",
           4, len(forced))


def test_chip_priors():
    group("chip priors", "high")

    priors = chip_model.load_priors()
    for chip in chip_model.CHIP_CODES:
        check(f"{chip} has a floor", "chip_priors.json", "a number, not negative",
              priors.get("floor", {}).get(chip),
              lambda v: isinstance(v, (int, float)) and v >= 0)

    check("a priors file with no floors falls back rather than raising",
          "floor missing", "0.0", safe(chip_model.floor, "bboost", {"floor": {}}),
          lambda v: v == 0.0, severity="medium",
          note="a bad file must not take a deadline down with it")

    # The order-statistic quantile is what makes the bot patient early and
    # decisive late. If it stops falling as the weeks run out, it never plays.
    q_many = chip_model.hold_quantile(15)
    q_few = chip_model.hold_quantile(2)
    q_one = chip_model.hold_quantile(1)
    check("holding is worth less as the weeks run out", "15 vs 2 vs 1 weeks",
          "the quantile falls monotonically", (q_many, q_few, q_one),
          lambda qs: qs[0] > qs[1] > qs[2], severity="high")
    check("the hold quantile is capped", "15 weeks", "at most 0.90", q_many,
          lambda q: q <= chip_model.MAX_HOLD_QUANTILE)

    check("confidence decays with lead time", "leads 0, 1, 4",
          "1.0 then falling",
          [chip_model.confidence(n) for n in (0, 1, 4)],
          lambda c: c[0] == 1.0 and c[0] > c[1] > c[2])

    check("an older scalar-per-context priors file still reads",
          "conditional.bboost.double = 19.0", "a number, not a crash",
          safe(chip_model.context_quantile, "bboost", "double", 0.5,
               {"quantiles": {"bboost": {"p50": 12.0}},
                "conditional": {"bboost": {"double": 19.0}}}),
          lambda v: isinstance(v, float), severity="medium")

    expect("an unknown context falls back to the pooled spread",
           "context 'mystery'", chip_model.prior_quantile("bboost", 0.5),
           chip_model.context_quantile("bboost", "mystery", 0.5))

    expect("quantile_of interpolates", "[0, 10], q=0.5", 5.0,
           chip_model.quantile_of([0, 10], 0.5))
    expect("quantile_of survives a single sample", "[7]", 7.0,
           chip_model.quantile_of([7], 0.9))
    expect("quantile_of survives none", "[]", None,
           chip_model.quantile_of([], 0.5))


def test_chip_gain_shapes():
    group("chip gain", "high")

    def player(pid, points, gw=5, **extra):
        return {"id": pid, "web_name": f"P{pid}", "pos": "MID", "cost": 5.0,
                "status": "a", "predicted": points,
                "next_gameweeks": [{"event": gw, "points": points}], **extra}

    bench = [dict(player(i, 4.0), starting=False) for i in range(1, 5)]
    starters = [dict(player(i, 6.0), starting=True) for i in range(5, 16)]
    starters[0]["is_captain"] = True
    lineup = {"squad": starters + bench, "predicted_points": 72.0}

    expect("bench boost is the bench's points", "4 bench players on 4.0 each",
           16.0, chip_model.bench_boost_gain(lineup, 5))
    expect("triple captain is one more multiple, not three",
           "captain projecting 6.0", 6.0,
           chip_model.triple_captain_gain(lineup, 5))

    # A double gameweek needs no special case: two entries under one event.
    doubled = dict(player(99, 5.0), starting=False)
    doubled["next_gameweeks"] = [{"event": 5, "points": 5.0},
                                 {"event": 5, "points": 4.0}]
    expect("a double gameweek is summed, not replaced", "two GW5 fixtures",
           9.0, chip_model._points_in(doubled, 5), severity="high")

    blanking = dict(player(98, 5.0), starting=False)
    blanking["next_gameweeks"] = [{"event": 6, "points": 5.0}]
    expect("a player with no fixture scores nothing that week",
           "fixture in GW6 only", 0.0, chip_model._points_in(blanking, 5),
           severity="high",
           note="falling back to his season projection here would value a "
                "bench boost on a blank week as if everyone played")

    # Free Hit and Wildcard cannot be evaluated against a squad that does not
    # exist yet, so they must never claim to be computable for a future week.
    squad = starters + bench
    expect("free hit is only computable for the gameweek being planned",
           "gw 6, now 5", False, chip_model.computable("freehit", 6, 5, squad))
    expect("wildcard likewise", "gw 6, now 5", False,
           chip_model.computable("wildcard", 6, 5, squad))
    expect("bench boost is computable while the pool reaches that gameweek",
           "gw 5, now 5", True, chip_model.computable("bboost", 5, 5, squad))
    expect("and not beyond it", "gw 12, now 5", False,
           chip_model.computable("bboost", 12, 5, squad))

    factors = chip_model.wildcard_factors(
        [dict(player(1, 5.0), status="i"), dict(player(2, 5.0)),
         dict(player(3, 0.1))], 5)
    check("wildcard factors count the injured", "one injured player",
          "injured >= 1", factors, lambda f: f["injured"] >= 1)
    check("wildcard factors count players contributing nothing",
          "one player projecting 0.1", "deadweight >= 1", factors,
          lambda f: f["deadweight"] >= 1,
          note="the reason the tab shows is built from these")


def test_bench_build_weight():
    group("bench boost build-up", "high")

    plan = {"schedule": [{"gameweek": 9, "chip": "bboost", "expected_gain": 21.0},
                         {"gameweek": 14, "chip": "3xc", "expected_gain": 11.0}]}
    check("the solver is told to value the bench just before a bench boost",
          "bboost scheduled for GW9, planning GW8", "a weight above zero",
          ai_manager.bench_build_weight(plan, 8), lambda w: w and w > 0,
          note="without this the planner schedules a bench boost and then "
               "fields a cheap bench into it")
    expect("and not months earlier", "planning GW3", None,
           ai_manager.bench_build_weight(plan, 3))
    expect("no bench boost scheduled means no change", "3xc only", None,
           ai_manager.bench_build_weight(
               {"schedule": [{"gameweek": 9, "chip": "3xc", "expected_gain": 1.0}]}, 8))
    expect("an empty plan is safe", "{}", None, ai_manager.bench_build_weight({}, 8))


def test_season_fixture_structure():
    group("season fixture structure", "high")

    fixtures = pd.DataFrame([
        # GW1 ordinary: 4 teams, 2 matches.
        {"event": 1, "team_h": 1, "team_a": 2},
        {"event": 1, "team_h": 3, "team_a": 4},
        # GW2: teams 1-4 all play twice.
        {"event": 2, "team_h": 1, "team_a": 2},
        {"event": 2, "team_h": 2, "team_a": 1},
        {"event": 2, "team_h": 3, "team_a": 4},
        {"event": 2, "team_h": 4, "team_a": 3},
        # An unscheduled fixture: no event yet.
        {"event": None, "team_h": 1, "team_a": 3},
    ])
    out = fixture_structure.season_outlook(fixtures=fixtures)

    expect("a gameweek where four teams play twice is a double", "GW2", True,
           out[2]["is_double"])
    expect("an ordinary gameweek is not", "GW1", False, out[1]["is_double"])
    expect("a short gameweek is a blank", "GW1, 4 teams playing", True,
           out[1]["is_blank"])
    expect("the doubling teams are named", "GW2", [1, 2, 3, 4],
           out[2]["doubling_teams"])

    check("an unscheduled fixture is not counted",
          "one row with event = None", "no gameweek beyond GW2", out,
          lambda o: max(o) == 2, severity="high",
          note="guessing which gameweek it belongs to would invent a double "
               "that does not exist - which is exactly how real ones appear")

    expect("context names the week type", "GW2", "double",
           fixture_structure.context(out[2]))
    expect("a missing gameweek reads as ordinary", "None", "normal",
           fixture_structure.context(None))

    expect("no fixture list returns None, not an empty plan", "empty frame",
           None, fixture_structure.season_outlook(
               fixtures=pd.DataFrame(columns=["event", "team_h", "team_a"])),
           severity="high",
           note="an empty dict would read as 'no doubles all season' rather "
                "than 'no data'")


def test_free_transfer_estimate():
    """How many free transfers a manager has for the gameweek being picked.

    The number that read 3 in a week where the answer was 1. FPL's rule is one
    per gameweek, banked up to five, spent by transferring - and GW1, where
    transfers are unlimited, is outside it entirely: it neither spends an
    allowance nor banks one.
    """
    group("free transfer estimate", "high")

    def history(rounds, chips=None):
        return {"current": [{"event": e, "event_transfers": t} for e, t in rounds],
                "chips": chips or []}

    est = team_service._estimate_free_transfers

    expect("preseason, nothing played yet", "no rounds in history",
           1, safe(est, history([])))
    # The live bug, pinned: one played gameweek is one free transfer, always.
    expect("after GW1 only", "GW1 played, 0 transfers",
           1, safe(est, history([(1, 0)])))
    expect("preseason churn doesn't cost the GW2 transfer",
           "GW1 played after 5 preseason changes",
           1, safe(est, history([(1, 5)])))
    expect("a quiet GW2 banks one", "GW1-2 played, none used in GW2",
           2, safe(est, history([(1, 0), (2, 0)])))
    expect("using it leaves one", "GW1-2, one transfer in GW2",
           1, safe(est, history([(1, 0), (2, 1)])))
    expect("taking a hit still leaves one", "GW1-2, two transfers in GW2",
           1, safe(est, history([(1, 0), (2, 2)])))
    expect("banked transfers cap at five", "six quiet gameweeks",
           5, safe(est, history([(g, 0) for g in range(1, 7)])))
    expect("and stay capped", "twelve quiet gameweeks",
           5, safe(est, history([(g, 0) for g in range(1, 13)])))
    expect("a wildcard week consumes nothing",
           "GW1-3 quiet, wildcard + 8 transfers in GW3",
           3, safe(est, history([(1, 0), (2, 0), (3, 8)],
                                [{"event": 3, "name": "wildcard"}])))
    expect("a free hit week consumes nothing",
           "GW1-3 quiet, free hit + 11 transfers in GW3",
           3, safe(est, history([(1, 0), (2, 0), (3, 11)],
                                [{"event": 3, "name": "freehit"}])))
    expect("rounds are walked in gameweek order, not list order",
           "history listing GW2 before GW1",
           2, safe(est, history([(2, 0), (1, 0)])))
    check("a missing history degrades to one rather than raising", "None",
          "1", safe(est, None), lambda v: v == 1)


def test_free_transfer_step():
    """The single-round rule both the bot and My Team now share."""
    group("free transfer step", "high")

    step = ai_manager.free_transfers
    expect("a quiet round banks one", "had 1, used 0", 2, safe(step, 1, 0))
    expect("spending it holds at one", "had 1, used 1", 1, safe(step, 1, 1))
    expect("never drops below one", "had 1, used 4", 1, safe(step, 1, 4))
    expect("never rises above five", "had 5, used 0", 5, safe(step, 5, 0))
    expect("a chip week ignores the transfers made", "had 2, used 9, on a chip",
           3, safe(step, 2, 9, True))


def test_live_overlay():
    """Provisional scores painted onto a frozen AI squad mid-gameweek.

    Display only - it must never be the thing that writes a number down. The
    scoring rule matches ai_team.backfill_actuals: starters only, captain
    doubled, extended by whichever chip is in play.
    """
    group("live overlay", "high")

    def squad():
        # Two clubs: 1 has kicked off, 2 has not.
        return [
            {"id": 10, "team": 1, "starting": True, "is_captain": True,
             "actual_points": None},
            {"id": 11, "team": 1, "starting": True, "is_captain": False,
             "actual_points": None},
            {"id": 12, "team": 2, "starting": True, "is_captain": False,
             "actual_points": None},
            {"id": 13, "team": 1, "starting": False, "is_captain": False,
             "actual_points": None},
        ]

    live = {10: 8, 11: 2, 12: 6, 13: 5}
    started = {1}

    def run(chip=None):
        return ai_team.live_overlay(
            squad(), 1, events=[{"id": 1, "finished": False, "data_checked": False}],
            chip=chip)

    saved = (ai_team.get_event_live, ai_team.started_teams)
    ai_team.get_event_live = lambda gw: live
    ai_team.started_teams = lambda gw: started
    try:
        plain = safe(run)
        # 8 doubled for the captain + 2. Player 12's club has not kicked off, so
        # his 6 is not counted and not shown - the whole point of the exercise.
        expect("captain doubled, unstarted club excluded",
               "clubs 1 started, 2 not; captain scored 8",
               18, plain["points"] if isinstance(plain, dict) else plain)
        scores = ([p["actual_points"] for p in plain["squad"]]
                  if isinstance(plain, dict) else plain)
        expect("a player whose match hasn't begun keeps no score",
               "player 12, club 2, not started",
               [8, 2, None, 5], scores)
        expect("provisional is always flagged", "an unfinished gameweek",
               True, plain["provisional"] if isinstance(plain, dict) else plain)

        boosted = safe(run, "bboost")
        expect("bench boost counts the bench too", "same squad, bboost",
               23, boosted["points"] if isinstance(boosted, dict) else boosted)

        tripled = safe(run, "3xc")
        expect("triple captain trebles rather than doubles", "same squad, 3xc",
               26, tripled["points"] if isinstance(tripled, dict) else tripled)

        # A settled gameweek has real stored numbers; overlaying provisional
        # ones over the top is exactly what must not happen.
        settled = safe(ai_team.live_overlay, squad(), 1,
                       [{"id": 1, "finished": True, "data_checked": True}])
        expect("a settled gameweek is left alone", "finished + data_checked",
               None, settled)

        ai_team.started_teams = lambda gw: None
        blind = safe(ai_team.live_overlay, squad(), 1,
                     [{"id": 1, "finished": False, "data_checked": False}])
        expect("no fixture data means no overlay at all",
               "started_teams unavailable", None, blind)
    finally:
        ai_team.get_event_live, ai_team.started_teams = saved


def test_upcoming_fixture_horizon():
    """The filter that used to be `finished == False`.

    None of this was covered, which is how a played gameweek came to sit at the
    top of every player's projection for a day after each round: FPL only flips
    `finished` once `data_checked` does, and until then a finished match still
    reads as upcoming.
    """
    group("upcoming fixture horizon", "high")

    # The exact GW1 shape from 2026-27: played, but not yet confirmed.
    fixtures = pd.DataFrame([
        {"event": 1, "team_h": 1, "team_a": 2, "finished": False,
         "finished_provisional": True, "started": True},
        {"event": 1, "team_h": 3, "team_a": 4, "finished": False,
         "finished_provisional": True, "started": True},
        {"event": 2, "team_h": 1, "team_a": 3, "finished": False,
         "finished_provisional": False, "started": False},
        {"event": 3, "team_h": 2, "team_a": 4, "finished": False,
         "finished_provisional": False, "started": False},
        {"event": None, "team_h": 1, "team_a": 4, "finished": False,
         "finished_provisional": False, "started": False},
    ])

    expect("the season clock wins when it has an answer",
           "next_gameweek=2", 2,
           fixture_structure.first_upcoming_event(fixtures, 2))

    expect("without the clock, a played-but-unconfirmed round is still skipped",
           "finished=False, finished_provisional=True, started=True", 2,
           fixture_structure.first_upcoming_event(fixtures),
           note="`finished` alone would have answered 1")

    horizon = fixture_structure.upcoming_fixtures(fixtures, 2)
    check("a played gameweek is excluded from the horizon", "GW1 rows", "none",
          int((horizon["event"] == 1).sum()), lambda n: n == 0)
    expect("the rounds that remain are kept", "GW2, GW3", [2.0, 3.0],
           sorted(horizon["event"].dropna().tolist()))
    check("an unscheduled fixture is not guessed at", "event=None", "excluded",
          len(horizon), lambda n: n == 2,
          note="same rule the double/blank maths uses")

    expect("nothing left to play returns an empty frame", "from_event=None", 0,
           len(fixture_structure.upcoming_fixtures(fixtures, None)))

    done = fixtures.assign(finished=True, started=True, finished_provisional=True)
    expect("a finished season has no anchor", "every fixture played", None,
           fixture_structure.first_upcoming_event(done))


def test_rotation_difficulty_spread():
    """The rotator drew every fixture green for the first weeks of 2026-27.

    Not a colour bug: FPL leaves its strength columns at zero until well into
    the season, the substitution that fills them in was gated on preseason
    mode, and `detect_mode` had already flipped. Every difficulty came out as
    0 - 0, and a flat range has no scale to draw.
    """
    group("rotation difficulty", "high")

    fixtures = pd.DataFrame([
        {"event": 1, "team_h": 1, "team_a": 2, "finished": False,
         "finished_provisional": True, "started": True},
        {"event": 2, "team_h": 1, "team_a": 2, "finished": False,
         "finished_provisional": False, "started": False},
        {"event": 3, "team_h": 2, "team_a": 1, "finished": False,
         "finished_provisional": False, "started": False},
    ])
    empty = pd.DataFrame([
        {"id": 1, "short_name": "AAA", "strength_attack_home": 0,
         "strength_attack_away": 0, "strength_defence_home": 0,
         "strength_defence_away": 0},
        {"id": 2, "short_name": "BBB", "strength_attack_home": 0,
         "strength_attack_away": 0, "strength_defence_home": 0,
         "strength_defence_away": 0},
    ])

    expect("all-zero strength columns are detected", "a fresh season's teams.csv",
           True, fixture_rotator.strength_data_is_empty(empty))

    flat = fixture_rotator.build_rotation_table(fixtures, empty, from_event=2)
    check("zero strength gives every fixture the same difficulty",
          "the state that produced the all-green grid", "no spread",
          float(flat["defensive_difficulty"].std()), lambda s: s == 0,
          note="which is why the colour scale must not paint this green")

    real = empty.copy()
    real.loc[0, ["strength_attack_home", "strength_attack_away",
                 "strength_defence_home", "strength_defence_away"]] = [1300, 1250, 1200, 1150]
    real.loc[1, ["strength_attack_home", "strength_attack_away",
                 "strength_defence_home", "strength_defence_away"]] = [1050, 1000, 980, 940]
    spread = fixture_rotator.build_rotation_table(fixtures, real, from_event=2)
    check("real strength gives a spread to colour", "populated teams.csv",
          "non-zero", float(spread["defensive_difficulty"].std()), lambda s: s > 0)

    check("the rotation grid starts at the anchor, not at GW1",
          "from_event=2", "no GW1 column",
          sorted(spread["event"].unique().tolist()), lambda e: 1 not in e)


def test_flat_difficulty_renders_neutral():
    """A degenerate range must read as "we don't know", not "every fixture is
    easy". Green for all twenty clubs is the one answer that cannot be true."""
    group("flat difficulty colouring", "high")

    rotation = pd.DataFrame([
        {"team": 1, "team_name": "AAA", "event": 2, "opponent": "BBB",
         "was_home": True, "defensive_difficulty": 0.0, "attacking_difficulty": 0.0},
        {"team": 2, "team_name": "BBB", "event": 2, "opponent": "AAA",
         "was_home": False, "defensive_difficulty": 0.0, "attacking_difficulty": 0.0},
    ])
    block = seo_tables.rotation_block(rotation, "defensive_difficulty")
    colours = {c["colour"] for team in block["teams"] for c in team["cells"]}

    check("a flat range is not painted green", "every difficulty 0.0",
          "no green", colours,
          lambda cs: not any(str(c).startswith("hsl(120") for c in cs),
          note="green here claimed twenty easy runs off a table of zeroes")
    expect("it is painted the neutral no-data colour", "every difficulty 0.0",
           {seo_tables.NO_DIFFICULTY_COLOUR}, colours)

    varied = rotation.copy()
    varied.loc[0, "defensive_difficulty"] = -200.0
    varied.loc[1, "defensive_difficulty"] = 200.0
    block = seo_tables.rotation_block(varied, "defensive_difficulty")
    colours = [c["colour"] for team in block["teams"] for c in team["cells"]]
    check("a real range still spans the scale", "-200 vs 200",
          "green and red present", colours,
          lambda cs: any(str(c).startswith("hsl(120") for c in cs)
          and any(str(c).startswith("hsl(0") for c in cs))


def test_fixture_runs_needs_a_spread():
    """`fixture_runs` used to award 10.0/10 to every club when the difficulties
    were identical, and print the first three alphabetically as the league's
    kindest run. That is how "Arsenal have the kindest attacking run" reached a
    live briefing off a table of zeroes."""
    group("fixture runs", "high")

    flat = pd.DataFrame([
        {"team_name": s, "event": e, "opponent": "XXX", "was_home": True,
         "attacking_difficulty": 0.0, "defensive_difficulty": 0.0}
        for s in ("ARS", "BOU", "CHE") for e in (2, 3, 4)
    ])
    out = gwr.fixture_runs(flat, {}, 2)
    expect("no attacking runs are claimed off a flat table", "every club 0.0",
           [], out["attack"])
    expect("and none defensively", "every club 0.0", [], out["defence"])

    varied = flat.copy()
    varied.loc[varied.team_name == "ARS", "attacking_difficulty"] = -500.0
    varied.loc[varied.team_name == "CHE", "attacking_difficulty"] = 500.0
    out = gwr.fixture_runs(varied, {}, 2)
    check("a real spread still produces runs", "ARS easiest", "ARS first",
          [r["team_short"] for r in out["attack"]], lambda r: r[:1] == ["ARS"])
    check("and the easiest run scores 10", "ARS", 10.0,
          out["attack"][0]["ease"], lambda e: e == 10.0)


def test_form_blend_weight():
    """Current-season results used to contribute nothing until three gameweeks
    existed, then last season was dropped entirely overnight. It is a shrinkage
    blend now, so GW1 counts from the night its stats land."""
    group("form blend weight", "high")

    cases = [(0, 0.0), (1, 0.25), (2, 0.4), (3, 0.5), (6, 2 / 3), (19, 0.864)]
    for games, expected in cases:
        check(f"{games} gameweek(s) played", f"games_seen={games}",
              f"~{expected:.3f}", rating_model.current_form_weight(games),
              lambda w, e=expected: abs(w - e) < 0.001)

    check("a played gameweek moves the numbers immediately", "games_seen=1",
          "> 0", rating_model.current_form_weight(1), lambda w: w > 0,
          note="the whole point - this was 0 under the old switch")
    check("the weight never reaches 1", "games_seen=38", "< 1",
          rating_model.current_form_weight(38), lambda w: w < 1.0)
    check("the ramp is monotone", "0..38 games", "increasing",
          [rating_model.current_form_weight(n) for n in range(39)],
          lambda ws: all(b > a for a, b in zip(ws, ws[1:])))
    expect("junk is treated as no games played", "games_seen=None", 0.0,
           rating_model.current_form_weight(None))

    expect("the fallback threshold is still three gameweeks",
           "current_form_weight(3)", rating_model.FALLBACK_FORM_WEIGHT,
           rating_model.current_form_weight(rating_model.MIN_CURRENT_GAMEWEEKS),
           note="so the captain-pick suppression and calibrators flip when they always did")


def test_gameweek_stats_schemas():
    """The two seasons' gameweek_stats.csv files do NOT share a schema, and the
    ratings read both.

    Last season's is the seeded historical dataset (`player_id`). The current
    season's is written from FPL's element-summary history, which names the
    column `element` and carries an appended `player_id` as well - so it has
    both. Renaming `element` unconditionally made two columns of the same name,
    `frame['player_id']` became a DataFrame, and `.map(dict)` raised
    "the first argument must be callable". That took every projection on the
    site down, and it went unnoticed because until the blend landed nothing
    ever read the current-season file.
    """
    group("gameweek stats schemas", "critical")

    players = pd.DataFrame([{"id": 7, "code": 700}, {"id": 8, "code": 800}])

    def load(rows):
        with tempfile.TemporaryDirectory() as d:
            gw = os.path.join(d, "gw.csv")
            pl = os.path.join(d, "players.csv")
            pd.DataFrame(rows).to_csv(gw, index=False)
            players.to_csv(pl, index=False)
            return rating_model._load_gameweek_stats(gw, pl)

    # The current season's shape: element AND player_id, identical values.
    both = load([{"element": 7, "player_id": 7, "round": 1, "minutes": 90,
                  "total_points": 6}])
    check("a file carrying both element and player_id loads",
          "current season's schema", "one row keyed on code", both,
          lambda f: len(f) == 1 and int(f["code"].iloc[0]) == 700)
    check("and does not end up with a duplicate column",
          "element renamed onto an existing player_id", "no duplicates",
          both.columns.tolist(), lambda c: len(c) == len(set(c)),
          note="the duplicate is what made .map() raise")
    check("player_id stays a Series, not a frame", "current season's schema",
          "Series", both["player_id"], lambda v: isinstance(v, pd.Series))

    # Last season's shape: player_id only.
    only = load([{"player_id": 8, "round": 5, "minutes": 45, "total_points": 2}])
    expect("the seeded historical schema still loads", "player_id only", 800,
           int(only["code"].iloc[0]))

    # FPL's raw shape, if the appended column ever goes away.
    element_only = load([{"element": 8, "round": 5, "minutes": 45,
                          "total_points": 2}])
    expect("and so does element on its own", "FPL history as-is", 800,
           int(element_only["code"].iloc[0]))

    unknown = load([{"element": 99, "player_id": 99, "round": 1, "minutes": 0,
                     "total_points": 0}])
    expect("a player with no code is dropped rather than carried as NaN",
           "an id not in players.csv", 0, len(unknown))


def test_form_blend():
    """The blend itself: who moves, who doesn't, and who stops being dropped."""
    group("form blend", "high")

    def frame(rows):
        f = pd.DataFrame(rows).set_index("code")
        f.index.name = "code"
        return f

    prior = frame([
        {"code": 1, "total_points_roll3": 2.0, "games_seen": 38.0},
        {"code": 2, "total_points_roll3": 6.0, "games_seen": 38.0},
    ])
    current = frame([
        {"code": 1, "total_points_roll3": 10.0, "games_seen": 1.0},
        {"code": 3, "total_points_roll3": 8.0, "games_seen": 1.0},
    ])
    out = rating_model._blend_form(current, prior)

    check("a player who played is pulled toward this season", "code 1, w=0.25",
          "0.25*10 + 0.75*2 = 4.0", float(out.loc[1, "total_points_roll3"]),
          lambda v: abs(v - 4.0) < 1e-9)

    check("a player who did not play keeps the prior exactly", "code 2",
          6.0, float(out.loc[2, "total_points_roll3"]),
          lambda v: abs(v - 6.0) < 1e-9,
          note="w comes from HIS appearances, not the league's gameweek count")

    check("a player with no prior is shrunk toward a typical one, not crowned",
          "code 3, one game, no last season", "between the median and his own game",
          float(out.loc[3, "total_points_roll3"]), lambda v: 2.0 < v < 8.0)

    check("nobody loses coverage to the blend", "3 distinct codes", 3, len(out),
          lambda n: n == 3,
          note="the 180-minute filter used to drop new signings outright")

    check("games_seen stays above the predict_ratings gate", "every player",
          "> 0", out["games_seen"], lambda s: bool((s > 0).all()),
          note="a zero here would drop the player off the site entirely")

    empty = prior.iloc[0:0]
    expect("an empty current season is the prior untouched", "no current rows",
           6.0, float(rating_model._blend_form(
               empty, prior).loc[2, "total_points_roll3"]))
    expect("and an empty prior is the current season untouched", "no prior rows",
           10.0, float(rating_model._blend_form(
               current, empty).loc[1, "total_points_roll3"]))


def test_settled_lineup_flags():
    """What a settled gameweek says about substitutions, on the pitch.

    The score being right is only half of it. The AI Manager's GW1 keeper
    recorded no minutes and the bench keeper replaced him, and once that was
    reflected in the total the page still drew the eleven that were PICKED - so
    the container said one thing and the squad above it showed another, with
    nothing connecting the two.

    `effective_multiplier` is the connection: FPL's own encoding, written by the
    backfill, and everything the pitch needs is derivable from it.
    """
    group("settled lineup flags", "high")

    import ai_team
    import autosubs

    class Row(dict):
        """A stand-in for sqlite3.Row, which the flags reader probes with
        .keys() so it stays safe on a database that predates the column."""

    starter_out = Row(position=1, effective_multiplier=0)
    bench_in = Row(position=12, effective_multiplier=1)
    armband = Row(position=2, effective_multiplier=2)
    ordinary = Row(position=3, effective_multiplier=1)

    expect("a starter multiplied by nothing was substituted off", "position 1, x0",
           True, ai_team.settled_flags(starter_out)["auto_sub_out"])
    expect("a bench player multiplied by anything came on", "position 12, x1",
           True, ai_team.settled_flags(bench_in)["auto_sub_in"])
    expect("a doubled pick wore the armband", "x2",
           True, ai_team.settled_flags(armband)["wore_armband"])
    check("an ordinary starter is flagged as nothing at all", "position 3, x1",
          "no flags", ai_team.settled_flags(ordinary),
          lambda f: not any(f.values()))

    check("an unscored round claims nothing", "multiplier NULL",
          "no flags", ai_team.settled_flags(Row(position=1, effective_multiplier=None)),
          lambda f: not any(f.values()),
          note="the squad as picked, which is all there is to show")

    check("and neither does a database that predates the column", "no such key",
          "no flags", ai_team.settled_flags(Row(position=1)),
          lambda f: not any(f.values()),
          note="the migration adds it, but a read must not depend on having run")

    # The multipliers themselves, against the rules that produce them.
    LAYOUT = ["GK"] + ["DEF"] * 4 + ["MID"] * 4 + ["FWD"] * 2 + ["GK", "DEF", "MID", "FWD"]
    squad = [{"id": i, "pos": pos, "position": i, "starting": i <= 11,
              "is_captain": i == 2, "is_vice_captain": i == 3}
             for i, pos in enumerate(LAYOUT, start=1)]
    minutes = {i: 90 for i in range(1, 16)}

    mults = autosubs.multipliers(squad, autosubs.apply(squad, minutes))
    check("a normal week doubles one player and benches four", "everyone played",
          "1 captain, 4 zeros", mults,
          lambda m: m[2] == 2 and sum(1 for v in m.values() if v == 0) == 4)

    subbed = autosubs.apply(squad, {**minutes, 1: 0})
    smults = autosubs.multipliers(squad, subbed)
    check("the keeper who didn't play is zeroed and his cover counted",
          "GK on 0 minutes", "1 -> 0, 12 -> 1", smults,
          lambda m: m[1] == 0 and m[12] == 1)

    cmults = autosubs.multipliers(squad, autosubs.apply(squad, {**minutes, 2: 0}))
    check("and the armband moves by the same arithmetic", "captain on 0 minutes",
          "3 -> 2", cmults, lambda m: m[3] == 2 and m[2] != 2)

    boosted = autosubs.multipliers(
        squad, autosubs.apply(squad, minutes, chip="bboost"), chip="bboost")
    check("a bench boost leaves nobody on nought", "bboost",
          "no zeros", boosted, lambda m: all(v > 0 for v in m.values()))


def test_draft_applies_to_one_gameweek():
    """A saved team is shown back, and only where it belongs.

    Saving worked; showing it back did not. Every load rebuilt the squad from
    FPL, so stepping to another gameweek and returning silently discarded the
    changes - the team you had just saved was the one thing the page would not
    show you.

    The conditions on when a draft applies are each a way of showing somebody
    the wrong team, so each is tested rather than assumed.
    """
    group("saved team applies", "high")

    from team_service import _draft_picks

    squad = [{"id": 100 + i, "position": i, "is_captain": i == 1,
              "is_vice_captain": i == 2} for i in range(1, 16)]
    draft = {"gameweek": 3, "source": "user", "squad": squad}

    picks = _draft_picks(draft, 3, current_event=2)
    check("the saved team is used for the round it was saved for",
          "GW3 draft, viewing GW3, GW2 in play", "15 picks", picks,
          lambda p: p is not None and len(p) == 15)
    check("and carries the armband and the bench with it", "same draft",
          "captain x2, bench x0", picks,
          lambda p: p[0]["multiplier"] == 2 and p[14]["multiplier"] == 0)

    check("a draft for another gameweek is ignored", "GW3 draft, viewing GW1",
          None, _draft_picks(draft, 1, current_event=2), lambda v: v is None,
          note="showing it would present a squad the manager never had")

    check("a settled gameweek ignores it too", "GW2 draft, viewing GW2, GW2 played",
          None, _draft_picks({**draft, "gameweek": 2}, 2, current_event=2),
          lambda v: v is None,
          note="what happened is not a matter of preference")

    check("the deadline snapshot is not treated as a preview", "source 'official'",
          None, _draft_picks({**draft, "source": "official"}, 3, current_event=2),
          lambda v: v is None,
          note="FPL's own copy is fresher and includes any real transfer since")

    check("a squad that isn't fifteen is refused", "14 picks",
          None, _draft_picks({**draft, "squad": squad[:14]}, 3, current_event=2),
          lambda v: v is None)

    check("and no draft at all is simply no draft", "None",
          None, _draft_picks(None, 3, current_event=2), lambda v: v is None)


def test_rescore_queue():
    """Gameweeks the backfill still owes work to.

    "points IS NULL" was the only condition, which meant a round scored under
    rules that were later corrected could never be revisited - the round most in
    need of rescoring was precisely the one this would never look at again. The
    marker is `effective_multiplier`, written by the same pass that writes the
    total, so a round heals once and then leaves the queue for good.
    """
    group("rescore queue", "high")

    import inspect
    import re

    import ai_team
    import manager_history

    for name, fn in (("manager", manager_history.gameweeks_awaiting_actuals),
                     ("Best XI", ai_team.snapshots_awaiting_actuals)):
        sql = " ".join(inspect.getsource(fn).split())
        check(f"the {name} queue looks for an unscored round", "the query",
              "IS NULL on the total", sql,
              lambda q: re.search(r"(?:^|\.|\s)(?:points|actual_points) IS NULL", q)
                        is not None)
        check(f"and for one scored by an older build", "the query",
              "IS NULL on effective_multiplier", sql,
              lambda q: "effective_multiplier IS NULL" in q,
              note="without this a scoring fix can never reach a settled round")

    # The manual escape hatch, for a change that lands after every round has
    # already been re-scored and the marker is present everywhere.
    src = " ".join(inspect.getsource(ai_team.clear_settled_scoring).split())
    check("clearing resets only the derived figures", "clear_settled_scoring",
          "no DELETE, predictions untouched", src,
          lambda q: "DELETE" not in q.upper() and "predicted_points" not in q,
          note="the frozen predictions are what make the track record a record")


def test_manager_points_backfill():
    """The AI Manager's gameweek score, which sat at "pending" all week.

    Two independent faults kept it there. `backfill_manager_actuals` wrote
    every pick's real score and never touched `manager_team.points` - the
    column the history API serves and the cumulative total is summed from - so
    the squad view showed real returns beside a track record that said nothing.
    And in the nightly job the manager backfill was nested inside the Best XI's
    success branch, on a loop that skipped any gameweek the Best XI had already
    settled: one attempt, never retried.
    """
    group("manager points backfill", "high")

    import manager_history as mh

    class FakeConn:
        """Just enough of a connection for the two pure helpers."""
        def __init__(self, rows, hits=0):
            self.rows, self.hits = rows, hits

        def execute(self, sql, args=()):
            if "ai_transfer_log" in sql:
                return _FakeCursor([[self.hits]])
            return _FakeCursor(self.rows)

    class _FakeCursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    # A legal fifteen: 1-4-4-2 starting, GK/DEF/MID/FWD on the bench. The
    # stored multiplier is deliberately left at 1 throughout - the total is
    # derived from the chip and the substitution rules now, not read off a
    # column written before the round was played. See _team_points_from_picks.
    LAYOUT = (["GK"] + ["DEF"] * 4 + ["MID"] * 4 + ["FWD"] * 2
              + ["GK", "DEF", "MID", "FWD"])

    def squad(points=3, captain=1, bench_points=None, blanks=()):
        """Fifteen picks, and the position/club index that goes with them."""
        rows, index = [], {}
        for i, pos in enumerate(LAYOUT, start=1):
            scored = 0 if i in blanks else (
                points if i <= 11 else
                (points if bench_points is None else bench_points))
            rows.append({"element_id": i, "position": i,
                         "actual_points": scored, "multiplier": 1,
                         "is_captain": i == captain,
                         "is_vice_captain": i == captain + 1})
            # Every player at his own club, so nothing collides on the pitch.
            index[i] = {"pos": pos, "team": i}
        # Everyone played 90 unless named in `blanks`.
        minutes = {i: (0 if i in blanks else 90) for i in range(1, 16)}
        return rows, index, minutes

    rows, index, minutes = squad()
    expect("a captain's double is counted once, not twice", "11 starters, 1 captain",
           36, mh._team_points_from_picks(FakeConn(rows), 1, minutes=minutes,
                                          index=index))

    expect("a points hit comes off the total", "36 scored, one -4 hit",
           32, mh._team_points_from_picks(FakeConn(rows), 1, hits=4,
                                          minutes=minutes, index=index))

    none_yet = [{"element_id": 1, "position": 1, "actual_points": None,
                 "multiplier": 1, "is_captain": 0, "is_vice_captain": 0}]
    check("an un-backfilled gameweek stays pending rather than reading zero",
          "no pick has an actual score", None,
          mh._team_points_from_picks(FakeConn(none_yet), 1),
          lambda v: v is None,
          note="0 would look like a real score of nothing")

    partial = [dict(r, actual_points=(5 if r["element_id"] == 2 else None))
               for r in rows]
    expect("a partly-settled gameweek counts what it has", "one of two scored",
           5, mh._team_points_from_picks(FakeConn(partial), 1, minutes=minutes,
                                         index=index))

    # The chip, not the stored multiplier. The column says 1 in every row here
    # and the answers below still come out right, which is the whole point:
    # a multiplier is written at capture time and the chip is what the round
    # was actually played under.
    expect("a triple captain trebles the armband", "3xc, 11 x 3 with one tripled",
           39, mh._team_points_from_picks(FakeConn(rows), 1, chip="3xc",
                                          minutes=minutes, index=index))

    # The regression this pair exists for. The old query read
    # `WHERE position <= 11`, so a Bench Boost was scored as an ordinary week
    # and the chip was spent for nothing.
    boosted, bindex, bminutes = squad(points=3, bench_points=2)
    expect("a bench boost scores the bench too", "11 x 3 + captain, plus 4 x 2",
           44, mh._team_points_from_picks(FakeConn(boosted), 1, chip="bboost",
                                          minutes=bminutes, index=bindex))
    expect("and without the chip the same bench scores nothing", "no chip",
           36, mh._team_points_from_picks(FakeConn(boosted), 1,
                                          minutes=bminutes, index=bindex))

    # The other regression: an unused starter is replaced by the first bench
    # player who played, exactly as FPL does it. Here the keeper blanks, so the
    # bench keeper comes on - a bench outfielder cannot, whatever he scored.
    subbed, sindex, sminutes = squad(points=3, captain=2, blanks=(1,))
    expect("a starter who didn't play is substituted", "GK blanked, bench GK on",
           36, mh._team_points_from_picks(FakeConn(subbed), 1, minutes=sminutes,
                                          index=sindex))

    # And the armband goes with it: captain blanks, vice takes over and is the
    # one doubled.
    capless, cindex, cminutes = squad(points=3, captain=1, blanks=(1,))
    expect("the armband moves to the vice-captain", "captain blanked",
           36, mh._team_points_from_picks(FakeConn(capless), 1,
                                          minutes=cminutes, index=cindex))


def test_performance_gap_season():
    """Under/overperformers describe the season being played.

    They used to fall back to last season per player for anyone under a
    180-minute floor - a floor nobody can clear until the third gameweek. So
    for the opening fortnight every row came from a season that had finished,
    under a heading about the one that had started.
    """
    group("performance gap season", "high")

    def frame(minutes):
        return pd.DataFrame([{
            "id": 1, "code": 101, "web_name": "Test", "element_type": 4,
            "team": 1, "team_code": 3, "now_cost": 70, "minutes": minutes,
            "expected_goals": 2.0, "goals_scored": 0.0,
            "expected_goals_conceded": 0.0, "goals_conceded": 0.0,
            "next_gameweeks": [],
        }])

    started = team_service.get_performance_gap_players({"Forward": frame(90)})
    expect("once the season starts the table is about this season",
           "90 minutes played", "this season", started["season"])
    check("and a player with one match qualifies", "90 minutes", 1,
          len(started["results"]), lambda n: n == 1)
    if started["results"]:
        expect("the row is labelled this season", "90 minutes", "this season",
               started["results"][0]["season"])

    thin = team_service.get_performance_gap_players({"Forward": frame(20)})
    check("a cameo is still excluded", "20 minutes", 0, len(thin["results"]),
          lambda n: n == 0,
          note="the floor drops to 60, it does not disappear")

    preseason = team_service.get_performance_gap_players({"Forward": frame(0)})
    expect("before a ball is kicked, last season is the honest answer",
           "0 minutes across the pool", "last season", preseason["season"])

    check("the floor is one substantial appearance, not two full matches",
          "MIN_MINUTES_GAP", 60, team_service.MIN_MINUTES_GAP,
          lambda v: v == 60)


def test_prev_season_totals():
    """Last season's totals, for the comparison column on a player page.

    The aggregate previously carried five columns, all of them for the
    performance-gap table. A player page needs the ones a reader recognises -
    points, assists, clean sheets, bonus - plus an appearance count, so points
    per game can be shown on the basis FPL uses.
    """
    group("previous season totals", "medium")

    for col in ("total_points", "assists", "clean_sheets", "bonus", "starts",
                "expected_assists", "minutes", "goals_scored"):
        check(f"{col} is aggregated", "_PREV_STAT_COLS", "present", col,
              lambda c: c in team_service._PREV_STAT_COLS)

    totals = team_service._prev_season_stats_by_code()
    if totals:
        sample = next(iter(totals.values()))
        check("an appearance count is derived", "any player", "appearances present",
              sorted(sample.keys()), lambda k: "appearances" in k)
        check("points per game comes with it", "any player",
              "points_per_game present", sorted(sample.keys()),
              lambda k: "points_per_game" in k)

        # Appearances count matches played, not gameweek rows - an unused
        # substitute has a row and did not appear. The busiest player last
        # season has 39 from 47 rows, which is the two things this is checking
        # at once: the 0-minute rows are excluded, and a double gameweek
        # legitimately puts a player above the 38 rounds in a season. So the
        # bound is on matches a club can actually play, not on gameweeks.
        bad = [c for c, r in totals.items()
               if r.get("appearances") and r["appearances"] > 46]
        check("appearances stay within a plausible season",
              "appearances per player", "<= 46 (38 rounds plus doubles)",
              len(bad), lambda n: n == 0)

        rows_counted = [c for c, r in totals.items() if r.get("appearances") == 47]
        check("unused-substitute rows are not counted as appearances",
              "the busiest player's raw row count is 47", 0, len(rows_counted),
              lambda n: n == 0)

        ppg_wrong = [
            c for c, r in totals.items()
            if r.get("points_per_game") is not None and r.get("appearances")
            and abs(r["points_per_game"] - r["total_points"] / r["appearances"]) > 0.06]
        check("points per game reconciles with the totals it came from",
              "total_points / appearances", 0, len(ppg_wrong), lambda n: n == 0)

    check("the eligibility floor is two full matches",
          "PREV_SEASON_MIN_MINUTES", 180, team_service.PREV_SEASON_MIN_MINUTES,
          lambda v: v == 180)


def test_events_cache():
    """The season clock is fetched once per TTL, and never regresses to [].

    bootstrap-static is 1.8 MB and every page that asks what gameweek it is used
    to pull the whole thing. The two properties that matter are covered here:
    repeated calls inside the window make ONE request, and a failed fetch serves
    the last good answer rather than [] - because [] is not "we couldn't reach
    FPL", it is "no gameweek has started", which the player pages render as a
    year-old caption on every set of numbers at once.
    """
    group("events cache", "high")

    import gameweek as gwc

    calls = []
    sample = [{"id": 1, "deadline_time": "2026-08-14T17:30:00Z", "finished": True},
              {"id": 2, "deadline_time": "2026-08-21T17:30:00Z", "finished": False}]

    real_get = gwc._get
    try:
        gwc._get = lambda url: (calls.append(url), {"events": sample})[1]
        gwc.clear_events_cache()

        first = gwc.get_events()
        for _ in range(9):
            gwc.get_events()
        expect("ten calls inside the TTL make one request",
               "get_events() x10", 1, len(calls),
               note="each miss would be a 1.8 MB download from FPL")
        expect("the cached value is the fetched value", "get_events()",
               sample, first)

        # force=True is what the deadline watcher passes: it acts on the clock
        # rather than rendering it, so it must never read a cached copy.
        gwc.get_events(force=True)
        expect("force=True bypasses the cache", "get_events(force=True)",
               2, len(calls))

        # A failed fetch must not blank the clock.
        gwc._get = lambda url: (calls.append(url), None)[1]
        during_outage = gwc.get_events(force=True)
        expect("a failed fetch serves the last good events",
               "get_events(force=True) with _get -> None", sample, during_outage,
               note="[] would be read as 'no gameweek has started'")

        # ...but a process that has NEVER succeeded still reports honestly.
        gwc.clear_events_cache()
        cold = gwc.get_events()
        expect("a cold start with no API returns []",
               "clear_events_cache(); get_events()", [], cold)
    finally:
        gwc._get = real_get
        gwc.clear_events_cache()


def test_team_map_retries_after_failure():
    """A failed first fetch must not be remembered forever.

    The bug this covers: the map was memoised on `is None`, so an empty dict
    from an unreachable API was cached as though it were an answer and every
    club on the site stayed nameless until the process restarted.
    """
    group("team lookups", "high")

    import team_service as ts

    real_get, real_short = ts._get, ts._TEAM_SHORT
    try:
        ts._TEAM_SHORT = None
        ts._get = lambda url: None
        expect("an outage yields no short names", "_team_short_map() offline",
               {}, ts._team_short_map())

        ts._get = lambda url: {"teams": [{"id": 1, "short_name": "ARS"},
                                         {"id": 2, "short_name": "AVL"}]}
        recovered = ts._team_short_map()
        expect("the next call retries and populates",
               "_team_short_map() once the API is back",
               {1: "ARS", 2: "AVL"}, recovered,
               note="the failure must not be cached, only the answer")

        calls = []
        ts._get = lambda url: (calls.append(url), {"teams": []})[1]
        ts._team_short_map()
        expect("a populated map is not refetched", "_team_short_map() again",
               0, len(calls))
    finally:
        ts._get, ts._TEAM_SHORT = real_get, real_short


def test_known_manager_bounds():
    """The deadline job's work queue stays finite.

    /api/team records every id it is asked about and is anonymous, so the list
    snapshot_managers walks is attacker-growable. Two bounds are asserted: the
    per-run cap, and the idle prune that stops the table growing in the first
    place.
    """
    group("known managers", "high")

    import datetime as _dt
    import db as _db
    import jobs

    now = _dt.datetime.now(_dt.timezone.utc)
    fresh = now.isoformat()
    ancient = (now - _dt.timedelta(days=400)).isoformat()

    with _db.connect() as conn:
        conn.execute("DELETE FROM known_manager")
        # 20 ids seen today, 5 last seen over a year ago.
        conn.executemany(
            "INSERT INTO known_manager (fpl_id, first_seen, last_seen) "
            "VALUES (?, ?, ?)",
            [(900000 + i, fresh, fresh) for i in range(20)]
            + [(950000 + i, ancient, ancient) for i in range(5)])

    expect("every id is counted", "count_known_managers()",
           25, _db.count_known_managers())

    capped = _db.known_managers(limit=10)
    expect("the limit is honoured", "known_managers(limit=10)", 10, len(capped))
    check("the cap keeps the most recently active",
          "known_managers(limit=10)", "no id last seen a year ago",
          sorted(capped), lambda ids: all(i < 950000 for i in ids),
          note="ordering by fpl_id would have selected close to arbitrarily")

    check("snapshot_managers has a ceiling", "jobs.MAX_MANAGERS_PER_RUN",
          "a positive int well below the number that stalls the hourly job",
          jobs.MAX_MANAGERS_PER_RUN,
          lambda n: isinstance(n, int) and 0 < n <= 5000,
          note="each manager costs 2-3 serial FPL API calls")

    pruned = _db.prune_idle_managers(days=90, now=now)
    expect("idle ids are forgotten", "prune_idle_managers(days=90)",
           5, pruned["removed"])
    expect("active ids are kept", "count_known_managers() after the prune",
           20, _db.count_known_managers())

    # An id with real data behind it is governed by the retention period, not
    # by this prune - it must survive however long ago it was last seen.
    with _db.connect() as conn:
        conn.execute("INSERT INTO known_manager (fpl_id, first_seen, last_seen) "
                     "VALUES (?, ?, ?)", (960001, ancient, ancient))
        conn.execute(
            "INSERT INTO manager_team (fpl_id, gameweek, captured_at) "
            "VALUES (?, ?, ?)", (960001, 3, ancient))
    again = _db.prune_idle_managers(days=90, now=now)
    expect("an id with a stored gameweek is exempt",
           "prune_idle_managers() with a manager_team row", 0, again["removed"])

    with _db.connect() as conn:
        conn.execute("DELETE FROM manager_team WHERE fpl_id = 960001")
        conn.execute("DELETE FROM known_manager")


def test_autosub_waits_for_the_bench():
    """A bench player whose match hasn't kicked off yet is not skipped over.

    FPL fills a vacated slot in bench order. Treating "hasn't played yet" as
    "didn't play" brings the wrong substitute on mid-afternoon and reverses it
    later, so the pitch shuffles twice and is only right at the end.
    """
    group("auto-subs", "high")

    import autosubs

    def p(pid, pos, team, starting, position, **kw):
        return dict(id=pid, pos=pos, team=team, starting=starting,
                    position=position, **kw)

    squad = [
        p(1, "GK", 10, True, 1), p(2, "DEF", 10, True, 2),
        p(3, "DEF", 10, True, 3), p(4, "DEF", 11, True, 4),
        p(5, "DEF", 11, True, 5), p(6, "MID", 11, True, 6),
        p(7, "MID", 12, True, 7), p(8, "MID", 12, True, 8),
        p(9, "MID", 12, True, 9), p(10, "FWD", 13, True, 10),
        p(11, "FWD", 13, True, 11, is_captain=True),
        p(12, "GK", 13, False, 12),
        p(13, "MID", 14, False, 13),   # club 14 has not kicked off
        p(14, "MID", 15, False, 14),   # club 15 has, and he played
        p(15, "DEF", 15, False, 15),
    ]
    # Starter 7 (club 12) recorded nothing and his club is done. The first
    # bench outfielder (13) belongs to a club still to play.
    minutes = {1: 90, 2: 90, 3: 90, 4: 90, 5: 90, 6: 90, 7: 0, 8: 90,
               9: 90, 10: 90, 11: 90, 12: 90, 13: 0, 14: 90, 15: 90}
    decided = {10, 11, 12, 13, 15}          # club 14 still to play

    mid = autosubs.apply(squad, minutes, decided_teams=decided)
    expect("no substitute is made while the next man up may still play",
           "starter 7 out, bench 13 undecided", [], mid["subs"],
           note="FPL fills the slot in bench order; 14 is not next in line yet")

    # Once club 14's match has finished with him unused, 14 comes on.
    settled = autosubs.apply(squad, minutes,
                             decided_teams=decided | {14})
    expect("once every club is decided the sub is made",
           "starter 7 out, bench 13 played 0, bench 14 played 90",
           [(7, 14)], [(s["out"]["id"], s["in"]["id"]) for s in settled["subs"]])
    check("the effective eleven stays legal", "apply() result",
          "11 players, one keeper, FPL minimums met",
          settled["starters"], lambda xi: autosubs._legal_xi(xi))


def test_accuracy_statistics():
    """The maths behind /accuracy, on inputs whose answers are known by hand.

    Worth testing properly rather than eyeballing, because this is the one page
    that makes a quantitative claim about the model. A ranking statistic that is
    quietly wrong would not look wrong - it would just publish a flattering
    number under a heading about honesty.
    """
    group("accuracy maths", "high")

    import accuracy as acc

    # Ties are the normal case here, not an edge one: most actual scores are 0,
    # 1 or 2, so an ordinal rank would invent an order among dozens of equal
    # values and report a correlation partly built out of it.
    expect("ties share an averaged rank", "_ranks([1, 2, 2, 3])",
           [1.0, 2.5, 2.5, 4.0], acc._ranks([1, 2, 2, 3]))
    expect("an all-tied list ranks flat", "_ranks([5, 5, 5])",
           [2.0, 2.0, 2.0], acc._ranks([5, 5, 5]))

    expect("a perfect linear relationship is 1", "_pearson([1,2,3,4],[2,4,6,8])",
           1.0, acc._pearson([1, 2, 3, 4], [2, 4, 6, 8]))
    expect("a reversed one is -1", "_pearson([1,2,3,4],[4,3,2,1])",
           -1.0, acc._pearson([1, 2, 3, 4], [4, 3, 2, 1]))
    # Undefined rather than zero: a week where everyone scored the same is not a
    # week the model got wrong, it is one with nothing to be right about.
    expect("no spread means undefined, not zero", "_pearson([1,2,3,4],[7,7,7,7])",
           None, acc._pearson([1, 2, 3, 4], [7, 7, 7, 7]))
    expect("a single pair is undefined", "_pearson([1],[1])",
           None, acc._pearson([1], [1]))

    # Hand-computable metrics. predicted - actual = [+1, -1, +2, -2], so the
    # mean absolute error is 1.5 and the bias cancels to 0.
    pairs = [(3.0, 2.0, 1), (2.0, 3.0, 1), (6.0, 4.0, 2), (4.0, 6.0, 2)]
    m = acc._metrics(pairs)
    expect("n counts every settled pair", str(pairs), 4, m["n"])
    expect("mean absolute error", "errors [+1,-1,+2,-2]", 1.5, m["mae"])
    expect("bias cancels when errors are symmetric", "same", 0.0, m["bias"])
    expect("rmse punishes the larger misses", "same",
           round(((1 + 1 + 4 + 4) / 4) ** 0.5, 2), m["rmse"])
    # Actuals are [2,3,4,6], mean 3.75, so the no-model baseline is out by
    # (1.75 + 0.75 + 0.25 + 2.25) / 4 = 1.25 - BETTER than the model's 1.5 here,
    # which must be reported as a negative improvement rather than hidden.
    expect("baseline predicts the mean actual", "actuals [2,3,4,6]", 1.25,
           m["baseline_mae"])
    check("a model worse than the baseline reports a negative improvement",
          "mae 1.5 vs baseline 1.25", "improvement_pct < 0", m["improvement_pct"],
          lambda v: v is not None and v < 0,
          note="the page has to be able to say the model is not earning its keep")

    expect("an empty sample is not sufficient", "_metrics([])",
           {"n": 0, "sufficient": False}, acc._metrics([]))
    check("the sufficiency threshold is the documented one",
          "accuracy.MIN_PREDICTIONS", "a positive int", acc.MIN_PREDICTIONS,
          lambda n: isinstance(n, int) and n > 0)
    check("a small sample is flagged insufficient", "_metrics(4 pairs)",
          "sufficient is False", m["sufficient"], lambda v: v is False)


def test_accuracy_reads_settled_rows_only():
    """Only frozen-and-settled rows count, and an unscored pick is not a zero.

    The backfill leaves `actual_points` NULL for a player FPL never reported,
    deliberately, rather than writing a nought. Counting those as zero would
    invent an error the model never made and drag every figure on the page.
    """
    group("accuracy data", "high")

    import accuracy as acc
    import db as _db

    with _db.connect() as conn:
        conn.execute("DELETE FROM ai_team_snapshot")
        cur = conn.execute(
            """INSERT INTO ai_team_snapshot
                   (gameweek, formation, budget, squad_cost, predicted_points,
                    actual_points, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (31, "3-4-3", 100.0, 99.0, 60.0, 55, "2026-08-01T00:00:00Z"))
        sid = cur.lastrowid
        # No row with a NULL prediction: ai_team_snapshot_picks declares
        # predicted_points NOT NULL, so a pick without a frozen projection
        # cannot exist on this table at all. The nullable half of the pair is
        # the one that matters anyway.
        rows = [
            # (position, predicted, actual)
            (1, 5.0, 6),      # settled
            (2, 4.0, 2),      # settled
            (3, 7.0, None),   # never reported by FPL - must not count as 0
        ]
        for pos, pred, actual in rows:
            conn.execute(
                """INSERT INTO ai_team_snapshot_picks
                       (snapshot_id, element_id, position, cost,
                        predicted_points, actual_points)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, 5000 + pos, pos, 5.0, pred, actual))

    pairs = acc._pairs("best_xi")
    expect("only rows with both numbers are compared",
           "3 picks: 2 settled, 1 never reported by FPL", 2, len(pairs))
    check("the unreported pick is absent, not zeroed", "_pairs('best_xi')",
          "no (7.0, 0.0) pair", pairs,
          lambda ps: not any(p == 7.0 and a == 0.0 for p, a, _ in ps),
          note="a NULL actual means 'not scored', not 'scored nothing'")

    totals = acc.squad_totals()
    expect("the squad total is read back", "squad_totals()",
           [{"gameweek": 31, "predicted": 60.0, "actual": 55,
             "source": "best_xi"}], totals)

    summary = acc.summary()
    check("the summary reports itself available", "summary()",
          "available is True", summary["available"], lambda v: v is True)
    check("per-gameweek covers the settled round", "summary()['per_gameweek']",
          "one row for GW31", summary["per_gameweek"],
          lambda rs: [r["gameweek"] for r in rs] == [31])

    with _db.connect() as conn:
        conn.execute("DELETE FROM ai_team_snapshot")

    # With nothing settled the page must say so rather than print zeroes.
    empty = acc.summary()
    check("an empty season is reported as unavailable", "summary() with no rows",
          "available is False", empty["available"], lambda v: v is False)
    expect("and carries no sources", "summary()['sources']", [], empty["sources"])


def _price_bootstrap(players, total_players=10_000_000):
    """A bootstrap-shaped dict for the price snapshot tests."""
    return {"total_players": total_players,
            "elements": [{"code": c, "now_cost": cost, "transfers_in": ti,
                          "transfers_out": to, "selected_by_percent": own}
                         for c, cost, ti, to, own in players]}


def test_price_snapshot_capture():
    """One reading a night, and a second run on the same night replaces it.

    The replace rule is the one worth pinning down. A price change happens once
    a night, so two captures on one date are two observations of the same event
    - appending both would double-count the transfers between them and inflate
    every momentum figure that crossed the date.
    """
    group("price snapshots", "high")

    import datetime
    import db as _db
    import price_changes as pc

    with _db.connect() as conn:
        conn.execute("DELETE FROM player_price_snapshot")

    boot = _price_bootstrap([(9001, 100, 50_000, 10_000, 10.0)])
    first = pc.capture(bootstrap=boot, day="2026-09-01")
    expect("a capture writes one row per player", "capture(1 player)",
           1, first["written"])

    # Same date, different numbers: must overwrite, not accumulate.
    pc.capture(bootstrap=_price_bootstrap([(9001, 100, 70_000, 12_000, 10.0)]),
               day="2026-09-01")
    with _db.connect() as conn:
        rows = list(conn.execute(
            "SELECT transfers_in, owners FROM player_price_snapshot "
            "WHERE snapshot_date = '2026-09-01' AND code = 9001"))
    expect("a second capture on the same night replaces the first",
           "capture() twice for 2026-09-01", 1, len(rows))
    expect("and it is the later reading that survives",
           "transfers_in after the replace", 70_000, rows[0]["transfers_in"])
    # 10% of 10,000,000 managers.
    expect("the owner count is resolved at capture time",
           "10.0% of 10,000,000", 1_000_000, rows[0]["owners"])

    empty = pc.capture(bootstrap={"elements": []}, day="2026-09-02")
    expect("a bootstrap with no players writes nothing",
           "capture(no elements)", 0, empty["written"])

    dropped = pc.prune(days=0, today=datetime.date(2026, 9, 2))
    check("pruning removes readings past the window", "prune(days=0)",
          "at least one row removed", dropped["removed"], lambda n: n >= 1)

    with _db.connect() as conn:
        conn.execute("DELETE FROM player_price_snapshot")


def test_price_momentum_and_calibration():
    """The two accumulation rules, which are subtly different on purpose.

    `observed_changes` totals the momentum that CAUSED a change, so it includes
    the night the price moved. `board` totals what has built up SINCE the last
    change, so it excludes it. Getting these the same way round would compare a
    player's progress against a threshold measured from a different quantity,
    and the error would be invisible - both numbers would still look plausible.
    """
    group("price momentum", "high")

    import price_changes as pc

    # 10% of 10,000,000 = 1,000,000 owners. +20,000 net a night, so momentum
    # reaches 0.06 after three nights, which is when the price moves.
    riser = []
    ti = to = 0
    cost = 100
    for day in range(1, 9):
        ti += 25_000
        to += 5_000
        riser.append({"date": f"2026-09-{day:02d}", "cost": cost, "in": ti,
                      "out": to, "owned": 10.0, "owners": 1_000_000})
        if day in (3, 6):
            cost += 1

    changes = pc.observed_changes({7001: riser})
    expect("both price changes are found", "8 nights, 2 rises",
           2, len(changes))
    check("each change carries the momentum that caused it",
          "8 nights, rises on the 4th and 7th",
          "both at 0.06 (3 nights x 20,000 over 1,000,000 owners)",
          [round(c["momentum"], 4) for c in changes],
          lambda ms: ms == [0.06, 0.06],
          note="counted from the PREVIOUS change, not from the season start")

    board = pc.board(history={7001: riser})
    row = board["risers"][0]
    # Two nights have passed since the change on the 7th: 40,000 / 1,000,000.
    expect("progress counts only from the last change",
           "2 nights since the rise on 2026-09-07", 0.02, row["momentum"])
    expect("last night's net transfers are reported as a fact",
           "one night", 20_000, row["net_last_night"])

    # A faller: negative momentum against a negative threshold is positive
    # progress, so the two directions rank the same way up.
    faller = []
    ti = to = 0
    cost = 95
    for day in range(1, 6):
        ti += 2_000
        to += 17_000
        faller.append({"date": f"2026-09-{day:02d}", "cost": cost, "in": ti,
                       "out": to, "owned": 5.0, "owners": 500_000})
    board = pc.board(history={7002: faller})
    check("a faller is classed as a fall", "net transfers out every night",
          "one row, code 7002", board["fallers"],
          lambda fs: len(fs) == 1 and fs[0]["code"] == 7002)
    check("and its progress is positive, not negative",
          "negative momentum over a negative threshold", "progress > 0",
          board["fallers"][0]["progress"], lambda p: p > 0)

    # Calibration only replaces the borrowed estimate once there is evidence.
    cal = pc.calibration(changes)
    expect("two observations are not enough to claim a measurement",
           "calibration(2 rises, 0 falls)", False, cal["measured"])
    expect("so the fallback threshold is what gets used", "same",
           pc.FALLBACK_THRESHOLD, cal["rise_threshold"])
    expect("but what WAS measured is still reported", "same",
           0.06, cal["measured_rise"])

    plenty = ([{"direction": "rise", "momentum": 0.05, "code": 1, "date": "d"}] * 20
              + [{"direction": "fall", "momentum": -0.07, "code": 2, "date": "d"}] * 20)
    cal = pc.calibration(plenty)
    expect("enough observations switch it to measured",
           "calibration(20 rises, 20 falls)", True, cal["measured"])
    expect("the rise threshold is the median of the rises", "same",
           0.05, cal["rise_threshold"])
    expect("falls are calibrated separately and keep their sign", "same",
           -0.07, cal["fall_threshold"],
           note="FPL is not symmetric; one averaged figure is wrong both ways")


def test_price_board_excludes_unmeasurable_players():
    """A player whose owner count is unknowable is left off, not guessed at.

    FPL reports ownership to one decimal place. At 0.0% the divisor is a floor
    rather than a fact, and a few dozen transfers over it produces a confident
    progress bar built out of nothing - which is exactly the failure this page
    exists not to commit.
    """
    group("price board", "high")

    import price_changes as pc

    invisible = [
        {"date": "2026-09-01", "cost": 40, "in": 0, "out": 0, "owned": 0.0, "owners": 0},
        {"date": "2026-09-02", "cost": 40, "in": 50, "out": 10, "owned": 0.0, "owners": 0},
    ]
    # 0.1% is a real figure FPL reports, and it is still excluded: rounded to
    # one decimal place it means "somewhere between 0.05% and 0.15%", so the
    # divisor is uncertain by half its own value.
    barely_owned = [
        {"date": "2026-09-01", "cost": 40, "in": 0, "out": 0, "owned": 0.1, "owners": 10_000},
        {"date": "2026-09-02", "cost": 40, "in": 600, "out": 0, "owned": 0.1, "owners": 10_000},
    ]
    properly_owned = [
        {"date": "2026-09-01", "cost": 60, "in": 0, "out": 0, "owned": 5.0, "owners": 500_000},
        {"date": "2026-09-02", "cost": 60, "in": 20_000, "out": 0, "owned": 5.0, "owners": 500_000},
    ]
    one_night = [
        {"date": "2026-09-02", "cost": 50, "in": 100, "out": 10, "owned": 5.0, "owners": 500_000},
    ]

    board = pc.board(history={8001: invisible, 8002: barely_owned,
                              8003: one_night, 8004: properly_owned})
    listed = {r["code"] for r in board["risers"] + board["fallers"]}
    check("a 0.0%-owned player is left off the board", "owned = 0.0%",
          "absent from risers and fallers", 8001 in listed, lambda v: v is False,
          note="the divisor is a floor, not a measurement")
    check("a 0.1%-owned player is left off too", "owned = 0.1%",
          "absent from the board", 8002 in listed, lambda v: v is False,
          note="one decimal place of ownership is a divisor uncertain by half "
               "its own value; the board was otherwise all noise")
    check("a properly owned player is shown", "owned = 5.0%",
          "present on the board", 8004 in listed, lambda v: v is True)
    check("the gate is the documented one", "price_changes.MIN_OWNERSHIP_PCT",
          "1.0", pc.MIN_OWNERSHIP_PCT, lambda v: v == 1.0)
    check("a player with one night recorded is skipped", "1 snapshot",
          "absent from the board", 8003 in listed, lambda v: v is False,
          note="there is no difference to take")

    expect("the owner divisor is floored, never zero", "_owners({'owners': 0})",
           1000.0, pc._owners({"owners": 0}))

    # Two nights is the minimum for the page to say anything at all.
    import db as _db
    with _db.connect() as conn:
        conn.execute("DELETE FROM player_price_snapshot")
    empty = pc.summary()
    check("no snapshots means the page reports itself unavailable",
          "summary() with an empty table", "available is False",
          empty["available"], lambda v: v is False)
    pc.capture(bootstrap=_price_bootstrap([(9101, 100, 10, 1, 5.0)]),
               day="2026-09-01")
    one = pc.summary()
    check("one night is still not enough", "summary() after a single capture",
          "available is False", one["available"], lambda v: v is False,
          note="every difference would be a difference from nothing")
    pc.capture(bootstrap=_price_bootstrap([(9101, 100, 5010, 1001, 5.0)]),
               day="2026-09-02")
    two = pc.summary()
    check("two nights is", "summary() after a second capture",
          "available is True", two["available"], lambda v: v is True)
    with _db.connect() as conn:
        conn.execute("DELETE FROM player_price_snapshot")



def test_watchlist():
    """The shortlist that survives the page.

    Keyed on `code` rather than `element_id`, which is the one thing here worth
    a test of its own: this is the only stored object on the site meant to
    outlive a season, and FPL reassigns `id` every August.
    """
    group("watchlist", "high")

    import db as _db
    import watchlist as wl

    with _db.connect() as conn:
        conn.execute("DELETE FROM watchlist")

    pool = [{"code": 111, "id": 1, "web_name": "Haaland", "team_name": "MCI",
             "pos": "FWD", "cost": 15.5, "predicted": 6.1, "rating": 100,
             "form": 7.5, "owned": 69.6, "status": "a",
             "path": "/player/erling-haaland-111", "team_code": 43,
             "next_gameweeks": []}]

    wl.add(7, 111, "watch the run")
    entries = wl.get(7, pool)
    expect("an added player comes back", "add(7, 111); get(7)", 1, len(entries))
    expect("joined against the pool at read time", "get(7)",
           "Haaland", entries[0]["web_name"],
           note="the row stores a code and nothing else - one source of truth "
                "for who a player is")
    expect("the note is kept", "get(7)", "watch the run", entries[0]["note"])

    # Adding again edits rather than duplicating, which is what makes the
    # button in the pop-up safe to press twice.
    wl.add(7, 111, "still watching")
    entries = wl.get(7, pool)
    expect("adding twice does not duplicate", "add(7, 111) again", 1, len(entries))
    expect("and updates the note instead", "get(7)",
           "still watching", entries[0]["note"])

    # A player who has left the game keeps his row. Dropping it silently would
    # look like the site had lost the entry rather than the player having gone.
    wl.add(7, 999999)
    entries = wl.get(7, pool)
    gone = [e for e in entries if e["code"] == 999999]
    expect("a player no longer in the pool is still listed",
           "get(7) with a code the pool has never heard of", 1, len(gone))
    check("and is flagged unavailable rather than dropped", "get(7)",
          "available is False", gone[0]["available"], lambda v: v is False)

    for bad in ("abc", -1, 0, None):
        err = safe(wl.add, 7, bad)
        check(f"a code of {bad!r} is rejected", f"add(7, {bad!r})",
              "WatchlistError", err,
              lambda v: isinstance(v, str) and "WatchlistError" in v)

    err = safe(wl.add, 7, 222, "x" * (wl.MAX_NOTE_CHARS + 1))
    check("an over-long note is rejected", "add(7, 222, 121 chars)",
          "WatchlistError", err,
          lambda v: isinstance(v, str) and "WatchlistError" in v,
          note="the one free-text field the app STORES rather than relays")

    expect("control characters are flattened, not stored",
           r"_clean_note('a\nb\tc')", "a b c", wl._clean_note("a\nb\tc"))
    expect("an empty note becomes None", "_clean_note('   ')",
           None, wl._clean_note("   "))

    # The cap is what stops an unauthenticated write endpoint being used as
    # storage for something other than football.
    with _db.connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE fpl_id = 8")
    for code in range(2000, 2000 + wl.MAX_ENTRIES):
        wl.add(8, code)
    err = safe(wl.add, 8, 9000)
    check("the size cap is enforced", f"add() past {wl.MAX_ENTRIES} entries",
          "WatchlistError", err,
          lambda v: isinstance(v, str) and "WatchlistError" in v)
    expect("and nothing was written past it", "get(8)",
           wl.MAX_ENTRIES, len(wl.get(8, [])))
    # Editing an existing entry must still work at the cap.
    err = safe(wl.add, 8, 2000, "edited at the cap")
    check("but an existing entry can still be edited at the cap",
          "add(8, 2000, note) when full", "no error", err,
          lambda v: isinstance(v, dict) and v.get("ok") is True)

    expect("removing takes one row", "remove(7, 111)",
           1, wl.remove(7, 111)["removed"])
    check("and it is gone", "get(7)", "111 absent",
          [e["code"] for e in wl.get(7, pool)], lambda cs: 111 not in cs)
    expect("removing something absent is not an error", "remove(7, 111) again",
           0, wl.remove(7, 111)["removed"])

    check("watchlists are per-id", "get(8) is unaffected by 7's removals",
          "still full", len(wl.get(8, [])), lambda n: n == wl.MAX_ENTRIES)

    with _db.connect() as conn:
        conn.execute("DELETE FROM watchlist")


# ---------------------------------------------------------------------------
#  Specification tests
# ---------------------------------------------------------------------------
# The cases below are derived from a RULE or a DEFINITION, and then compared
# against what the code says - rather than from what the code already does.
#
# The distinction matters and it is not academic. The auto-sub tests originally
# here were written by reading the implementation, which meant they agreed with
# it by construction: a wrong rule would have produced a wrong expectation and a
# green run. Rewritten against FPL's published substitution rules, they
# immediately found a formation the game forbids and the code allowed.
#
# Where a statistic has a textbook definition, the oracle is numpy or pandas
# rather than arithmetic done by hand here - if the formula has been
# misunderstood, a hand-derived expectation would agree with the mistake.

def _sq_player(pid, pos, starting, position, **kw):
    return dict(id=pid, pos=pos, team=1, starting=starting, position=position, **kw)


def _squad_442():
    """A legal 4-4-2 with a full bench, in FPL's bench order: GK, then three
    outfielders in the order the manager set."""
    P = _sq_player
    return [
        P(1, "GK", True, 1),
        P(2, "DEF", True, 2), P(3, "DEF", True, 3),
        P(4, "DEF", True, 4), P(5, "DEF", True, 5),
        P(6, "MID", True, 6), P(7, "MID", True, 7),
        P(8, "MID", True, 8), P(9, "MID", True, 9),
        P(10, "FWD", True, 10), P(11, "FWD", True, 11, is_captain=True),
        P(12, "GK", False, 12), P(13, "DEF", False, 13),
        P(14, "MID", False, 14), P(15, "FWD", False, 15),
    ]


def _mins(over=None):
    m = {i: 90 for i in range(1, 16)}
    m.update(over or {})
    return m


def _subs_made(result):
    return [(s["out"]["id"], s["in"]["id"]) for s in result["subs"]]


def test_autosub_rules():
    """FPL's substitution rules, as the game publishes them.

      R1  A starter who does not play is replaced by the first substitute who
          did, taken in the bench order the manager set.
      R2  A goalkeeper can only be replaced by the substitute goalkeeper.
      R3  A substitution is only made if the resulting formation is legal.
      R4  If the captain does not play, the vice-captain is captained instead.
      R5  Bench Boost: all fifteen score, so no substitutions happen.
      R6  Triple Captain triples rather than doubles.
    """
    group("auto-sub rules", "high")

    import autosubs

    # R1
    r = autosubs.apply(_squad_442(), _mins({8: 0}))
    expect("R1: the first legal substitute comes on",
           "4-4-2, MID 8 blanks, bench GK/DEF/MID/FWD",
           [(8, 13)], _subs_made(r),
           note="the bench keeper is skipped because two keepers is not a legal XI")
    r = autosubs.apply(_squad_442(), _mins({8: 0, 13: 0}))
    expect("R1: a substitute who did not play is passed over",
           "bench DEF blanked too", [(8, 14)], _subs_made(r))

    # R2
    r = autosubs.apply(_squad_442(), _mins({1: 0}))
    expect("R2: the bench keeper replaces the keeper",
           "GK blanks", [(1, 12)], _subs_made(r))
    r = autosubs.apply(_squad_442(), _mins({1: 0, 12: 0}))
    expect("R2: no outfielder ever takes the gloves",
           "GK and bench GK both blank", [], _subs_made(r))
    r = autosubs.apply(_squad_442(), _mins({10: 0}))
    expect("R2: the bench keeper does not come on for an outfielder",
           "FWD blanks, keeper is first on the bench", [(10, 13)], _subs_made(r))

    # R3 - a replacement need not share the position of the man replaced. This
    # is the case that was got wrong when these were first written: FPL's rule
    # is about the resulting FORMATION, not about like-for-like.
    r = autosubs.apply(_squad_442(), _mins({10: 0, 11: 0}))
    expect("R3: both forwards are replaced, ending 5-4-1",
           "both FWDs blank, bench has DEF then FWD",
           [(10, 13), (11, 15)], _subs_made(r))

    # R3 - the floor genuinely binds.
    P = _sq_player
    lone_forward = [
        P(1, "GK", True, 1),
        P(2, "DEF", True, 2), P(3, "DEF", True, 3), P(4, "DEF", True, 4), P(5, "DEF", True, 5),
        P(6, "MID", True, 6), P(7, "MID", True, 7), P(8, "MID", True, 8),
        P(9, "MID", True, 9), P(10, "MID", True, 10),
        P(11, "FWD", True, 11, is_captain=True),
        P(12, "GK", False, 12), P(13, "DEF", False, 13),
        P(14, "MID", False, 14), P(15, "FWD", False, 15),
    ]
    r = autosubs.apply(lone_forward, _mins({11: 0, 15: 0}))
    expect("R3: no substitution when it would leave zero forwards",
           "4-5-1, the lone FWD and the bench FWD both blank",
           [], _subs_made(r))

    # R3 - the CEILING binds too. This is the bug these tests found: every FPL
    # minimum can be satisfied by a formation the game still forbids, because
    # one keeper plus ten outfielders leaves room for six midfielders.
    three_five_two = [
        P(1, "GK", True, 1),
        P(2, "DEF", True, 2), P(3, "DEF", True, 3), P(4, "DEF", True, 4),
        P(5, "MID", True, 5), P(6, "MID", True, 6), P(7, "MID", True, 7),
        P(8, "MID", True, 8), P(9, "MID", True, 9),
        P(10, "FWD", True, 10), P(11, "FWD", True, 11, is_captain=True),
        P(12, "GK", False, 12), P(13, "MID", False, 13),
        P(14, "MID", False, 14), P(15, "DEF", False, 15),
    ]
    r = autosubs.apply(three_five_two, _mins({10: 0}))
    expect("R3: a sixth midfielder is refused even though every floor is met",
           "3-5-2, FWD blanks, two MIDs ahead of the DEF on the bench",
           [(10, 15)], _subs_made(r),
           note="3-6-1 satisfies DEF>=3, MID>=2 and FWD>=1 and is still illegal")

    # R3 - exhaustively, against FPL's own bounds.
    bad = []
    for d in range(0, 12):
        for m in range(0, 12 - d):
            f = 10 - d - m
            if f < 0:
                continue
            xi = ([P(0, "GK", True, 0)]
                  + [P(100 + i, "DEF", True, 100 + i) for i in range(d)]
                  + [P(200 + i, "MID", True, 200 + i) for i in range(m)]
                  + [P(300 + i, "FWD", True, 300 + i) for i in range(f)])
            fpl_legal = (3 <= d <= 5) and (2 <= m <= 5) and (1 <= f <= 3)
            if autosubs._legal_xi(xi) != fpl_legal:
                bad.append({"DEF": d, "MID": m, "FWD": f, "fpl": fpl_legal})
    expect("R3: every possible eleven is judged exactly as FPL judges it",
           "all 1-GK formations summing to 11", [], bad, severity="high")

    # R4
    sq = _squad_442()
    sq[9]["is_vice_captain"] = True
    expect("R4: the armband moves to the vice when the captain blanks",
           "captain plays 0", 10, autosubs.apply(sq, _mins({11: 0}))["captain_id"])
    expect("R4: and stays put when he plays",
           "captain plays 90", 11, autosubs.apply(sq, _mins())["captain_id"])
    r = autosubs.apply(sq, _mins({10: 0, 11: 0}))
    doubled = [pid for pid, mult in autosubs.multipliers(sq, r).items() if mult >= 2]
    expect("R4: nobody is doubled when captain and vice both blank",
           "both play 0", [], doubled)

    # R5
    r = autosubs.apply(_squad_442(), _mins({8: 0, 10: 0}), chip="bboost")
    expect("R5: a bench boost makes no substitutions",
           "two starters blank under bboost", [], _subs_made(r))
    mult = autosubs.multipliers(_squad_442(), r, chip="bboost")
    expect("R5: and all fifteen count", "bboost multipliers", 15,
           sum(1 for v in mult.values() if v >= 1))
    r = autosubs.apply(sq, _mins({11: 0}), chip="bboost")
    expect("R5: but the armband still moves under a bench boost",
           "captain blanks under bboost", 10, r["captain_id"])

    # R6
    r = autosubs.apply(_squad_442(), _mins())
    expect("R6: the triple captain is on x3", "chip='3xc'", 3,
           autosubs.multipliers(_squad_442(), r, chip="3xc")[11])
    points = {i: 5 for i in range(1, 16)}
    expect("R6: and the total reflects it", "everyone on 5 points", 65,
           autosubs.score(_squad_442(), points, r, chip="3xc"),
           note="ten starters on 5, plus the captain's 5 tripled")

    # Scoring rules that are not about substitutions.
    missing = {i: 5 for i in range(1, 16)}
    del missing[7]
    expect("a player FPL never reported contributes nothing rather than a nought",
           "player 7 absent from the points feed",
           55, autosubs.score(_squad_442(), missing, r),
           note="counting him as 0 would invent a blank he never had")
    r = autosubs.apply(_squad_442(), _mins({8: 0}))
    scored = {i: 5 for i in range(1, 16)}
    scored[8] = 0
    expect("only the effective eleven counts", "MID 8 blanks, DEF 13 comes on",
           60, autosubs.score(_squad_442(), scored, r),
           note="the three unused substitutes must not score")


def test_accuracy_against_numpy():
    """The statistics, checked against their textbook definitions.

    numpy and pandas are the oracle rather than arithmetic written out here.
    Both are already dependencies, and pandas' `.rank()` implements the
    average-rank tie handling Spearman needs independently of this project's
    own `_ranks`, which is the part most likely to be subtly wrong.
    """
    group("accuracy vs definitions", "high")

    import random as _random
    import numpy as _np
    import pandas as _pd

    import accuracy as acc

    _random.seed(20)
    worst = {"mae": 0.0, "rmse": 0.0, "bias": 0.0, "base": 0.0, "rho": 0.0}
    for _ in range(5):
        n = _random.randint(30, 300)
        pred = [round(_random.uniform(0, 14), 2) for _ in range(n)]
        act = [max(0, round(_random.gauss(p, 3))) for p in pred]
        m = acc._metrics([(p, a, 1) for p, a in zip(pred, act)])

        P = _np.array(pred, dtype=float)
        A = _np.array(act, dtype=float)
        # Spearman from first principles: Pearson over average ranks.
        rho = float(_np.corrcoef(_pd.Series(P).rank(), _pd.Series(A).rank())[0, 1])

        worst["mae"] = max(worst["mae"], abs(float(_np.mean(_np.abs(P - A))) - m["mae"]))
        worst["rmse"] = max(worst["rmse"], abs(float(_np.sqrt(_np.mean((P - A) ** 2))) - m["rmse"]))
        worst["bias"] = max(worst["bias"], abs(float(_np.mean(P - A)) - m["bias"]))
        worst["base"] = max(worst["base"], abs(float(_np.mean(_np.abs(A.mean() - A))) - m["baseline_mae"]))
        worst["rho"] = max(worst["rho"], abs(rho - m["spearman"]))

    # Rounding to 2dp (3 for rho) is the only difference allowed.
    check("mean absolute error matches numpy", "5 random samples",
          "within rounding", round(worst["mae"], 4), lambda v: v <= 0.005)
    check("root mean squared error matches numpy", "5 random samples",
          "within rounding", round(worst["rmse"], 4), lambda v: v <= 0.005)
    check("bias matches numpy", "5 random samples",
          "within rounding", round(worst["bias"], 4), lambda v: v <= 0.005)
    check("the no-model baseline is the mean-actual MAE", "5 random samples",
          "within rounding", round(worst["base"], 4), lambda v: v <= 0.005)
    check("rank correlation matches Pearson-over-pandas-ranks",
          "5 random samples, heavy ties at the low scores",
          "within rounding", round(worst["rho"], 5), lambda v: v <= 0.0005,
          note="tie handling is the part most likely to be quietly wrong")

    # Anchors that follow from the definitions rather than from any sample.
    perfect = acc._metrics([(float(v), float(v), 1) for v in range(1, 40)])
    expect("a perfect model has zero error", "predicted == actual for 39 rows",
           0.0, perfect["mae"])
    expect("a perfect model has zero bias", "predicted == actual for 39 rows",
           0.0, perfect["bias"])
    expect("a perfect model beats the baseline by 100%",
           "predicted == actual for 39 rows", 100.0, perfect["improvement_pct"])
    expect("a perfect model ranks perfectly", "predicted == actual for 39 rows",
           1.0, perfect["spearman"])

    actuals = [3, 5, 1, 9, 4, 4, 7, 2]
    mean_actual = sum(actuals) / len(actuals)
    same_as_baseline = acc._metrics([(mean_actual, float(a), 1) for a in actuals])
    expect("predicting the mean for everybody gains exactly nothing",
           "every prediction = mean(actuals)",
           0.0, same_as_baseline["improvement_pct"])

    inverted = acc._metrics([(float(9 - a), float(a), 1) for a in range(1, 9)])
    check("a model worse than the baseline reports a negative gain",
          "predictions anti-correlated with outcomes", "improvement < 0",
          inverted["improvement_pct"], lambda v: v is not None and v < 0,
          note="the page has to be able to say the model is not earning its keep")
    expect("and a perfectly inverted ranking is -1",
           "predictions anti-correlated with outcomes",
           -1.0, inverted["spearman"])


def test_price_change_invariants():
    """Properties the price board must have whatever the implementation does.

    Invariants rather than worked examples on purpose: a worked example can be
    satisfied by accident by the same misunderstanding that produced it, and
    these are the claims the page actually makes to a reader.
    """
    group("price invariants", "high")

    import random as _random
    import price_changes as _pc

    def hist(entries):
        return [{"date": "2026-09-%02d" % d, "cost": c, "in": i, "out": o,
                 "owned": 5.0, "owners": w} for d, c, i, o, w in entries]

    # Momentum is net transfers OVER owners, so the same ratio at two very
    # different club sizes has to produce the same number. If it did not, the
    # board would simply rank by popularity.
    small = hist([(1, 100, 0, 0, 100_000), (2, 100, 5_000, 0, 100_000)])
    big = hist([(1, 100, 0, 0, 1_000_000), (2, 100, 50_000, 0, 1_000_000)])
    expect("momentum is a ratio, not a count",
           "5k net over 100k owners vs 50k over 1m",
           _pc.board(history={1: small})["risers"][0]["momentum"],
           _pc.board(history={2: big})["risers"][0]["momentum"])

    flat = hist([(1, 100, 0, 0, 500_000), (2, 100, 9_000, 9_000, 500_000)])
    board = _pc.board(history={3: flat})
    expect("equal transfers in and out cancel to nothing",
           "9,000 in and 9,000 out", 0.0,
           (board["risers"] + board["fallers"])[0]["momentum"])

    # Direction and progress, over a few hundred random movements.
    _random.seed(4)
    misfiled, negative = [], []
    for _ in range(200):
        h = hist([(1, 100, 0, 0, 400_000),
                  (2, 100, _random.randint(0, 40_000), _random.randint(0, 40_000), 400_000)])
        b = _pc.board(history={9: h})
        misfiled += [r["momentum"] for r in b["risers"] if r["momentum"] < 0]
        misfiled += [r["momentum"] for r in b["fallers"] if r["momentum"] >= 0]
        negative += [r["progress"] for r in b["risers"] + b["fallers"] if r["progress"] < 0]
    expect("nobody is filed under the wrong direction",
           "200 random transfer movements", [], misfiled)
    expect("progress is never negative, so both boards rank the same way up",
           "200 random transfer movements", [], negative)

    # Every price transition in a history must be found, and no others.
    _random.seed(6)
    mismatches = []
    for _ in range(50):
        costs, cur = [], 100
        for _d in range(11):
            if _random.random() < 0.3:
                cur += _random.choice([-1, 1])
            costs.append(cur)
        h = hist([(d + 1, costs[d], 1000 * (d + 1), 0, 300_000)
                  for d in range(len(costs))])
        transitions = sum(1 for a, b in zip(costs, costs[1:]) if a != b)
        found = len(_pc.observed_changes({7: h}))
        if found != transitions:
            mismatches.append((transitions, found))
    expect("one observed change per price transition, no more and no fewer",
           "50 random 11-night price histories", [], mismatches)

    rise = _pc.observed_changes({1: hist([(1, 100, 0, 0, 300_000),
                                          (2, 101, 50_000, 0, 300_000)])})[0]
    fall = _pc.observed_changes({2: hist([(1, 100, 0, 0, 300_000),
                                          (2, 99, 0, 50_000, 300_000)])})[0]
    expect("a price going up is recorded as a rise", "cost 100 -> 101",
           "rise", rise["direction"])
    expect("a price going down is recorded as a fall", "cost 100 -> 99",
           "fall", fall["direction"])
    check("a rise carries positive momentum", "cost 100 -> 101",
          "momentum > 0", rise["momentum"], lambda v: v > 0)
    check("a fall carries negative momentum", "cost 100 -> 99",
          "momentum < 0", fall["momentum"], lambda v: v < 0)

    # FPL resets its own counter when a price moves, so the board has to as
    # well. Without this a player who rose on Tuesday reads on Wednesday as
    # though he still had the whole week's buying behind him.
    after_change = hist([(1, 100, 0, 0, 100_000), (2, 100, 20_000, 0, 100_000),
                         (3, 100, 40_000, 0, 100_000), (4, 101, 60_000, 0, 100_000),
                         (5, 101, 61_000, 0, 100_000)])
    row = _pc.board(history={11: after_change})["risers"][0]
    expect("momentum counts from the last price change, not the first reading",
           "three heavy nights, a rise, then one quiet night",
           0.01, row["momentum"],
           note="one quiet night of 1,000 over 100,000 owners")

    # The calibrated threshold is a median of observations, and the two
    # directions are calibrated separately because FPL is not symmetric.
    obs = ([{"direction": "rise", "momentum": v, "code": 1, "date": "d"}
            for v in (0.02, 0.04, 0.06, 0.08, 0.10)] * 3
           + [{"direction": "fall", "momentum": -0.05, "code": 2, "date": "d"}] * 15)
    cal = _pc.calibration(obs)
    expect("the rise threshold is the median observed rise",
           "rises at .02 .04 .06 .08 .10", 0.06,
           round(cal["rise_threshold"], 6))
    expect("the fall threshold keeps its sign", "15 falls at -0.05", -0.05,
           round(cal["fall_threshold"], 6))
    check("and it only claims to be measured once there is enough of it",
          "15 rises, 15 falls", "measured", cal["measured"], lambda v: v is True)


def test_the_tests_are_wired_correctly():
    """A test that checks the tests, because two failure modes here are silent.

    **Arity.** `expect` takes (name, input, expected, actual) and `check` takes
    (name, input, expected, actual, predicate). Call either with one argument
    too few and Python raises at CALL time, which the runner catches and reports
    as "suite crashed" - so a single miscounted call takes out every remaining
    case in that suite and the run still prints a large number of passes. That
    has now happened twice while writing these, which is twice more than it
    should take to automate.

    **Registration.** A test function that is never added to SUITES does not
    fail - it does not run at all, and nothing anywhere says so. That is the
    worse of the two, because the suite gets quietly smaller while appearing to
    grow.
    """
    group("test wiring", "high")

    import ast
    import pathlib

    here = pathlib.Path(__file__).resolve().parent
    arity = {"expect": 4, "check": 5}

    underfed, unregistered = [], []
    for path in sorted(here.glob("suite_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in arity
                    and len(node.args) < arity[node.func.id]):
                underfed.append(f"{path.name}:{node.lineno} {node.func.id}() "
                                f"got {len(node.args)}, needs {arity[node.func.id]}")

        defined = {n.name for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
        listed = set()
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "SUITES" for t in node.targets)
                    and isinstance(node.value, ast.List)):
                listed = {e.id for e in node.value.elts if isinstance(e, ast.Name)}
        unregistered += [f"{path.name}:{n}" for n in sorted(defined - listed)]

    expect("every expect()/check() call is given enough arguments",
           "ast scan of tests/suite_*.py", [], underfed, severity="high",
           note="one short call aborts its whole suite and still reports passes")
    expect("every test function is registered in its SUITES list",
           "ast scan of tests/suite_*.py", [], unregistered, severity="high",
           note="an unregistered test does not fail, it simply never runs")


def test_watchlist_survives_id_reassignment():
    """The claim the watchlist's whole design rests on, tested directly.

    FPL reassigns `element_id` every August. A watchlist is the only thing this
    app stores that is meant to outlive a season, so it is keyed on `code`
    instead - and this simulates the summer to prove the difference is real
    rather than a comment.
    """
    group("watchlist keys", "high")

    import db as _db
    import watchlist as wl

    with _db.connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE fpl_id = 4711")

    # Two players, this season.
    before = [
        {"code": 500001, "id": 12, "web_name": "Tracked", "team_name": "ARS",
         "pos": "MID", "cost": 7.0, "path": "/player/tracked-500001"},
        {"code": 500002, "id": 13, "web_name": "Other", "team_name": "CHE",
         "pos": "DEF", "cost": 5.0, "path": "/player/other-500002"},
    ]
    wl.add(4711, 500001)
    expect("the tracked player is found this season",
           "watchlist keyed on code 500001", "Tracked",
           wl.get(4711, before)[0]["web_name"])

    # August: FPL hands out new element ids and they land on DIFFERENT players.
    # The codes are untouched, because a code belongs to a footballer.
    after = [
        {"code": 500002, "id": 12, "web_name": "Other", "team_name": "CHE",
         "pos": "DEF", "cost": 5.2, "path": "/player/other-500002"},
        {"code": 500001, "id": 13, "web_name": "Tracked", "team_name": "ARS",
         "pos": "MID", "cost": 7.3, "path": "/player/tracked-500001"},
    ]
    entry = wl.get(4711, after)[0]
    expect("and is still the same footballer after the ids are reshuffled",
           "element_id 12 now belongs to a different player",
           "Tracked", entry["web_name"], severity="high",
           note="keyed on element_id this would now read Other - a different "
                "player, silently")
    expect("picking up his new price with him",
           "same code, new season", 7.3, entry["cost"])

    with _db.connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE fpl_id = 4711")

SUITES = [test_slugify, test_article, test_fmt_and_plural, test_fixture_label,
          test_describe_zero_minutes,
          test_a_to_z_grouping, test_draft_validation, test_storage_kind,
          test_horizon_points, test_gw_report_predicted_for,
          test_gw_report_availability, test_gw_report_sections,
          test_gw_report_captains, test_gw_report_build,
          test_social_x_limits, test_social_fit_and_facts, test_social_hook,
          test_social_drafts_render, test_social_roundup_drafts,
          test_roundup_players, test_roundup_table_and_shocks,
          test_roundup_momentum, test_roundup_build,
          test_spotlight_injury_return, test_spotlight_underlying,
          test_spotlight_minutes_and_fixtures, test_spotlight_choice_and_ledger,
          test_spotlight_post, test_spotlight_drafts,
          test_clubelo_cache_freshness, test_dispersion_calibration,
          test_retention, test_ops_heartbeat, test_ops_backups,
          test_notification_channels, test_channel_messages,
          test_kofi_message, test_kofi_route_absent_when_unconfigured,
          test_declared_dependencies,
          test_model_leakage_and_scale, test_team_strength_ranks_are_scale_free,
          test_model_bundle_version_guard, test_two_stage_predictions,
          test_kit_colours,
          test_chip_halves, test_chip_schedule_rules,
          test_chip_schedule_fallback, test_chip_priors,
          test_chip_gain_shapes, test_bench_build_weight,
          test_season_fixture_structure,
          test_free_transfer_estimate, test_free_transfer_step,
          test_live_overlay,
          test_upcoming_fixture_horizon, test_rotation_difficulty_spread,
          test_flat_difficulty_renders_neutral, test_fixture_runs_needs_a_spread,
          test_form_blend_weight, test_form_blend,
          test_gameweek_stats_schemas,
          test_manager_points_backfill, test_performance_gap_season,
          test_settled_lineup_flags, test_draft_applies_to_one_gameweek,
          test_rescore_queue,
          test_prev_season_totals,
          test_events_cache, test_team_map_retries_after_failure,
          test_known_manager_bounds, test_autosub_waits_for_the_bench,
          test_accuracy_statistics, test_accuracy_reads_settled_rows_only,
          test_price_snapshot_capture, test_price_momentum_and_calibration,
          test_price_board_excludes_unmeasurable_players, test_watchlist,
          test_autosub_rules, test_accuracy_against_numpy,
          test_price_change_invariants, test_watchlist_survives_id_reassignment,
          test_the_tests_are_wired_correctly]
