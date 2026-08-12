"""HTTP routing, HTML correctness and SEO plumbing.

Everything a crawler or a first-time visitor touches. A failure here is usually
invisible in a browser and expensive in search rankings.
"""

import json
import re
import xml.etree.ElementTree as ET

import main
from harness import check, expect, group

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _client():
    from context import client
    return client


def test_page_routes():
    group("page routes", "critical")
    c = _client()
    for path in main.PAGES:
        r = c.get(path)
        expect(f"GET {path} status", f"GET {path}", 200, r.status_code,
               severity="critical")
        check(f"GET {path} is HTML", f"GET {path}", "text/html content-type",
              r.headers.get("content-type", ""), lambda v: "text/html" in v)
        check(f"GET {path} has a non-trivial body", f"GET {path}",
              ">2000 bytes of HTML", len(r.text), lambda v: v > 2000)
        check(f"GET {path} document is not cached", f"GET {path}",
              "no-cache on the HTML document",
              r.headers.get("cache-control", ""), lambda v: "no-cache" in v,
              note="the document carries the versioned asset URLs")

    r = c.get("/nonexistent-page")
    expect("unknown path 404s", "GET /nonexistent-page", 404, r.status_code)

    r = c.get("/my-team/")
    check("trailing slash does not 500", "GET /my-team/",
          "redirect or 404, never 5xx", r.status_code, lambda v: v < 500)


def test_tab_panes():
    group("tab routing", "high")
    c = _client()
    for tab in main.TABS:
        r = c.get(tab["path"])
        check(f"{tab['path']} opens {tab['pane']}", f"GET {tab['path']}",
              f'__INITIAL_PANE__ = "{tab["pane"]}"', r.text,
              lambda body, p=tab["pane"]: f'__INITIAL_PANE__ = "{p}"' in body,
              severity="high")
        check(f"{tab['path']} marks its own nav link active", f"GET {tab['path']}",
              "the matching nav-link carries .active", r.text,
              lambda body, t=tab: re.search(
                  r'class="nav-link active"[^>]*href="%s"' % re.escape(t["path"]), body)
              or re.search(r'href="%s"[^>]*class="nav-link active"' % re.escape(t["path"]), body))
        check(f"{tab['path']} renders all four tab links as anchors",
              f"GET {tab['path']}", "4 <a class=nav-link> elements", r.text,
              lambda body: len(re.findall(r'<a class="nav-link', body)) == 4,
              note="anchors not buttons, or a crawler cannot follow them")


def test_seo_tags():
    group("SEO metadata", "high")
    c = _client()
    titles, descs = {}, {}
    for path, meta in main.PAGES.items():
        body = c.get(path).text
        m = re.search(r"<title>(.*?)</title>", body, re.S)
        title = m.group(1).strip() if m else ""
        titles[path] = title
        expect(f"{path} title matches PAGES", f"GET {path}",
               meta["title"], title, severity="high")

        m = re.search(r'<meta name="description" content="(.*?)">', body, re.S)
        desc = m.group(1).strip() if m else ""
        descs[path] = desc
        # 160 is Bing's limit - it reports anything longer as an error - and
        # Google truncates at roughly the same width, so a longer description
        # has a tail no search result will ever show.
        check(f"{path} has a description of usable length", f"GET {path}",
              "50-160 characters", f"{len(desc)} chars",
              lambda _, d=desc: 50 <= len(d) <= 160)

        m = re.search(r'<link rel="canonical" href="(.*?)">', body)
        canonical = m.group(1) if m else ""
        expect(f"{path} canonical is absolute and correct", f"GET {path}",
               main.SITE_URL + path, canonical, severity="high")

        check(f"{path} has og:title", f"GET {path}", "og:title present", body,
              lambda b: 'property="og:title"' in b, severity="medium")

    check("every page title is unique", str(list(titles)),
          "no two pages share a title", len(set(titles.values())),
          lambda v: v == len(titles), severity="high",
          note="duplicate titles make pages compete with each other")
    check("every description is unique", str(list(descs)),
          "no two pages share a description", len(set(descs.values())),
          lambda v: v == len(descs), severity="high")


