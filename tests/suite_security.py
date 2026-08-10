"""Security tests.

Two kinds of case here:

  * Attacks. Injection, traversal, XSS, oversized bodies, type confusion. These
    should all fail to do anything, and a pass means "the attack was repelled".
  * Posture. Headers, auth on state-changing routes, information disclosure.
    A failure is a hardening gap rather than a live exploit, and is graded by
    what it would actually cost.

Nothing here attacks a third party: every request goes to the local test client.
"""

import json
import re

import main
from harness import check, expect, group


def _client():
    from context import client
    return client


# Payloads reused across every parameter that reaches a query, a path or a page.
SQLI = [
    "1 OR 1=1",
    "1; DROP TABLE manager_draft;--",
    "' OR '1'='1",
    "1' UNION SELECT name FROM sqlite_master--",
    "1/**/OR/**/1=1",
    "admin'--",
]
XSS = [
    "<script>alert(1)</script>",
    '"><script>alert(1)</script>',
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    "'\"><svg/onload=alert(1)>",
    "%3Cscript%3Ealert(1)%3C/script%3E",
]
TRAVERSAL = [
    "../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "....//....//etc/passwd",
    "/etc/passwd",
    "..\\..\\windows\\win.ini",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
]


def test_sql_injection():
    group("SQL injection", "critical")
    c = _client()

    # Path parameters typed as int: FastAPI should reject before any handler.
    for payload in SQLI:
        for route in [f"/api/draft/{payload}", f"/api/league/{payload}",
                      f"/api/player/{payload}", f"/api/manager/{payload}/history"]:
            r = c.get(route)
            # A payload containing a slash doesn't match the route pattern at
            # all, so 404 is the correct answer there rather than 422. Either
            # way the handler never runs, which is the point.
            ok = (404, 422) if "/" in payload else (422,)
            check(f"int path param rejects {payload!r}", f"GET {route}",
                  f"{' or '.join(map(str, ok))} — handler never runs",
                  r.status_code, lambda v: v in ok, severity="critical")

    # The same strings through a POST body, where the value reaches drafts.py.
    for payload in SQLI:
        picks = [{"element_id": 100 + i, "position": i + 1} for i in range(15)]
        picks[0]["element_id"] = payload
        r = c.post("/api/draft/999999998", json={"picks": picks})
        check(f"draft body rejects element_id={payload!r}",
              f"POST /api/draft/999999998 element_id={payload!r}",
              "400 Bad Request", r.status_code, lambda v: v == 400,
              severity="critical")

    # And through the query string, into a pandas filter.
    for payload in SQLI:
        r = c.get("/api/search", params={"q": payload})
        check(f"search survives {payload!r}", f"GET /api/search?q={payload!r}",
              "200, no error", r.status_code, lambda v: v == 200,
              severity="high")

    # The database still exists and still has its tables after all that.
    body = c.get("/api/ai/status").json()
    tables = list((body.get("db") or {}).get("counts") or {})
    check("schema intact after injection attempts", "GET /api/ai/status",
          "manager_draft still present", tables,
          lambda t: "manager_draft" in t, severity="critical",
          note="the check that the DROP attempts genuinely did nothing")


def test_regex_injection():
    """The search endpoint passes its argument to pandas .str.contains(), which
    treats it as a regular expression by default. That makes the pattern
    user-controlled, which is both a crash and a CPU-exhaustion vector."""
    group("regex injection", "high")
    c = _client()

    invalid = [
        ("**", "multiple repeat"),
        ("(", "unbalanced group"),
        ("[", "unterminated character class"),
        ("a{99999999}", "absurd repeat count"),
        ("(?P<n>a)(?P<n>b)", "duplicate group name"),
        ("\\", "trailing backslash"),
    ]
    for pattern, why in invalid:
        r = c.get("/api/search", params={"q": pattern})
        check(f"invalid regex {pattern!r} ({why})",
              f"GET /api/search?q={pattern!r}",
              "200 with no matches — the input is data, not a pattern",
              r.status_code, lambda v: v == 200, severity="high",
              note="a 500 here means the query string is compiled as a regex")

    # Catastrophic backtracking. Kept small so the suite can't hang itself:
    # if the input is treated as data this is instant either way.
    redos = [("(a+)+$", "nested quantifier"), ("(.*a){20}", "repeated group"),
             ("(x+x+)+y", "classic evil regex")]
    for pattern, why in redos:
        started = __import__("time").time()
        r = c.get("/api/search", params={"q": pattern})
        elapsed = __import__("time").time() - started
        check(f"ReDoS pattern {pattern!r} ({why}) is not evaluated",
              f"GET /api/search?q={pattern!r}",
              "returns in under 1s", f"{elapsed:.2f}s, status {r.status_code}",
              lambda _: elapsed < 1.0 and r.status_code == 200, severity="high",
              note="567 names x 3 columns per request, and the endpoint is "
                   "unauthenticated and uncached")

    # A pattern that matches everything proves the input is being compiled.
    r = c.get("/api/search", params={"q": ".*"})
    body = r.json() if r.status_code == 200 else {}
    check("'.*' does not match the entire pool", "GET /api/search?q=.*",
          "200, and 0 results — '.*' is not a substring of any name",
          f"status {r.status_code}, {len(body.get('results', []))} results",
          lambda _: r.status_code == 200 and not body.get("results"),
          severity="high",
          note="if this returns hundreds of players the argument is being "
               "used as a pattern rather than as literal text")


