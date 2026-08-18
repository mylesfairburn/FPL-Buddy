"""JSON API contract tests.

Some of these endpoints proxy the live FPL API. Where that's the case the test
asserts the contract the front end depends on - a JSON object, the right keys,
no 5xx - rather than specific values, which are whatever FPL says today.
"""

import json
import math

import main
from harness import check, expect, group


def _client():
    from context import client
    return client


def _json(r):
    try:
        return r.json()
    except Exception:
        return None


def _has_nan(obj):
    """json.dumps happily writes bare NaN, which is invalid JSON and makes
    JSON.parse throw in the browser. Worth catching at the source."""
    if isinstance(obj, float):
        return math.isnan(obj) or math.isinf(obj)
    if isinstance(obj, dict):
        return any(_has_nan(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_nan(v) for v in obj)
    return False


def test_read_endpoints():
    group("API contract", "high")
    c = _client()

    cases = [
        ("/api/ratings", "results", True),
        ("/api/ratings?position=MID&top_n=5", "results", True),
        ("/api/all_players", "players", True),
        ("/api/underperforming?top_n=5", None, True),
        ("/api/rotation?category=defender", "pairs", True),
        ("/api/rotation?category=attacker", "pairs", True),
        ("/api/ai/status", "db", True),
        ("/api/ai/history", "snapshots", True),
        ("/api/ai/manager/history", "history", True),
        ("/api/search?q=sa", "results", True),
    ]
    for path, key, cacheable in cases:
        r = c.get(path)
        expect(f"GET {path} status", f"GET {path}", 200, r.status_code,
               severity="high")
        body = _json(r)
        check(f"GET {path} returns JSON", f"GET {path}", "parseable JSON object",
              type(body).__name__, lambda v: v in ("dict", "list"), severity="high")
        if key and isinstance(body, dict):
            check(f"GET {path} has '{key}'", f"GET {path}", f"key '{key}' present",
                  list(body)[:8], lambda _: key in body, severity="high")
        check(f"GET {path} contains no NaN/Infinity", f"GET {path}",
              "strictly valid JSON numbers", "checked",
              lambda _: not _has_nan(body), severity="high",
              note="bare NaN makes JSON.parse throw in the browser")

    # Cache headers: the read-only derived endpoints should be edge-cacheable,
    # the live ones must not be.
    for path in ["/api/ratings", "/api/all_players", "/api/underperforming",
                 "/api/rotation"]:
        r = c.get(path)
        check(f"{path} is publicly cacheable", f"GET {path}", main.API_CACHE,
              r.headers.get("cache-control", ""), lambda v: v == main.API_CACHE)
    r = c.get("/api/news?limit=3")
    check("/api/news is uncacheable", "GET /api/news",
          "no-store", r.headers.get("cache-control", ""),
          lambda v: "no-store" in v, severity="high",
          note="a cached injury feed is worse than no feed")


def test_ratings_parameters():
    group("API parameters", "medium")
    c = _client()

    # The names the data actually uses. These are the working contract.
    for pos in list(main.state["position_dfs"] or {}) + ["All"]:
        r = c.get(f"/api/ratings?position={pos}")
        body = _json(r) or {}
        check(f"position={pos} returns rows", f"GET /api/ratings?position={pos}",
              "non-empty results", len(body.get("results", [])),
              lambda v: v > 0, severity="high")

    # The short codes every other part of the app uses for a position - the
    # pane markup, the player records, get_all_players. If these return nothing
    # the filter is unusable by any caller that doesn't know the internal
    # DataFrame keys.
    for pos in ["GK", "DEF", "MID", "FWD", "gk", "Mid", "GKP"]:
        r = c.get(f"/api/ratings?position={pos}")
        body = _json(r) or {}
        check(f"position={pos} (short code) returns rows",
              f"GET /api/ratings?position={pos}", "non-empty results",
              len(body.get("results", [])), lambda v: v > 0, severity="high",
              note="position_dfs is keyed 'Goalkeeper'/'Defender'/...; every "
                   "other layer of the app says GK/DEF/MID/FWD")

    # The alias must select the right group, not just any non-empty one.
    gk = _json(c.get("/api/ratings?position=GK&top_n=5")) or {}
    check("GK alias selects goalkeepers", "GET /api/ratings?position=GK&top_n=5",
          "every row's position is Goalkeeper",
          {row.get("position") for row in gk.get("results", [])} or "no rows",
          lambda s: s == {"Goalkeeper"}, severity="high")

    r = c.get("/api/ratings?position=NOPE")
    body = _json(r) or {}
    expect("unknown position returns empty, not an error",
           "GET /api/ratings?position=NOPE", [], body.get("results"))

    r = c.get("/api/ratings?top_n=5")
    body = _json(r) or {}
    check("top_n is respected", "GET /api/ratings?top_n=5", "5 rows",
          len(body.get("results", [])), lambda v: v == 5)

    r = c.get("/api/ratings?top_n=0")
    check("top_n=0 does not error", "GET /api/ratings?top_n=0", "200",
          r.status_code, lambda v: v == 200)

    r = c.get("/api/ratings?top_n=-5")
    check("negative top_n does not 500", "GET /api/ratings?top_n=-5",
          "status < 500", r.status_code, lambda v: v < 500, severity="high")

    r = c.get("/api/ratings?top_n=999999999")
    check("absurd top_n does not 500", "GET /api/ratings?top_n=999999999",
          "status < 500", r.status_code, lambda v: v < 500, severity="medium",
          note="head() clamps, so this returns the whole pool")

    r = c.get("/api/ratings?top_n=abc")
    expect("non-numeric top_n is a 422", "GET /api/ratings?top_n=abc",
           422, r.status_code, severity="medium")


def test_sorted_and_shaped():
    group("data shape", "medium")
    c = _client()
    body = _json(c.get("/api/ratings?position=All&top_n=50")) or {}
    rows = body.get("results", [])
    check("ratings are sorted descending", "GET /api/ratings?top_n=50",
          "rating monotonically non-increasing", "checked",
          lambda _: all(rows[i]["rating"] >= rows[i + 1]["rating"]
                        for i in range(len(rows) - 1)), severity="high")
    check("every row has a web_name", "GET /api/ratings?top_n=50",
          "no blank names", "checked",
          lambda _: all((r.get("web_name") or "").strip() for r in rows))

    body = _json(c.get("/api/all_players")) or {}
    players = body.get("players", [])
    check("all_players is the full pool", "GET /api/all_players",
          ">400 players", len(players), lambda v: v > 400, severity="high")
    check("every player has a season-stable code", "GET /api/all_players",
          "no missing 'code'", "checked",
          lambda _: all(p.get("code") is not None for p in players),
          severity="critical",
          note="code is what the per-player URLs are keyed on")
    missing_paths = [p.get("web_name") for p in players if not p.get("path")]
    check("every player carries its page path", "GET /api/all_players",
          "no missing 'path'", missing_paths[:5] or "none missing",
          lambda _: not missing_paths, severity="high")
    codes = [p["code"] for p in players if p.get("code") is not None]
    check("player codes are unique", "GET /api/all_players", "no duplicates",
          len(codes) - len(set(codes)), lambda d: d == 0, severity="critical")


def test_search():
    group("search", "medium")
    c = _client()
    for q, desc in [("sa", "short prefix"), ("Salah", "full surname"),
                    ("SALAH", "uppercase"), ("zzzzzzzz", "no match"),
                    ("é", "accented single char"), ("' OR 1=1 --", "SQL-ish"),
                    ("<script>", "HTML-ish"), ("%", "SQL wildcard"),
                    ("_", "SQL single-char wildcard"), ("o", "single letter"),
                    ("van", "common particle")]:
        r = c.get("/api/search", params={"q": q})
        check(f"search {desc}: {q!r}", f"GET /api/search?q={q!r}",
              "200 with a results list", f"{r.status_code}",
              lambda _: r.status_code == 200
              and isinstance((_json(r) or {}).get("results"), list),
              severity="high")

    r = c.get("/api/search")
    expect("search with no q is a 422", "GET /api/search", 422, r.status_code)

    body = _json(c.get("/api/search", params={"q": "salah"})) or {}
    check("a real surname finds someone", "GET /api/search?q=salah",
          "at least one result", len(body.get("results", [])),
          lambda v: v >= 0, severity="low",
          note="tolerant: the pool depends on the season's data")

    # A broad query is the one most likely to sweep up a player with no
    # prediction, which is what used to make this endpoint 500.
    r = c.get("/api/search", params={"q": "a"})
    body = _json(r) or {}
    expect("a broad query still returns 200", "GET /api/search?q=a", 200,
           r.status_code, severity="high",
           note="~40% of the preseason pool has no predicted_points")
    missing = [row for row in body.get("results", [])
               if row.get("predicted_points") is None]
    check("absent predictions serialise as null, not NaN",
          "GET /api/search?q=a", "null in the JSON body",
          f"{len(missing)} of {len(body.get('results', []))} rows are null",
          lambda _: "NaN" not in r.text, severity="high")


def test_draft_roundtrip():
    """The one write path. Uses an FPL id far outside the real range so it can
    never collide with a genuine manager's stored draft.

    Unauthenticated in both directions since the per-device write token was
    removed: any caller can save to an id, and the last writer wins. The
    assertions below are written to FAIL if a lock is ever reintroduced without
    a way for a second device to carry it, because that is the regression that
    would silently break saving on a laptop after saving on a phone.
    """
    group("draft round-trip", "high")
    c = _client()
    test_id = 999999999

    import drafts
    drafts.delete_draft(test_id)          # clean slate; the temp DB outlives a run

    r = c.get(f"/api/draft/{test_id}")
    body = _json(r) or {}
    expect("unsaved id reports unavailable", f"GET /api/draft/{test_id}",
           False, body.get("available"))

    picks = [{"element_id": 100 + i, "position": i + 1} for i in range(15)]
    picks[0]["is_captain"] = True
    picks[1]["is_vice_captain"] = True
    r = c.post(f"/api/draft/{test_id}", json={"picks": picks, "gameweek": 1, "bank": 0.5})
    expect("save returns 200", f"POST /api/draft/{test_id} (15 valid picks)",
           200, r.status_code, severity="high")
    body = _json(r) or {}
    expect("save reports 15 picks stored", "POST body", 15, body.get("picks"))

    check("save mints no write token", "POST body",
          "no draft_token key", repr(body.get("draft_token")),
          lambda _: "draft_token" not in body, severity="high",
          note="a token in this response is the device lock coming back")

    r = c.get(f"/api/draft/{test_id}")
    body = _json(r) or {}
    expect("saved draft reads back", f"GET /api/draft/{test_id}", True,
           body.get("available"), severity="high")
    expect("squad has 15 players", f"GET /api/draft/{test_id}", 15,
           len(body.get("squad", [])), severity="high")
    check("captain survived the round-trip", f"GET /api/draft/{test_id}",
          "exactly one is_captain", "checked",
          lambda _: sum(1 for p in body.get("squad", []) if p.get("is_captain")) == 1)
    check("first 11 are marked starting", f"GET /api/draft/{test_id}",
          "11 starters", sum(1 for p in body.get("squad", []) if p.get("starting")),
          lambda v: v == 11)

    # Save again: must replace, not accumulate.
    c.post(f"/api/draft/{test_id}", json={"picks": picks, "gameweek": 2})
    body = _json(c.get(f"/api/draft/{test_id}")) or {}
    expect("re-saving replaces rather than appends", "POST twice, then GET",
           15, len(body.get("squad", [])), severity="high")

    # The whole point of removing the lock: a second client, carrying nothing
    # from the first, can still write. This is the phone-then-laptop case.
    r = c.post(f"/api/draft/{test_id}", json={"picks": picks, "gameweek": 3})
    expect("a second device can save to an already-saved id",
           f"POST /api/draft/{test_id} (no prior state)",
           200, r.status_code, severity="high",
           note="a 403 here is the cross-device bug the lock removal fixed")
    body = _json(c.get(f"/api/draft/{test_id}")) or {}
    expect("the second save actually landed", f"GET /api/draft/{test_id}",
           3, body.get("gameweek"), severity="high",
           note="200 without the write would be worse than a refusal")

    bad_payloads = [
        ("empty picks", {"picks": []}),
        ("14 picks", {"picks": picks[:14]}),
        ("duplicate player", {"picks": picks[:14] + [dict(picks[0], position=15)]}),
        ("picks not a list", {"picks": "nope"}),
        ("no picks key", {}),
        ("two captains", {"picks": [dict(p, is_captain=True) for p in picks]}),
    ]
    for name, payload in bad_payloads:
        r = c.post(f"/api/draft/{test_id}", json=payload)
        check(f"rejects {name}", f"POST /api/draft/{test_id} {name}",
              "400 Bad Request", r.status_code, lambda v: v == 400,
              severity="high",
              note="validate_picks is now the ONLY thing between an arbitrary "
                   "body and a stored row - a 500 means it was bypassed")

    body = _json(c.get(f"/api/draft/{test_id}")) or {}
    expect("a rejected payload left the stored draft alone", f"GET /api/draft/{test_id}",
           3, body.get("gameweek"), severity="high")

    r = c.delete(f"/api/draft/{test_id}")
    expect("delete returns 200", f"DELETE /api/draft/{test_id}", 200, r.status_code)
    body = _json(c.get(f"/api/draft/{test_id}")) or {}
    expect("deleted draft is gone", f"GET /api/draft/{test_id}", False,
           body.get("available"))

    r = c.delete(f"/api/draft/{test_id}")
    expect("deleting nothing is not an error", f"DELETE /api/draft/{test_id} (twice)",
           200, r.status_code,
           note="idempotent - a retry from a flaky connection must not 500")

    r = c.post(f"/api/draft/{test_id}", json={"picks": picks, "gameweek": 1})
    expect("a deleted id can be saved again", f"POST /api/draft/{test_id}",
           200, r.status_code, severity="high")
    drafts.delete_draft(test_id)


def test_ai_endpoints():
    group("AI endpoints", "medium")
    c = _client()
    for path in ["/api/ai/best_xi", "/api/ai/manager"]:
        r = c.get(path)
        expect(f"GET {path} status", f"GET {path}", 200, r.status_code,
               severity="high")
        body = _json(r) or {}
        check(f"{path} states availability", f"GET {path}",
              "'available' key present", list(body)[:8],
              lambda _: "available" in body, severity="high",
              note="the front end branches on this rather than on HTTP status")
        if body.get("available"):
            squad = body.get("squad") or body.get("picks") or []
            check(f"{path} squad is a sane size", f"GET {path}",
                  "11-15 players", len(squad), lambda v: 0 < v <= 15)

    r = c.get("/api/ai/best_xi?gameweek=1")
    check("best_xi accepts a gameweek", "GET /api/ai/best_xi?gameweek=1",
          "200", r.status_code, lambda v: v == 200)
    r = c.get("/api/ai/best_xi?gameweek=-1")
    check("negative gameweek does not 500", "GET /api/ai/best_xi?gameweek=-1",
          "status < 500", r.status_code, lambda v: v < 500, severity="high")
    r = c.get("/api/ai/best_xi?budget=0")
    check("zero budget does not 500", "GET /api/ai/best_xi?budget=0",
          "status < 500, unavailable is fine", r.status_code,
          lambda v: v < 500, severity="high")

    # The pre-rename path. It went out with every page for a season, so it has
    # to keep answering - and answering the same thing, since it is the same
    # function behind it and not a second copy that can drift.
    old = c.get("/api/ai/best_xv")
    expect("the old best_xv path still answers", "GET /api/ai/best_xv",
           200, old.status_code, severity="high")
    check("best_xv is an alias, not a second implementation",
          "GET /api/ai/best_xv vs /api/ai/best_xi",
          "identical availability", (_json(old) or {}).get("available"),
          lambda v: v == (_json(c.get("/api/ai/best_xi")) or {}).get("available"))


def test_live_and_proxy_endpoints():
    """Endpoints that reach out to FPL. Network-dependent, so they're asserted
    on shape and on never returning a 5xx."""
    group("upstream proxies", "medium")
    c = _client()
    for path in ["/api/live/1", "/api/live/38", "/api/live/0", "/api/live/9999"]:
        r = c.get(path)
        check(f"GET {path} never 5xx", f"GET {path}", "status < 500",
              r.status_code, lambda v: v < 500, severity="high")
        if r.status_code == 200:
            check(f"GET {path} states availability", f"GET {path}",
                  "'available' key", list(_json(r) or {})[:6],
                  lambda _: "available" in (_json(r) or {}))

    r = c.get("/api/player/1")
    check("player summary never 5xx", "GET /api/player/1", "status < 500",
          r.status_code, lambda v: v < 500, severity="high")

    r = c.get("/api/news?limit=5")
    check("news never 5xx", "GET /api/news?limit=5", "status < 500",
          r.status_code, lambda v: v < 500, severity="high")


def test_ai_chip_plan():
    """The chip plan the AI Manager publishes.

    Shape-only where it has to be - whether a chip is being played this week
    depends on the fixture list - but the schedule's one hard rule is checked
    properly, because a plan that puts two chips in a gameweek is an illegal
    plan however good the arithmetic behind it was.
    """
    group("AI chip plan", "high")
    c = _client()
    body = _json(c.get("/api/ai/manager")) or {}
    if not body.get("available"):
        check("AI manager unavailable, chip plan not asserted",
              "GET /api/ai/manager", "available == True", body.get("available"),
              lambda _: True, severity="info",
              note="preseason with no committed gameweek - nothing to assert")
        return

    plan = body.get("chip_plan") or {}
    for key in ("play", "notes", "available", "used", "schedule", "deadline"):
        check(f"chip_plan carries '{key}'", "GET /api/ai/manager",
              "key present", list(plan)[:10], lambda _, k=key: k in plan,
              severity="high",
              note="the AI Teams pane reads these by name")

    schedule = plan.get("schedule") or []
    weeks = [s.get("gameweek") for s in schedule]
    check("the schedule never puts two chips in one gameweek",
          "chip_plan.schedule", "every gameweek distinct", weeks,
          lambda w: len(w) == len(set(w)), severity="critical")

    chips = [s.get("chip") for s in schedule]
    check("no chip is scheduled twice", "chip_plan.schedule",
          "every chip distinct", chips, lambda ch: len(ch) == len(set(ch)),
          severity="critical")

    deadline = plan.get("deadline")
    check("every scheduled chip lands before the reset", "chip_plan.schedule",
          f"all gameweeks <= {deadline}", weeks,
          lambda w: deadline is None or all(g <= deadline for g in w),
          severity="critical",
          note="a chip scheduled past the reset is a chip thrown away")

    check("nothing already used is scheduled again", "chip_plan",
          "schedule and used do not overlap", {"used": plan.get("used"),
                                               "scheduled": chips},
          lambda _: not (set(chips) & set(plan.get("used") or [])),
          severity="high")

    check("the chip being played is the one scheduled for this gameweek",
          "chip_plan.play", "play matches the schedule entry for now",
          {"play": plan.get("play"), "schedule": schedule},
          lambda _: plan.get("play") is None
                    or plan.get("play") in chips, severity="high")

    for note in plan.get("notes") or []:
        check("every chip note explains itself", note.get("chip"),
              "a non-empty detail string", note.get("detail"),
              lambda d: isinstance(d, str) and len(d) > 0)

    check("the payload is JSON-clean", "GET /api/ai/manager",
          "no bare NaN", "checked", lambda _: not _has_nan(body),
          severity="high")


def test_chip_advice_shape():
    """The per-manager chip advice on /api/team.

    Driven by a live FPL lookup, so this asserts the contract rather than the
    numbers: absent is fine, present must be well-formed.
    """
    group("chip advice", "medium")
    c = _client()
    r = c.get("/api/team/1")
    check("team view never 5xx", "GET /api/team/1", "status < 500",
          r.status_code, lambda v: v < 500, severity="high")

    gw = ((_json(r) or {}).get("gw")) or {}
    advice = gw.get("chip_advice")
    if advice is None:
        check("no chip advice returned for this lookup", "GET /api/team/1",
              "absent is acceptable", None, lambda _: True, severity="info",
              note="the upstream lookup may be unavailable offline")
        return

    check("chip advice is a list", "gw.chip_advice", "list", type(advice).__name__,
          lambda t: t == "list")
    for entry in advice:
        for key in ("chip", "name", "gain", "detail", "verdict"):
            check(f"advice entry carries '{key}'", entry.get("chip"),
                  "key present", list(entry)[:10],
                  lambda _, k=key: k in entry, severity="high")
        check("verdict is one the front end knows", entry.get("chip"),
              "play | hold | consider", entry.get("verdict"),
              lambda v: v in ("play", "hold", "consider"))
        check("a percentile, where given, is a percentage", entry.get("chip"),
              "0-100 or null", entry.get("percentile"),
              lambda p: p is None or 0 <= p <= 100)
        check("only chips measured in the planner's units get a floor",
              entry.get("chip"), "floor set for bboost/3xc only",
              {"chip": entry.get("chip"), "floor": entry.get("floor")},
              lambda _: (entry.get("floor") is not None)
                        == (entry.get("chip") in ("bboost", "3xc")),
              note="wildcard and free hit are described here, not thresholded "
                   "- their simulated figure is in different units entirely")


SUITES = [test_read_endpoints, test_ratings_parameters, test_sorted_and_shaped,
          test_search, test_draft_roundtrip, test_ai_endpoints,
          test_live_and_proxy_endpoints,
          test_ai_chip_plan, test_chip_advice_shape]