def test_robots_and_security_txt():
    group("robots / security.txt", "medium")
    c = _client()
    r = c.get("/robots.txt")
    expect("robots.txt 200", "GET /robots.txt", 200, r.status_code)
    check("robots.txt points at the sitemap", "GET /robots.txt",
          f"Sitemap: {main.SITE_URL}/sitemap.xml", r.text,
          lambda b: f"Sitemap: {main.SITE_URL}/sitemap.xml" in b, severity="high")
    check("robots.txt disallows /api/", "GET /robots.txt", "Disallow: /api/",
          r.text, lambda b: "Disallow: /api/" in b)
    check("robots.txt does not block the whole site", "GET /robots.txt",
          "no bare 'Disallow: /'", r.text,
          lambda b: not re.search(r"^Disallow: /$", b, re.M), severity="critical",
          note="one stray character here delists the entire site")

    r = c.get("/.well-known/security.txt")
    expect("security.txt 200", "GET /.well-known/security.txt", 200, r.status_code)
    check("security.txt has Contact and Expires", "GET /.well-known/security.txt",
          "both RFC 9116 mandatory fields", r.text,
          lambda b: "Contact:" in b and "Expires:" in b)

    r = c.get("/llms.txt")
    expect("llms.txt 200", "GET /llms.txt", 200, r.status_code)
    check("llms.txt links are absolute", "GET /llms.txt",
          "no relative hrefs - the file may be read out of context", r.text,
          lambda b: "](/" not in b, severity="medium")
    check("llms.txt points at pages that exist", "GET /llms.txt",
          "every linked path returns 200", r.text,
          lambda b: all(c.get(p).status_code == 200
                        for p in re.findall(r"\]\(%s(/[a-z0-9/-]*)\)"
                                            % re.escape(main.SITE_URL), b)),
          severity="medium",
          note="a stale link here misinforms exactly the readers it exists for")


def test_faq():
    group("FAQ page", "medium")
    c = _client()
    r = c.get("/faq")
    expect("/faq 200", "GET /faq", 200, r.status_code, severity="high")

    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S)
    check("/faq carries FAQPage JSON-LD", "GET /faq", "one ld+json block", bool(m),
          lambda v: v, severity="medium")
    if not m:
        return
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        check("/faq JSON-LD parses", "GET /faq", "valid JSON", str(exc),
              lambda _: False, severity="high",
              note="invalid JSON-LD is ignored wholesale, not partially")
        return
    check("/faq JSON-LD parses", "GET /faq", "valid JSON", "parsed",
          lambda _: True, severity="high")
    expect("/faq JSON-LD is a FAQPage", "GET /faq", "FAQPage", data.get("@type"))
    questions = data.get("mainEntity", [])
    expect("/faq JSON-LD has every question", "GET /faq",
           len(main.FAQ_ITEMS), len(questions))

    # The failure this guards against: someone edits FAQ_ITEMS' wording, the
    # markup updates, the visible page doesn't (or vice versa) - and Google
    # treats structured data that isn't on the page as a manual-action risk.
    # Both come from one list today, so this stays green unless that changes.
    for item in main.FAQ_ITEMS:
        check(f"FAQ question is visible on the page: {item['q'][:40]}",
              "GET /faq", "question text present in the HTML body", item["q"],
              lambda q, b=r.text: q.replace('"', "&#34;") in b or q in b,
              severity="medium")


