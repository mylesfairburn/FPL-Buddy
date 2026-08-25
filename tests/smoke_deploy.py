"""Post-deploy smoke test: point it at a running site and see what is broken.

Different job from the other suites here. They import the app and exercise pure
functions; this one talks HTTP to a deployment and asserts on what a browser
would actually receive. That distinction matters, because every failure this was
written for was invisible to the unit tests:

  * the ratings crashing on startup, which leaves every page serving a 200 with
    an empty table inside it
  * the fixture horizon including a round already played
  * the rotation grid rendering flat because its strength data was never
    substituted
  * a roundup that was never written
  * the analytics beacon, which needs a token, a script tag and two CSP
    directives to line up before a single hit is counted

Usage:

    python tests/smoke_deploy.py                        # https://fpl.mfhost.co.uk
    python tests/smoke_deploy.py http://localhost:8020  # a local run

Exits non-zero if anything failed, so it can gate a deploy.
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ROWS, SEVERITY, check, expect, group, summary

DEFAULT_URL = "https://fpl.mfhost.co.uk"
TIMEOUT = 30

# Cloudflare's own origins. The beacon needs BOTH: the script is fetched from
# static., and it posts its hits to the bare host. A policy naming only the
# first loads the script and then silently drops every beacon it sends.
BEACON_SRC = "https://static.cloudflareinsights.com"
BEACON_POST = "https://cloudflareinsights.com"


def header(headers, name):
    """Case-insensitive header lookup.

    Not fussiness: HTTP/2 lowercases every field name, so a site behind
    Cloudflare returns `content-security-policy` while a direct uvicorn run
    returns `Content-Security-Policy`. A case-sensitive get() finds the policy
    locally and reports it missing in production, which is precisely backwards.
    """
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    return lowered.get(name.lower(), "")


# Transport failures get one retry. The pages here are 60-180KB and the suite
# fires them back to back, which is enough to draw an occasional connection
# reset out of a local uvicorn. A smoke test that cries wolf gets ignored, and
# then it is worth nothing on the day it is right.
RETRIES = 2
RETRY_PAUSE = 1.0

# Why a failed fetch returns body=None rather than the error text: an error
# string is still a str, so it sails through `isinstance(body, str)` and gets
# scanned for markup - which finds none, and reports a healthy page as an empty
# one. That mistake cost a debugging session, so the failure is now
# unmistakable to every caller.
FAILURES = {}


def fetch(base, path, as_json=False):
    """(status, body, headers), with body None if nothing was received.

    Never raises: a smoke test that dies on the first unreachable endpoint
    tells you less than one that finishes and shows the whole picture.
    """
    url = base.rstrip("/") + path
    # Compression is asked for because a browser asks for it, so this exercises
    # the path real traffic takes. It also sidesteps a Windows-only failure:
    # against a local uvicorn, urllib takes a connection reset on the largest
    # uncompressed responses (/api/all_players at ~500KB, /players/a-z at
    # ~175KB) while curl and the live site are fine. Compressed, both arrive.
    req = urllib.request.Request(url, headers={
        "User-Agent": "fpl-buddy-smoke/1",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    })
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", "replace")
                return r.status, (json.loads(body) if as_json else body), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, None, dict(e.headers or {})
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt + 1 < RETRIES:
                time.sleep(RETRY_PAUSE)
    FAILURES[path] = last
    return None, None, {}


def body_of(base, path):
    """Fetch a page and assert it arrived, so a transport failure is reported
    as one rather than as a page missing its content."""
    status, html, headers = fetch(base, path)
    ok = status == 200 and isinstance(html, str)
    if not ok:
        check(f"{path} was retrieved", f"GET {path}", "200 with a body",
              FAILURES.get(path) or f"status={status}", lambda _: False)
    return (html if ok else None), headers


# ---------------------------------------------------------------------------


def test_ratings_loaded(base):
    """The one that matters most: did the pool actually load.

    Every page answers 200 whether or not it did - the app is deliberately
    built to keep serving prose and the gameweek archive when the projections
    are unavailable - so uptime checks and status codes both read green while
    the site shows "Ratings not loaded yet". `degraded` is the field that
    doesn't lie.
    """
    group("ratings loaded", "critical")

    status, data, _ = fetch(base, "/api/ai/status", as_json=True)
    expect("/api/ai/status answers", "GET /api/ai/status", 200, status)
    if not isinstance(data, dict):
        return None

    ratings = data.get("ratings") or {}
    check("the initial data load did not fail", "ratings.degraded", "null",
          ratings.get("degraded"), lambda d: d is None,
          note="non-null here is the 'Ratings not loaded yet' state")

    status, players, _ = fetch(base, "/api/all_players", as_json=True)
    rows = (players or {}).get("players") if isinstance(players, dict) else None
    check("the player pool is populated", "/api/all_players", "hundreds of players",
          len(rows) if rows is not None else players, lambda n: isinstance(n, int) and n > 300)

    if rows:
        unrated = [p for p in rows if p.get("rating") is None]
        check("every player carries a rating", "players[].rating", "no nulls",
              len(unrated), lambda n: n == 0)

    return data


def test_form_blend(base, status_data):
    """Current-season results are in the numbers, not waiting for gameweek 3.

    `form_weight` is the figure to read. `form_basis` is three buckets cut out
    of it and stays "prior-weighted-blend" until the third round, which is
    correct but says nothing about whether GW1 counted.
    """
    group("form blend", "high")

    ratings = (status_data or {}).get("ratings") or {}
    played = ratings.get("current_gameweeks_on_disk")
    weight = ratings.get("form_weight")

    check("the current season's stats are on disk",
          "ratings.current_gameweeks_on_disk", ">= 1", played,
          lambda n: isinstance(n, int) and n >= 1,
          note="0 means refresh-stats has not run; the blend has nothing to blend")

    if isinstance(played, int) and played >= 1:
        check("a played gameweek is carrying weight", "ratings.form_weight",
              "> 0", weight, lambda w: isinstance(w, (int, float)) and w > 0,
              note="0 here is the old switch: results ignored until GW3")

        # 1 -> 0.25, 2 -> 0.40, 3 -> 0.50, mirroring current_form_weight().
        expected = (2.0 * played) / (2.0 * played + 6.0)
        check(f"the weight matches the ramp for {played} gameweek(s)",
              f"played={played}", f"~{expected:.2f}", weight,
              lambda w: isinstance(w, (int, float)) and abs(w - expected) < 0.01)


def test_fixture_horizon(base, status_data):
    """No projection may name a gameweek that has already been played.

    FPL leaves `finished` false until `data_checked` flips, so a round stays
    "upcoming" for a day or more after its last whistle. Anchoring on
    next_gameweek is what fixed it; this asserts the anchor held.
    """
    group("fixture horizon", "high")

    next_gw = (status_data or {}).get("next_gameweek")
    check("the season clock reports a next gameweek", "/api/ai/status",
          "an integer", next_gw, lambda g: isinstance(g, int) and g > 0)
    if not isinstance(next_gw, int):
        return

    _status, players, _ = fetch(base, "/api/all_players", as_json=True)
    rows = (players or {}).get("players") if isinstance(players, dict) else []
    firsts = [p["next_gameweeks"][0]["event"] for p in (rows or [])
              if p.get("next_gameweeks")]
    check("every player's projection starts at the next gameweek",
          "players[].next_gameweeks[0].event", f">= {next_gw}",
          sorted(set(firsts))[:5], lambda e: bool(e) and min(e) >= next_gw,
          note="a lower value is a round already played, priced as upcoming")

    _status, rot, _ = fetch(base, "/api/rotation?category=defender", as_json=True)
    events = (rot or {}).get("gameweeks") if isinstance(rot, dict) else None
    if events:
        check("the rotation grid starts at the next gameweek too",
              "/api/rotation gameweeks", f"first >= {next_gw}", events[:3],
              lambda e: min(e) >= next_gw)


def test_fixture_difficulty(base):
    """The rotator must discriminate between fixtures.

    All-zero strength data made every difficulty identical, the colour scale
    collapsed, and the grid rendered green from end to end - which reads as
    "every fixture is a banker" rather than "no data". A spread is the thing to
    assert; the colours follow from it.
    """
    group("fixture difficulty", "high")

    status, rot, _ = fetch(base, "/api/rotation?category=defender", as_json=True)
    expect("/api/rotation answers", "GET /api/rotation", 200, status)
    teams = (rot or {}).get("teams") if isinstance(rot, dict) else None
    if not teams:
        check("the rotation payload has teams", "/api/rotation", "20 teams",
              rot, lambda _: False)
        return

    values = [f["difficulty"] for t in teams
              for f in (t.get("fixtures") or {}).values()
              if f.get("difficulty") is not None]
    check("difficulties are not all identical", "every fixture cell",
          "a real spread", f"min={min(values):.1f} max={max(values):.1f}"
          if values else "none", lambda _: bool(values) and min(values) != max(values),
          note="min == max is the all-green grid")

    averages = [t.get("average") for t in teams if t.get("average") is not None]
    check("per-team averages are not all zero", "teams[].average", "varied",
          sorted(set(averages))[:4],
          lambda a: len(set(averages)) > 1 and any(v != 0 for v in averages))

    # The server-rendered mirror, which is what a crawler and a no-JS reader
    # get. It carries the colours the script would otherwise compute.
    html, _headers = body_of(base, "/fixture-rotator")
    if html is not None:
        cells = re.findall(r"background-color:(hsl\([^)]*\))", html)
        hues = {c.split("(")[1].split(",")[0].strip() for c in cells}
        check("the rendered grid is not one flat colour", "/fixture-rotator",
              "several hues", sorted(hues)[:6],
              lambda h: len(h) > 1 if cells else False,
              note="one hue across every cell means the scale collapsed")


def test_gameweek_roundup(base, status_data):
    """A settled round should have a roundup, and the hub should link it."""
    group("gameweek roundup", "medium")

    current = (status_data or {}).get("current_gameweek")
    html, _headers = body_of(base, "/gameweek")
    if html is not None:
        check("the hub shows a roundup card", "/gameweek", "a roundup link",
              bool(re.search(r"/gameweek/\d+/roundup", html)), lambda v: v,
              note="absent means no roundup has ever been written")

    # Whichever rounds are plausibly settled. Not asserted for the round in
    # play: FPL confirms stats at no fixed hour, and the roundup deliberately
    # waits for data_checked rather than guessing.
    gw = None
    if isinstance(current, int) and current >= 2:
        gw = current - 1
        status, _body, _ = fetch(base, f"/gameweek/{gw}/roundup")
        expect(f"GW{gw}'s roundup is published", f"GET /gameweek/{gw}/roundup",
               200, status)

    # A defender's clean sheet is usually the largest single part of his score,
    # and the cards used to mention it only in the prose - so a 15-point
    # defender listed "Goals 1, Bonus 1" and left the other four points
    # unexplained.
    target = gw if gw else 1
    page, _headers = body_of(base, f"/gameweek/{target}/roundup")
    if page and "gw-card" in page:
        cards = re.findall(r'<article class="gw-card">(.*?)</article>', page, re.S)
        backs = [c for c in cards
                 if re.search(r">(GK|DEF)<", c) and "clean sheet" in c.lower()]
        if backs:
            listed = [c for c in backs if "<dt>Clean sheet</dt>" in c]
            check("a defender's clean sheet is listed, not just narrated",
                  f"/gameweek/{target}/roundup cards", "a Clean sheet stat",
                  f"{len(listed)}/{len(backs)} cards", lambda _v, l=listed,
                  b=backs: len(l) == len(b))


def test_analytics_beacon(base):
    """The Cloudflare beacon, end to end.

    Four things have to agree before a single page view is counted, and each
    fails silently on its own:

      1. FPL_CF_ANALYTICS_TOKEN is set in the container, and is 32 hex
         characters - main.py drops anything else with a warning
      2. the script tag renders (it is behind `{% if cf_analytics_token %}`)
      3. CSP script-src allows static.cloudflareinsights.com
      4. CSP connect-src allows cloudflareinsights.com, or the script loads and
         then cannot post anything it collects

    Checking only for the tag would have passed a page whose CSP blocked it.
    """
    group("analytics beacon", "medium")

    html, headers = body_of(base, "/")
    check("the home page answers", "GET /", "200 with a body",
          "yes" if html is not None else "no", lambda v: v == "yes")
    if html is None:
        return

    has_tag = BEACON_SRC + "/beacon.min.js" in html
    check("the beacon script tag is rendered", "GET / (HTML)",
          "a cloudflareinsights script tag", has_tag, lambda v: v,
          note="absent means FPL_CF_ANALYTICS_TOKEN is unset or was rejected "
               "as malformed - check `docker exec fpl-buddy printenv "
               "FPL_CF_ANALYTICS_TOKEN`")

    token = re.search(r'data-cf-beacon=\'\{"token":\s*"([0-9a-f]{32})"\}\'', html)
    check("it carries a well-formed site token", "data-cf-beacon",
          "32 hex characters", token.group(1) if token else None,
          lambda t: bool(t))

    csp = header(headers, "Content-Security-Policy")
    script_src = next((d for d in csp.split(";") if d.strip().startswith("script-src")), "")
    connect_src = next((d for d in csp.split(";") if d.strip().startswith("connect-src")), "")

    check("CSP script-src allows the beacon's origin", "Content-Security-Policy",
          f"contains {BEACON_SRC}", script_src.strip() or "(no script-src)",
          lambda d: BEACON_SRC in d,
          note="without this the browser refuses to load the script at all")
    check("CSP connect-src allows it to report", "Content-Security-Policy",
          f"contains {BEACON_POST}", connect_src.strip() or "(no connect-src)",
          lambda d: BEACON_POST in d,
          note="without this it loads and then silently sends nothing")

    check("the tag and the policy agree", "beacon tag vs CSP",
          "both present or both absent", (has_tag, BEACON_SRC in script_src),
          lambda p: p[0] == p[1],
          note="one without the other counts nothing and is the easy state to "
               "deploy into by accident")


def test_pages_render(base):
    """Every page a reader can reach, and whether it came back with content.

    Byte length rather than status: an empty pool serves a 200 with the shell
    and no table, which is exactly the failure that prompted this file.
    """
    group("pages render", "high")

    for path, floor in (("/", 8000), ("/players", 60000), ("/ai-teams", 60000),
                        ("/my-team", 60000), ("/fixture-rotator", 60000),
                        ("/players/a-z", 20000), ("/injuries", 8000),
                        ("/gameweek", 6000)):
        status, html, _ = fetch(base, path)
        size = len(html) if isinstance(html, str) else 0
        # The predicate takes the recorded value positionally; the loop
        # variables are bound as defaults so each row keeps its own.
        check(f"{path} renders with content", f"GET {path}",
              f"200 and > {floor:,} bytes", f"{status}, {size:,} bytes",
              lambda _actual, _s=status, _z=size, _f=floor: _s == 200 and _z > _f)

    # The mobile track-record fix ships as a class and a row cap; both are
    # served as static assets, so their presence is checkable from here even
    # though the layout itself is not.
    html, _headers = body_of(base, "/ai-teams")
    if html is not None:
        check("track-record containers carry their own sizing class",
              "/ai-teams markup", "ps-list-history present",
              "ps-list-history" in html, lambda v: v)
    appjs, _headers = body_of(base, "/static/app.js")
    if appjs is not None:
        check("the history row cap shipped", "/static/app.js",
              "TRACK_RECORD_ROWS present", "TRACK_RECORD_ROWS" in appjs,
              lambda v: v)


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Smoke-testing {base}\n")

    status_data = test_ratings_loaded(base)
    test_form_blend(base, status_data)
    test_fixture_horizon(base, status_data)
    test_fixture_difficulty(base)
    test_gameweek_roundup(base, status_data)
    test_analytics_beacon(base)
    test_pages_render(base)

    width = max(len(r["name"]) for r in ROWS) + 2
    last = None
    for r in ROWS:
        if r["group"] != last:
            print(f"\n{r['group'].upper()}")
            last = r["group"]
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['name']:<{width}} {r['actual']}")
        if not r["ok"] and r["note"]:
            print(f"         -> {r['note']}")

    s = summary()
    print(f"\n{s['passed']} passed, {s['failed']} failed, {s['total']} total")
    for sev in SEVERITY:
        if s["failures_by_severity"].get(sev):
            print(f"  {sev}: {s['failures_by_severity'][sev]} failed")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