def test_xss():
    group("XSS", "critical")
    c = _client()

    for payload in XSS:
        r = c.get("/api/search", params={"q": payload})
        body = r.text
        check(f"search does not reflect {payload[:30]!r} unescaped",
              f"GET /api/search?q={payload!r}",
              "no raw <script> or onerror= in the response", r.status_code,
              lambda _: "<script>" not in body and "onerror=" not in body,
              severity="critical")

        r = c.get(f"/player/{payload}")
        check(f"player route does not reflect {payload[:30]!r}",
              f"GET /player/{payload!r}",
              "404/307, and no raw payload echoed", r.status_code,
              lambda v, b=r.text: v in (404, 307, 400)
              and "<script>alert" not in b, severity="critical")

    # Jinja autoescaping is what stands between page data and stored XSS. Prove
    # it is on, rather than assuming the default.
    from fastapi.templating import Jinja2Templates
    check("Jinja autoescape is enabled", "main.templates.env.autoescape",
          "True", main.templates.env.autoescape, lambda v: v is True,
          severity="critical")

    # A hostile string rendered through the real template path.
    rendered = main.templates.env.from_string(
        "{{ v }}").render(v="<script>alert(1)</script>")
    expect("template escapes a script tag",
           'render("{{ v }}", v="<script>alert(1)</script>")',
           "&lt;script&gt;alert(1)&lt;/script&gt;", rendered, severity="critical")

    # obf_mail builds markup by hand, so it is the one place autoescape is
    # bypassed - check it escapes its own inputs.
    out = str(main.obf_mail('"><script>alert(1)</script>@x.com'))
    check("obf_mail escapes a hostile address",
          'obf_mail(\'"><script>alert(1)</script>@x.com\')',
          "no raw <script>", out, lambda v: "<script>" not in v,
          severity="critical",
          note="the only handwritten Markup() in the app")


def test_path_traversal():
    group("path traversal", "critical")
    c = _client()
    for payload in TRAVERSAL:
        r = c.get(f"/static/{payload}")
        check(f"static refuses {payload!r}", f"GET /static/{payload}",
              "404/400/403, never 200", r.status_code,
              lambda v: v in (400, 403, 404), severity="critical")
        body = r.text if r.status_code == 200 else ""
        check(f"no file contents leak for {payload!r}", f"GET /static/{payload}",
              "no /etc/passwd or win.ini content", "checked",
              lambda _: "root:" not in body and "[fonts]" not in body,
              severity="critical")

        r = c.get(f"/player/{payload}")
        check(f"player route refuses {payload!r}", f"GET /player/{payload}",
              "404/307, never 200", r.status_code,
              lambda v: v in (307, 404, 400), severity="high")