def test_gameweek_pages():
    group("gameweek briefings", "high")
    c = _client()

    r = c.get("/gameweek")
    expect("/gameweek archive 200", "GET /gameweek", 200, r.status_code,
           severity="high")

    # An unpublished gameweek must 404 rather than generating on demand: a page
    # built at request time would be dated wrong and would put the whole rating
    # pipeline behind a URL a crawler can hit.
    r = c.get("/gameweek/99")
    expect("an unpublished gameweek 404s", "GET /gameweek/99", 404, r.status_code,
           severity="high")

    r = c.get("/gameweek/feed.xml")
    expect("the RSS feed 200s", "GET /gameweek/feed.xml", 200, r.status_code)
    check("the feed is XML", "GET /gameweek/feed.xml", "application/rss+xml",
          r.headers.get("content-type", ""), lambda v: "xml" in v)
    check("the feed parses", "GET /gameweek/feed.xml", "well-formed XML", r.text,
          lambda b: ET.fromstring(b) is not None, severity="high",
          note="an aggregator drops a malformed feed silently")
    # Declaration order matters here: /gameweek/{gw} would swallow "feed.xml"
    # and try to read it as an integer if it were declared first.
    check("feed.xml is not matched as a gameweek number",
          "GET /gameweek/feed.xml", "not a 404 or 422", r.status_code,
          lambda v: v == 200, severity="high")

    # Publish a real edition and read it back, so the template is exercised
    # rather than just the empty-archive path.
    import db as _db
    payload = {
        "gameweek": 1, "season": "2026-27", "deadline": None,
        "summary": "Test edition for the suite.",
        "in_form": [{"name": "Tester", "path": "/player/tester-1", "pos": "MID",
                     "team_code": 3, "team_name": "Arsenal", "cost": 7.0,
                     "owned": 12.5, "predicted": 5.5, "form": 6.0,
                     "headline": "6.0 form", "why": "Because this is a test.",
                     "fixtures": [{"event": 1, "label": "CHE (H)"}]}],
        "differentials": [], "attack_runs": [], "defence_runs": [], "news": [],
    }
    wrote = _db.save_gw_report(1, payload)
    check("an edition can be published", "save_gw_report(1)", True, wrote,
          lambda v: v is True, severity="high")

    r = c.get("/gameweek/1")
    expect("a published edition renders", "GET /gameweek/1", 200, r.status_code,
           severity="critical")
    check("the edition names its players", "GET /gameweek/1",
          "'Tester' in the server-rendered HTML", r.text,
          lambda b: "Tester" in b, severity="critical",
          note="the whole point is being readable without JavaScript")
    check("the edition carries Article structured data", "GET /gameweek/1",
          '"@type": "Article"', r.text, lambda b: '"Article"' in b)
    m = re.search(r"<title>(.*?)</title>", r.text, re.S)
    check("the edition has its own <title>", "GET /gameweek/1",
          "a title naming this gameweek", m.group(1).strip() if m else "",
          lambda t: "Gameweek 1" in t, severity="medium",
          note="38 near-identical titles get classed as duplicates")

    # Freezing is what makes the archive trustworthy: after it, a nightly run
    # must not be able to rewrite the page.
    froze = _db.freeze_gw_report(1)
    check("an edition can be frozen", "freeze_gw_report(1)", True, froze,
          lambda v: v is True, severity="high")
    again = _db.save_gw_report(1, {**payload, "summary": "OVERWRITTEN"})
    check("a frozen edition refuses to be rewritten", "save_gw_report on a frozen row",
          "False, and the payload unchanged", again,
          lambda v: v is False, severity="critical",
          note="this refusal is the only thing making the archive honest")
    check("the frozen edition kept its original text", "GET /gameweek/1",
          "the original summary, not 'OVERWRITTEN'", c.get("/gameweek/1").text,
          lambda b: "OVERWRITTEN" not in b, severity="critical")
    check("re-freezing is idempotent", "freeze_gw_report(1) twice",
          "False the second time", _db.freeze_gw_report(1),
          lambda v: v is False,
          note="the hourly watcher may call it more than once in the window")

    r = c.get("/sitemap.xml")
    check("a published edition is in the sitemap", "GET /sitemap.xml",
          "/gameweek/1 present", r.text,
          lambda b: f"{main.SITE_URL}/gameweek/1<" in b, severity="high")
    check("a frozen edition is marked changefreq never", "GET /sitemap.xml",
          "<changefreq>never</changefreq> on the frozen edition", r.text,
          lambda b: "never" in b, severity="low",
          note="stops Google recrawling dead pages looking for an edit "
               "that will never come")


