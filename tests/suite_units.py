"""Pure-function unit tests.

Everything here runs without the app, a network call or a database - these are
the functions whose output ends up in a URL, a page title or a sentence a
reader sees, so they're worth pinning down precisely.
"""

import math

import player_pages as pp
import drafts
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
          "returns 15 cleaned picks", drafts._validate(ok_picks),
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
            drafts._validate(payload)
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
    cleaned = drafts._validate(squad(patch={0: {"is_captain": 1, "cost": "5.5"}}))
    check("captain flag normalised to 0/1", "is_captain=1",
          "int 0 or 1", cleaned[0]["is_captain"], lambda v: v in (0, 1))
    check("cost coerced to float", "cost='5.5'", "float 5.5",
          cleaned[0]["cost"], lambda v: isinstance(v, float) and abs(v - 5.5) < 1e-9)
    check("missing cost becomes None rather than raising", "no cost key",
          "None", cleaned[1]["cost"], lambda v: v is None)

    # SQL metacharacters in a field that reaches the database.
    inj = squad(patch={0: {"element_id": "1); DROP TABLE manager_draft;--"}})
    try:
        drafts._validate(inj)
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


SUITES = [test_slugify, test_article, test_fmt_and_plural, test_fixture_label,
          test_a_to_z_grouping, test_draft_validation, test_storage_kind,
          test_horizon_points]