def test_auth_on_state_changing_routes():
    group("authentication", "high")
    c = _client()

    # Both of these run the full pipeline - about a minute of CPU each - so an
    # open version is a free denial of service. The gate is only active when
    # FPL_REFRESH_TOKEN is set, which is right for local dev but means the test
    # has to set it to prove the gate works at all.
    #
    # Deliberately never sent with a VALID token: that would reload the
    # pipeline mid-run and change what every later test sees.
    original = main.REFRESH_TOKEN
    main.REFRESH_TOKEN = "test-token-not-a-real-secret"
    try:
        for route in ["/api/refresh", "/api/mode?mode=preseason"]:
            r = c.post(route)
            expect(f"{route.split('?')[0]} rejects a missing token",
                   f"POST {route} (no header, token configured)",
                   403, r.status_code, severity="critical")
            r = c.post(route, headers={"x-refresh-token": "wrong"})
            expect(f"{route.split('?')[0]} rejects a wrong token",
                   f"POST {route} x-refresh-token: wrong", 403, r.status_code,
                   severity="critical")
            r = c.post(route, headers={"x-refresh-token": ""})
            expect(f"{route.split('?')[0]} rejects an empty token",
                   f"POST {route} x-refresh-token: ''", 403, r.status_code,
                   severity="critical")
    finally:
        main.REFRESH_TOKEN = original

    check("FPL_REFRESH_TOKEN is configured in this environment",
          "os.environ['FPL_REFRESH_TOKEN']",
          "set in production; unset locally is expected",
          "set" if original else "unset", lambda v: v == "set",
          severity="info",
          note="not a code defect - a deployment requirement. Unset, both "
               "/api/refresh and /api/mode are anonymously callable and each "
               "costs a full pipeline run")

    r = c.post("/api/mode?mode=nonsense",
               headers={"x-refresh-token": "wrong"} if main.REFRESH_TOKEN else {})
    body = r.json() if r.status_code == 200 else {}
    check("/api/mode validates its argument", "POST /api/mode?mode=nonsense",
          "rejected", body.get("status", r.status_code),
          lambda v: v in ("error", 401, 403, 404, 422), severity="medium")


def test_security_headers():
    group("security headers", "medium")
    c = _client()
    r = c.get("/")
    headers = {k.lower(): v for k, v in r.headers.items()}

    wanted = [
        ("x-content-type-options", "medium",
         "stops a browser MIME-sniffing a response into something executable"),
        ("x-frame-options", "medium",
         "otherwise nothing stops the site being framed and clickjacked"),
        ("referrer-policy", "low",
         "otherwise full URLs leak to Ko-fi and the Bootstrap CDN"),
    ]
    for name, sev, why in wanted:
        check(f"{name} is set", "GET / response headers", f"{name} present",
              headers.get(name, "absent"), lambda v: v != "absent",
              severity=sev, note=why)

    # HSTS is scheme-conditional: sending it over plain HTTP would pin a local
    # dev browser to https://localhost.
    r_https = c.get("https://testserver/")
    check("strict-transport-security is set over HTTPS",
          "GET https://testserver/", "max-age present",
          {k.lower(): v for k, v in r_https.headers.items()}.get(
              "strict-transport-security", "absent"),
          lambda v: "max-age" in v, severity="medium")
    check("strict-transport-security is NOT set over HTTP",
          "GET http://testserver/", "absent — would break local dev",
          headers.get("strict-transport-security", "absent"),
          lambda v: v == "absent", severity="low")

    check("content-security-policy is set", "GET / response headers",
          "content-security-policy present",
          headers.get("content-security-policy", "absent"),
          lambda v: v != "absent", severity="medium",
          note="KNOWN GAP, deliberately deferred: the templates carry inline "
               "<script> blocks, so a real policy needs nonces first. No known "
               "injection path today — autoescape is on and no template uses "
               "|safe — so this is defence in depth, not an open hole")

    check("no server banner leaking a version", "GET / response headers",
          "no version in Server", headers.get("server", "absent"),
          lambda v: not re.search(r"\d+\.\d+", v), severity="low")


