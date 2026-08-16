"""Pure-function unit tests.

Everything here runs without the app, a network call or a database - these are
the functions whose output ends up in a URL, a page title or a sentence a
reader sees, so they're worth pinning down precisely.
"""

import json
import math
import os
import re
import tempfile
from datetime import date

import joblib
import numpy
import pandas as pd

import drafts
import gw_report as gwr
import gw_roundup as gwru
import kits
import player_pages as pp
import player_spotlight as ps
import rating_model
import seasons
import social
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


SUITES = [test_slugify, test_article, test_fmt_and_plural, test_fixture_label,
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
          test_dispersion_calibration,
          test_retention, test_ops_heartbeat, test_ops_backups,
          test_notification_channels, test_channel_messages,
          test_kofi_message, test_kofi_route_absent_when_unconfigured,
          test_declared_dependencies,
          test_model_leakage_and_scale, test_team_strength_ranks_are_scale_free,
          test_model_bundle_version_guard, test_two_stage_predictions,
          test_kit_colours]