def test_sitemap():
    group("sitemap.xml", "high")
    c = _client()
    r = c.get("/sitemap.xml")
    expect("sitemap 200", "GET /sitemap.xml", 200, r.status_code, severity="critical")
    check("sitemap content-type is XML", "GET /sitemap.xml", "application/xml",
          r.headers.get("content-type", ""), lambda v: "xml" in v)

    try:
        root = ET.fromstring(r.content)
        parsed = True
    except ET.ParseError as e:
        root, parsed = None, False
        check("sitemap is well-formed XML", "GET /sitemap.xml", "parses",
              str(e), lambda _: False, severity="critical")
    if not parsed:
        return

    locs = [el.text for el in root.iter(f"{SM}loc")]
    check("sitemap parses as XML", "GET /sitemap.xml", "valid urlset",
          f"{len(locs)} <loc> entries", lambda _: True, severity="critical")

    # Three sources now: the routing table, one page per player, and one per
    # published gameweek edition. The edition count is read live rather than
    # hardcoded because it grows by one a week during a season.
    expected = (len(main.PAGES) + len(main.player_page_index())
                + len(main.db.gw_report_index()))
    expect("sitemap URL count",
           "len(PAGES) + len(player_page_index()) + published editions",
           expected, len(locs), severity="high")
    check("every loc is absolute", "all <loc> values",
          f"all start with {main.SITE_URL}", len(locs),
          lambda _: all((l or "").startswith(main.SITE_URL) for l in locs),
          severity="high")
    check("no duplicate URLs", "all <loc> values", "all unique",
          len(locs) - len(set(locs)),
          lambda dupes: dupes == 0, severity="high")
    check("under the 50,000 URL limit", "len(locs)", "<= 50000", len(locs),
          lambda v: v <= 50000)
    check("no unescaped ampersands", "raw sitemap body",
          "no bare & outside an entity", r.text,
          lambda b: not re.search(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", b),
          severity="high",
          note="a player named O'Brien & Co would otherwise break the file")
    check("priorities are in range", "all <priority> values", "0.0-1.0",
          "checked", lambda _: all(
              0.0 <= float(el.text) <= 1.0 for el in root.iter(f"{SM}priority")))
    check("changefreq values are valid", "all <changefreq> values",
          "sitemaps.org vocabulary", "checked", lambda _: all(
              el.text in {"always", "hourly", "daily", "weekly", "monthly",
                          "yearly", "never"} for el in root.iter(f"{SM}changefreq")))

    # Sampled, not exhaustive: 570 requests would dominate the run time.
    sample = locs[:8] + locs[len(locs) // 2: len(locs) // 2 + 8] + locs[-8:]
    bad = []
    for loc in sample:
        path = loc[len(main.SITE_URL):]
        code = c.get(path).status_code
        if code != 200:
            bad.append(f"{path} -> {code}")
    check("sampled sitemap URLs all resolve 200",
          f"{len(sample)} sampled URLs (head/middle/tail)",
          "every one returns 200", bad or "all 200",
          lambda _: not bad, severity="critical",
          note="a sitemap listing a 404 is a crawl-budget leak")


def test_player_pages():
    group("player pages", "high")
    c = _client()
    index = main.player_page_index()
    check("player index is populated", "main.player_page_index()",
          "several hundred records", len(index), lambda v: v > 100,
          severity="critical")

    slugs = [r["slug"] for r in index.values()]
    check("slugs are unique", "all page records", "no collisions",
          len(slugs) - len(set(slugs)), lambda d: d == 0, severity="critical",
          note="a collision means two players share one URL")
    check("every slug ends in its code", "all page records",
          "slug ends with the record's code", "checked",
          lambda _: all(r["slug"].endswith(str(r["code"])) for r in index.values()),
          severity="high")
    check("every path is /player/<slug>", "all page records", "consistent paths",
          "checked",
          lambda _: all(r["path"] == f"/player/{r['slug']}" for r in index.values()))

    sample = list(index.values())[:5]
    for rec in sample:
        r = c.get(rec["path"])
        expect(f"GET {rec['path']}", f"GET {rec['path']}", 200, r.status_code,
               severity="high")
        body = r.text
        check(f"{rec['path']} names the player", rec["path"],
              "web_name appears in the body", rec["web_name"],
              lambda n, b=body: n in b)
        check(f"{rec['path']} carries Person JSON-LD", rec["path"],
              '"@type": "Person"', "checked",
              lambda _, b=body: '"@type"' in b and "Person" in b)
        check(f"{rec['path']} prose contains no 'nan'", rec["path"],
              "no NaN leaking into sentences", "checked",
              lambda _, b=body: not re.search(r"\bnan\b", b, re.I), severity="high")
        check(f"{rec['path']} prose contains no 'None'", rec["path"],
              "no Python None in sentences", "checked",
              lambda _, b=body: not re.search(r"\bNone\b", b), severity="medium")

    # Canonicalisation: the words are decoration, the number identifies.
    rec = sample[0]
    r = c.get(f"/player/wrong-words-{rec['code']}", follow_redirects=False)
    expect("non-canonical slug 301s", f"GET /player/wrong-words-{rec['code']}",
           301, r.status_code, severity="high")
    expect("301 points at the canonical path",
           f"GET /player/wrong-words-{rec['code']}",
           rec["path"], r.headers.get("location"), severity="high")

    for bad, why in [("no-digits-here", "no trailing number"),
                     ("999999999", "unknown code"),
                     ("", "empty slug"),
                     ("-", "just a hyphen")]:
        r = c.get(f"/player/{bad}")
        check(f"404 for a slug with {why}", f"GET /player/{bad}",
              "404 (or 307 for the empty case)", r.status_code,
              lambda v: v in (404, 307), severity="high")


def test_az_index():
    group("A-Z index page", "medium")
    c = _client()
    r = c.get("/players/a-z")
    expect("A-Z page 200", "GET /players/a-z", 200, r.status_code)
    links = re.findall(r'href="(/player/[^"]+)"', r.text)
    index = main.player_page_index()
    expect("A-Z links to every player", "GET /players/a-z",
           len(index), len(set(links)), severity="high",
           note="the page is what stops the player pages being orphans")
    check("A-Z links all exist in the index", "GET /players/a-z",
          "no dangling links", "checked",
          lambda _: set(links) <= {r["path"] for r in index.values()},
          severity="high")


def test_compression():
    group("compression", "medium")
    c = _client()
    r = c.get("/api/all_players", headers={"accept-encoding": "gzip"})
    check("large API response is gzipped", "GET /api/all_players (accept gzip)",
          "content-encoding: gzip", r.headers.get("content-encoding", "none"),
          lambda v: v == "gzip", severity="medium")
    r = c.get("/", headers={"accept-encoding": "gzip"})
    check("HTML is gzipped", "GET / (accept gzip)", "content-encoding: gzip",
          r.headers.get("content-encoding", "none"), lambda v: v == "gzip")


def test_static_assets():
    group("static assets", "medium")
    c = _client()
    for path in ["/static/app.js", "/static/style.css", "/static/kits.js",
                 "/static/favicon.png", "/static/icon_180.png"]:
        r = c.get(path)
        expect(f"{path} served", f"GET {path}", 200, r.status_code, severity="high")
        check(f"{path} is cacheable", f"GET {path}", "max-age=86400",
              r.headers.get("cache-control", ""), lambda v: "86400" in v)

    # app.js only loads on the app shell, so the landing page is checked for
    # the stylesheet alone.
    for path, assets in [("/", ["/static/style.css"]),
                         ("/my-team", ["/static/style.css", "/static/app.js",
                                       "/static/kits.js"])]:
        body = c.get(path).text
        for asset in assets:
            check(f"{asset} is version-stamped on {path}", f"GET {path}",
                  f"{asset}?v=<token>", "checked",
                  lambda _, a=asset, b=body: re.search(
                      re.escape(a) + r"\?v=[0-9a-f]{6,}", b),
                  severity="high",
                  note="without the token a phone can pin itself to stale CSS")


SUITES = [test_page_routes, test_tab_panes, test_seo_tags,
          test_robots_and_security_txt, test_faq, test_gameweek_pages,
          test_sitemap, test_player_pages, test_az_index, test_compression,
          test_static_assets]