def test_information_disclosure():
    group("information disclosure", "high")
    c = _client()

    # Endpoints that return str(e) to the caller. Confirm none of them is
    # currently emitting a filesystem path or a stack frame.
    leaky = []
    for path in ["/api/draft/999999997", "/api/ai/manager/history",
                 "/api/ai/history", "/api/manager/999999997/history",
                 "/api/ai/status"]:
        text = c.get(path).text
        if re.search(r"[A-Za-z]:\\\\|/home/|Traceback|line \d+, in ", text):
            leaky.append(path)
    check("no filesystem paths or tracebacks in API errors",
          "GET five endpoints that return str(e)", "no paths, no tracebacks",
          leaky or "none", lambda _: not leaky, severity="high")

    body = c.get("/api/ai/status").json()
    path = (body.get("db") or {}).get("path", "")
    check("ai/status does not expose an absolute DB path", "GET /api/ai/status",
          "a bare filename, not a directory layout", path or "(empty)",
          lambda v: "/" not in v and "\\" not in v, severity="medium",
          note="the endpoint is unauthenticated; the filename keeps the "
               "failed-volume-mount diagnostic without publishing the layout")
    check("ai/status still reports DB availability", "GET /api/ai/status",
          "'available' present", list(body.get("db") or {}),
          lambda keys: "available" in keys, severity="low",
          note="the redaction must not cost the diagnostic it exists for")

    check("ai/status does not expose an absolute data root", "GET /api/ai/status",
          "a bare directory name", (body.get("data") or {}).get("data_root", ""),
          lambda v: "/" not in v and "\\" not in v, severity="medium")

    # The redacted path is only acceptable because this replaces it.
    for section in ("db", "data"):
        check(f"ai/status reports {section} storage kind", "GET /api/ai/status",
              "one of bind/volume/anon/image/unknown",
              (body.get(section) or {}).get("storage", "missing"),
              lambda v: v in ("bind", "volume", "anon", "image", "unknown"),
              severity="high",
              note="this is what tells you a redeploy will keep the data; it "
                   "replaced the absolute path that used to serve that purpose")
        check(f"ai/status reports {section} persisted flag", "GET /api/ai/status",
              "a boolean", (body.get(section) or {}).get("persisted", "missing"),
              lambda v: isinstance(v, bool), severity="medium")

    r = c.get("/api/nonexistent")
    check("unknown API path returns a clean 404", "GET /api/nonexistent",
          "404, no traceback", r.status_code, lambda v: v == 404)


def test_input_fuzzing():
    group("input fuzzing", "high")
    c = _client()

    weird = [
        ("null byte", "a\x00b"),
        ("very long string", "a" * 10000),
        ("unicode", "𝕏𝕐ℤ"),
        ("rtl override", "‮abc"),
        ("newlines", "a\nb\rc"),
        ("format string", "%s%s%s%n"),
        ("template injection", "{{7*7}}"),
        ("jinja injection", "{{config.items()}}"),
        ("negative", "-1"),
        ("huge int", "9" * 40),
        ("float", "1.5"),
        ("empty", ""),
    ]
    for name, value in weird:
        r = c.get("/api/search", params={"q": value})
        check(f"search handles {name}", f"GET /api/search?q={value[:40]!r}",
              "status < 500", r.status_code, lambda v: v < 500, severity="high")

        r = c.get("/api/ratings", params={"position": value, "top_n": 5})
        check(f"ratings position handles {name}",
              f"GET /api/ratings?position={value[:40]!r}", "status < 500",
              r.status_code, lambda v: v < 500, severity="high")

    # Server-side template injection would show as 49 in the output.
    r = c.get("/api/search", params={"q": "{{7*7}}"})
    check("no server-side template evaluation",
          "GET /api/search?q={{7*7}}", "'49' does not appear", r.text[:200],
          lambda b: "49" not in b, severity="critical")

    for value in ["abc", "1.5", "-1", "1e999", "null", "[]"]:
        r = c.get(f"/api/live/{value}")
        check(f"live gameweek rejects {value!r}", f"GET /api/live/{value}",
              "422 or a clean non-5xx", r.status_code, lambda v: v < 500,
              severity="high")


def test_payload_limits():
    group("payload limits", "high")
    c = _client()

    # A body far larger than any real squad.
    huge = [{"element_id": i, "position": (i % 15) + 1} for i in range(50000)]
    r = c.post("/api/draft/999999996", json={"picks": huge})
    check("oversized draft body is rejected", "POST 50,000 picks (~2 MB)",
          "400, and quickly", r.status_code, lambda v: v == 400,
          severity="high",
          note="rejected on length before any per-pick work")

    r = c.post("/api/draft/999999996", data="not json",
               headers={"content-type": "application/json"})
    check("malformed JSON body is a 422", "POST 'not json'",
          "422, never 500", r.status_code, lambda v: v in (400, 422),
          severity="high")

    r = c.post("/api/draft/999999996", json={"picks": [{"element_id": 1,
                                                        "position": 1}] * 15})
    check("15 identical picks are rejected", "POST 15 copies of one player",
          "400", r.status_code, lambda v: v == 400, severity="high")

    deep = {"picks": []}
    node = deep
    for _ in range(200):
        node["nested"] = {}
        node = node["nested"]
    r = c.post("/api/draft/999999996", json=deep)
    check("deeply nested body does not crash", "POST 200-level nested JSON",
          "status < 500", r.status_code, lambda v: v < 500, severity="medium")

    c.delete("/api/draft/999999996")


def test_third_party_assets():
    group("third-party assets", "medium")
    c = _client()
    body = c.get("/").text

    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', body)
    check("external assets are enumerated", "GET / markup",
          "list of third-party origins", externals or "none",
          lambda _: True, severity="info")

    for url in externals:
        if url.endswith((".js", ".css")):
            tag = re.search(r"<[^>]*%s[^>]*>" % re.escape(url), body)
            tag = tag.group(0) if tag else ""
            check(f"SRI on {url.split('/')[2]}", url,
                  "integrity= and crossorigin= present", tag[:160] or "tag not found",
                  lambda t: "integrity=" in (tag or ""), severity="medium",
                  note="without SRI, a compromised CDN executes arbitrary "
                       "script on every page of the site")

    handlers = re.findall(r'\son(?:click|load|error)\s*=\s*"[^"]*"', body)
    check("no inline event handlers in the shell", "GET / markup",
          "no onclick=/onload=/onerror= attributes",
          handlers or "none", lambda h: h == "none", severity="low",
          note="two logo onerror fallbacks. Harmless in themselves, but each "
               "one needs a hash or a refactor before a CSP can be added, so "
               "this is part of the same job")


def test_privacy_surface():
    group("privacy", "medium")
    c = _client()

    r = c.get("/privacy")
    text = r.text.lower()
    topics = [
        ("what is stored", ["fpl id"]),
        ("cookies / local storage", ["cookie", "local storage", "localstorage"]),
        ("how long it is kept", ["retention", "months", "deleted automatically"]),
        ("how to have it removed", ["delete", "erasure", "removed"]),
    ]
    for label, terms in topics:
        check(f"privacy policy covers {label}", "GET /privacy",
              f"one of {terms}", [t for t in terms if t in text] or "none found",
              lambda hits: hits != "none found", severity="medium",
              note="UK GDPR: the site stores an FPL id and a squad against it")

    # The draft API is unauthenticated by design; state that as a finding rather
    # than a failure, so it stays visible.
    picks = [{"element_id": 100 + i, "position": i + 1} for i in range(15)]
    c.post("/api/draft/999999995", json={"picks": picks})
    r = c.get("/api/draft/999999995")
    check("any caller can read any id's draft", "GET /api/draft/<someone else's id>",
          "documented as unauthenticated", r.json().get("available"),
          lambda v: v is not True, severity="medium",
          note="deliberate - FPL id is the whole identity - but it means one "
               "guessable integer exposes and overwrites a stored squad")
    c.delete("/api/draft/999999995")


def test_http_methods():
    group("HTTP methods", "low")
    c = _client()
    r = c.request("TRACE", "/")
    check("TRACE is not enabled", "TRACE /", "405 or 404", r.status_code,
          lambda v: v in (404, 405, 501), severity="medium")

    r = c.post("/")
    expect("POST to a GET-only page is 405", "POST /", 405, r.status_code)

    r = c.delete("/api/ratings")
    expect("DELETE on a read endpoint is 405", "DELETE /api/ratings", 405,
           r.status_code)

    r = c.options("/api/ratings", headers={"origin": "https://evil.example"})
    check("no permissive CORS", "OPTIONS /api/ratings Origin: evil.example",
          "no Access-Control-Allow-Origin: *",
          r.headers.get("access-control-allow-origin", "absent"),
          lambda v: v != "*", severity="high",
          note="the API is read-only and public, but a wildcard would also "
               "expose the draft write endpoint to any origin")


def test_open_redirect():
    group("open redirect", "high")
    c = _client()
    for target in ["//evil.example", "https://evil.example",
                   "/\\evil.example", "http:/evil.example"]:
        r = c.get(f"/player/{target}", follow_redirects=False)
        loc = r.headers.get("location", "")
        check(f"no redirect off-site for {target!r}", f"GET /player/{target}",
              "no Location pointing at another host", loc or "no redirect",
              lambda _: "evil.example" not in loc, severity="high")


SUITES = [test_sql_injection, test_regex_injection, test_xss, test_path_traversal,
          test_auth_on_state_changing_routes, test_security_headers,
          test_information_disclosure, test_input_fuzzing, test_payload_limits,
          test_third_party_assets, test_privacy_surface, test_http_methods,
          test_open_redirect]
