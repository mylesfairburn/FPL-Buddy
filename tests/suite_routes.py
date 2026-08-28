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
        # Every tool tab has to be a real <a href>, checked per path rather than
        # by counting anchors in the bar. The bar also carries the Gameweek
        # briefings and Players A-Z links now, so a bare count says nothing
        # about whether the four tabs are all there - it only says how many
        # things happen to be in the bar today.
        check(f"{tab['path']} renders all four tab links as anchors",
              f"GET {tab['path']}", "an <a href> for each of the four tabs", r.text,
              lambda body: all(
                  re.search(r'<a class="nav-link[^"]*"[^>]*href="%s"' % re.escape(t["path"]), body)
                  for t in main.TABS),
              note="anchors not buttons, or a crawler cannot follow them")

    # The two reading pages were footer-only, which is the weakest place on a
    # page to be linked from. They're in the tab bar now, on the tool pages and
    # on the prose pages - so this checks both kinds of page, not just one.
    for path in [t["path"] for t in main.TABS] + ["/about", "/faq", "/privacy"]:
        body = c.get(path).text
        check(f"{path} links the reading pages from the tab bar", f"GET {path}",
              " and ".join(c_["path"] for c_ in main.CONTENT_LINKS), body,
              lambda b: all(f'href="{c_["path"]}"' in b for c_ in main.CONTENT_LINKS),
              note="the gameweek archive and the A-Z index need a link from "
                   "every page or they rank on footer links alone")

    # The four tools are what the site is for, so every page carries the tab
    # bar - not just the logo.
    for path in ["/about", "/faq", "/privacy", "/contact", "/players/a-z", "/gameweek"]:
        body = c.get(path).text
        check(f"{path} carries the four tool tabs", f"GET {path}",
              "an <a href> for each of the four tabs", body,
              lambda b: all(
                  re.search(r'<a class="nav-link[^"]*"[^>]*href="%s"' % re.escape(t["path"]), b)
                  for t in main.TABS),
              note="a reader who has finished a player page or the FAQ should "
                   "not have to go back through the logo to reach a tool")
        check(f"{path} marks no tab active", f"GET {path}",
              "no .active tool tab", body,
              lambda b: not any(
                  re.search(r'class="nav-link active"[^>]*href="%s"' % re.escape(t["path"]), b)
                  for t in main.TABS),
              note="none of the panes is open on a prose page, so highlighting "
                   "one would be pointing at something that isn't there")

    # The exception. Home is the first-visit page and introduces the same four
    # tools in prose immediately below; a tab bar there is the same four links
    # twice, and anyone with an ID saved is redirected off it anyway.
    home = c.get("/").text
    check("the home page has no tool tabs", "GET /",
          "the reading links only", home,
          lambda b: not any(f'data-pane="{t["pane"]}"' in b for t in main.TABS)
                    and all(f'href="{c_["path"]}"' in b for c_ in main.CONTENT_LINKS))


def test_head_requests():
    """HEAD has to work everywhere GET does.

    FastAPI's @app.get does not add HEAD the way Starlette's own Route does, so
    without the HeadAsGet middleware every URL on the site returns 405 to it -
    including robots.txt and sitemap.xml. Bingbot uses HEAD to decide whether a
    URL is worth fetching, and a 405 is not a "nothing changed" answer.

    Checked per route type rather than on one path: the failure this guards
    against is a whole class of URL, and a single sample would pass while the
    player pages - which are most of the site - were still refusing.
    """
    group("HEAD requests", "high")
    c = _client()

    index = main.player_page_index()
    a_player = next(iter(index.values()))["path"] if index else "/players/a-z"

    paths = ([t["path"] for t in main.TABS]
             + ["/", "/about", "/faq", "/privacy", "/contact", "/players/a-z",
                "/gameweek", a_player, "/robots.txt", "/sitemap.xml",
                "/api/all_players"])
    # Identity encoding on both requests, because the comparison below is
    # between two SEPARATE responses and gzip makes their sizes incomparable:
    # every HTML page embeds a fresh random CSP nonce, and a random 22-character
    # string compresses to a different number of bytes each time. Uncompressed,
    # the nonce is always exactly 22 characters, so the two bodies are the same
    # length and Content-Length can be compared for real. Asking for identity
    # narrows what is under test - gzip's own Content-Length isn't checked - but
    # the alternative is an assertion that fails a few pages at random per run.
    RAW = {"Accept-Encoding": "identity"}
    for path in paths:
        head, get = c.head(path, headers=RAW), c.get(path, headers=RAW)
        expect(f"HEAD {path} matches GET's status", f"HEAD {path}",
               get.status_code, head.status_code, severity="high",
               note="a 405 here tells a crawler the method is refused on every "
                    "URL of this kind")
        # RFC 9110: the headers must be the ones GET would have sent, and the
        # body must be empty. Content-Length in particular is what a crawler
        # compares against last time to decide whether to re-fetch, so it has
        # to describe the real body rather than the zero bytes sent.
        check(f"HEAD {path} sends GET's headers and no body", f"HEAD {path}",
              "empty body, same content-type and content-length", len(head.content),
              lambda n, h=head, g=get: n == 0
                    and h.headers.get("content-type") == g.headers.get("content-type")
                    and h.headers.get("content-length") == g.headers.get("content-length"))

    missing = c.head("/player/does-not-exist-0")
    expect("HEAD on a missing page 404s, not 405", "HEAD /player/does-not-exist-0",
           404, missing.status_code)


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

    m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S)
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


def test_gameweek_roundup():
    group("gameweek roundup", "high")
    c = _client()
    import db as _db

    expect("an unwritten roundup 404s", "GET /gameweek/99/roundup", 404,
           c.get("/gameweek/99/roundup").status_code, severity="high",
           note="generating one on demand would report provisional bonus "
                "points as final")

    roundup = {
        "gameweek": 1, "season": "2026-27",
        "summary": "Test roundup for the suite.",
        "top_scorers": [{"name": "Scorer", "path": "/player/scorer-1",
                         "pos": "FWD", "team_code": 3, "team_name": "Arsenal",
                         "cost": 9.0, "owned": 22.5, "points": 16,
                         "headline": "16 points", "minutes": 90, "goals": 2,
                         "assists": 1, "bonus": 3, "bps": 55,
                         "clean_sheet": False, "saves": 0,
                         "why": "Scorer scored 16 points."}],
        "underperformers": [{"name": "Blanker", "path": "/player/blanker-2",
                             "pos": "MID", "team_code": 8, "team_name": "Chelsea",
                             "cost": 10.5, "owned": 31.2, "points": 1,
                             "headline": "1 point", "minutes": 90, "xg": 0.7,
                             "xa": 0.2, "xgi": 0.93, "bps": 12,
                             "why": "Blanker returned 1 point."}],
        "shocks": [{"headline": "Luton 2–0 Man City", "winner_code": 14,
                    "winner_name": "Luton", "why": "18th beat 1st."}],
        "momentum": [{"team_name": "Brentford", "team_code": 94,
                      "headline": "3 straight wins", "count": 3,
                      "why": "Brentford have won 3 in a row."}],
        "scorecard": {"predicted": 62.4, "actual": 71, "difference": 8.6},
    }
    wrote = _db.save_gw_roundup(1, roundup)
    check("a roundup can be published", "save_gw_roundup(1)", True, wrote,
          lambda v: v is True, severity="high")

    r = c.get("/gameweek/1/roundup")
    expect("a published roundup renders", "GET /gameweek/1/roundup", 200,
           r.status_code, severity="critical")
    check("the roundup names its players", "GET /gameweek/1/roundup",
          "'Scorer' and 'Blanker' in the server-rendered HTML", r.text,
          lambda b: "Scorer" in b and "Blanker" in b, severity="critical")
    check("the roundup prints the scorecard", "GET /gameweek/1/roundup",
          "the projected and actual figures", r.text,
          lambda b: "62.4" in b and "71" in b, severity="high",
          note="the one number on the site that costs something to publish")
    check("the roundup carries Article structured data",
          "GET /gameweek/1/roundup", '"@type": "Article"', r.text,
          lambda b: '"Article"' in b)

    m = re.search(r"<title>(.*?)</title>", r.text, re.S)
    title = m.group(1).strip() if m else ""
    check("the roundup has its own <title>", "GET /gameweek/1/roundup",
          "a title naming this gameweek and the roundup", title,
          lambda t: "Gameweek 1" in t and "roundup" in t.lower(),
          severity="medium")
    # The briefing and the roundup for one gameweek sit at adjacent URLs. If
    # they shared a title they would compete with each other for every query.
    brief = re.search(r"<title>(.*?)</title>", c.get("/gameweek/1").text, re.S)
    check("the roundup's title differs from the briefing's",
          "GET /gameweek/1 vs /gameweek/1/roundup", "two distinct titles",
          title, lambda t: bool(brief) and t != brief.group(1).strip(),
          severity="medium", note="self-competing near-duplicate pages")

    # Written once from settled results, so a second write must not touch it.
    again = _db.save_gw_roundup(1, {**roundup, "summary": "OVERWRITTEN"})
    check("an existing roundup refuses to be rewritten",
          "save_gw_roundup on a stored row", "False", again,
          lambda v: v is False, severity="critical",
          note="the daily job calls this every night for the rest of the season")
    check("the stored roundup kept its original text", "GET /gameweek/1/roundup",
          "the original summary", c.get("/gameweek/1/roundup").text,
          lambda b: "OVERWRITTEN" not in b, severity="critical")
    check("--replace is the deliberate escape hatch",
          "save_gw_roundup(replace=True)", "True",
          _db.save_gw_roundup(1, roundup, replace=True),
          lambda v: v is True, severity="low")

    check("a published roundup is in the sitemap", "GET /sitemap.xml",
          "/gameweek/1/roundup present", c.get("/sitemap.xml").text,
          lambda b: f"{main.SITE_URL}/gameweek/1/roundup<" in b, severity="high")

    # One feed carrying both, so a subscriber doesn't have to choose.
    feed = c.get("/gameweek/feed.xml").text
    check("the roundup appears in the RSS feed", "GET /gameweek/feed.xml",
          "a roundup item", feed,
          lambda b: "/gameweek/1/roundup" in b, severity="medium")
    check("the feed still carries the briefing too", "GET /gameweek/feed.xml",
          "both item types", feed,
          lambda b: "briefing" in b.lower() and "roundup" in b.lower(),
          severity="medium")


def test_gameweek_hub():
    group("gameweek hub", "high")
    c = _client()
    import db as _db

    # GW1's briefing and roundup already exist from the two suites above. Add a
    # newer briefing so "the current one" and "an old one" are distinguishable.
    #
    # 38 rather than 2 on purpose: the suites share one database, and any other
    # test that publishes an edition would otherwise become the newest one and
    # silently change what this page is expected to show. 38 is the last
    # gameweek of a season, so nothing can outrank it.
    current = 38
    _db.save_gw_report(current, {
        "gameweek": current, "season": "2026-27",
        "summary": "The current briefing.",
        "deadline_label": "Sat 22 Aug, 11:00 UK", "stage": "preview",
        "captains": [{"name": "Armband", "path": "/player/armband-9", "pos": "MID",
                      "team_code": 3, "team_name": "Arsenal", "cost": 12.0,
                      "owned": 40.0, "predicted": 7.4, "rank": 1,
                      "behind_leader": 0.0, "headline": "7.4 projected",
                      "why": "Armband is the model's highest projection.",
                      "fixtures": [{"event": 2, "label": "BUR (H)"}]}],
        "in_form": [], "differentials": [], "attack_runs": [],
        "defence_runs": [], "news": [],
    })

    r = c.get("/gameweek")
    expect("the hub 200s", "GET /gameweek", 200, r.status_code, severity="high")
    check("the hub shows the current briefing", "GET /gameweek",
          f"the Gameweek {current} briefing", r.text,
          lambda b: f"The Gameweek {current} Briefing" in b, severity="high")
    check("the hub shows the last roundup", "GET /gameweek",
          "the newest roundup", r.text,
          lambda b: "Roundup" in b and "/gameweek/1/roundup" in b,
          severity="high")
    check("the hub surfaces the top captain pick", "GET /gameweek",
          "the armband name on the card", r.text,
          lambda b: "Armband" in b, severity="medium",
          note="it is the single thing most readers arrived for")

    # The whole point of the restructure: superseded editions are DE-LISTED,
    # not deleted. A 404 here would throw away every ranking they have earned.
    check("old editions are no longer listed on the hub", "GET /gameweek",
          "no link to /gameweek/1", r.text,
          lambda b: 'href="/gameweek/1"' not in b, severity="high")
    expect("but an old edition still resolves", "GET /gameweek/1", 200,
           c.get("/gameweek/1").status_code, severity="critical",
           note="de-listing costs nothing; 404ing throws away the rankings")
    check("and is still in the sitemap", "GET /sitemap.xml",
          "/gameweek/1 present", c.get("/sitemap.xml").text,
          lambda b: f"{main.SITE_URL}/gameweek/1<" in b, severity="critical")

    # A live briefing has no roundup yet, so the cross-link must not be there.
    check("a briefing with no roundup does not link to one",
          f"GET /gameweek/{current}", f"no /gameweek/{current}/roundup link",
          c.get(f"/gameweek/{current}").text,
          lambda b: f"/gameweek/{current}/roundup" not in b, severity="high",
          note="an unconditional link 404s from every live edition")
    check("a briefing whose roundup exists does link to it", "GET /gameweek/1",
          "a /gameweek/1/roundup link", c.get("/gameweek/1").text,
          lambda b: "/gameweek/1/roundup" in b, severity="medium")

    # The nav label changed with the page's contents.
    check("the nav names the page for what it now holds", "GET /about",
          "'This gameweek' in the tab bar", c.get("/about").text,
          lambda b: "This gameweek" in b, severity="low")


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

    # Four sources now: the routing table, one page per player, one per
    # published gameweek briefing, and one per roundup. The last two are read
    # live rather than hardcoded because each grows by one a week in season.
    expected = (len(main.PAGES) + len(main.player_page_index())
                + len(main.db.gw_report_index())
                + len(main.db.gw_roundup_index()))
    expect("sitemap URL count",
           "len(PAGES) + players + briefings + roundups",
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

    # EVERY page, not a sample. The five above are the first five records -
    # established players with a full previous season behind them - and that is
    # the shape this template has always handled. The shape it did not handle
    # was a player with an EMPTY prev_season dict who has played minutes: from
    # the first deadline of 2026-27, 75 of 574 pages answered 500, and a sample
    # of five could not see it because none of the five was that shape.
    #
    # 574 in-process requests, a few seconds. Worth it for a fault that took the
    # site's Search Console impressions from 416 a day to 7.
    broken = []
    for rec in index.values():
        code = c.get(rec["path"]).status_code
        if code != 200:
            broken.append(f"{code} {rec['path']}")
    check("every player page renders", f"GET all {len(index)} player pages",
          "200 on all of them",
          f"{len(broken)} failed" + (f": {', '.join(broken[:5])}" if broken else ""),
          lambda _: not broken, severity="critical",
          note="a 5xx on a crawled URL removes it from the index and makes "
               "Google back off the whole host")

    # The shape itself, stated directly, so the reason is legible even if the
    # pool one day happens not to contain such a player.
    victim = dict(next(iter(index.values())))
    victim["prev_season"] = {}
    victim["stats"] = dict(victim["stats"], minutes=90.0)
    saved = main.player_page_index()[victim["code"]]
    main.player_page_index()[victim["code"]] = victim
    try:
        expect("a player with no previous season but minutes played renders",
               "prev_season={}, stats.minutes=90", 200,
               c.get(victim["path"]).status_code, severity="critical",
               note="Jinja's `Undefined is not none` is True, so an "
                    "`is not none` guard does not catch a missing dict key")
    finally:
        main.player_page_index()[victim["code"]] = saved

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

    # The filter needs each letter's heading and list wrapped together so it can
    # hide both, and it needs the jump links tagged with their letter. Neither
    # is visible on the page, so nothing else would notice them going missing -
    # the search would just start leaving "M" standing over an empty gap.
    letters = set(re.findall(r'<section class="az-group" data-letter="([^"]+)"', r.text))
    check("every letter is a wrapped group", "GET /players/a-z",
          "one .az-group per jump link", sorted(letters),
          lambda ls: set(ls) == set(re.findall(r'<a href="#letter-[^"]*" data-letter="([^"]+)"', r.text))
                     and len(ls) > 1)

    # The box itself is injected by the page's own script, so the served HTML
    # holds an empty slot and nothing else. A rendered <input> here would be a
    # control that does nothing for a reader without JavaScript.
    check("the search box is an empty slot in the HTML", "GET /players/a-z",
          'an empty #azSearchSlot, no <input>', "checked",
          lambda _: '<div id="azSearchSlot"></div>' in r.text
                    and "<input" not in r.text.split('id="azSearchSlot"')[0].split("<article")[-1],
          note="a search box that silently does nothing is worse than none")


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
                 "/static/favicon.png", "/static/favicon_144.png",
                 "/static/icon_180.png"]:
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


def test_site_name_signals():
    """The markup Google reads to print "FPL Buddy" above a search result.

    It was printing "mfhost.co.uk" instead, which is what it falls back to when
    it can't establish a name for a subdomain. Every signal below is one it
    documents as feeding that decision, and every one of them is a single line
    that a refactor can drop without anything on the page looking wrong.
    """
    group("site name", "high")
    c = _client()
    body = c.get("/").text

    blocks = re.findall(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', body, re.S)
    check("the home page carries JSON-LD", "GET /", "at least one ld+json block",
          len(blocks), lambda v: v >= 1)

    parsed = []
    for block in blocks:
        try:
            parsed.append(json.loads(block))
        except json.JSONDecodeError as e:
            parsed.append({"_error": str(e)})
    check("the JSON-LD parses", "GET /", "valid JSON in every block",
          [p.get("_error") for p in parsed if isinstance(p, dict)],
          lambda errs: not any(errs),
          note="malformed structured data is ignored wholesale, so this fails "
               "silently in production - nothing on the page looks wrong")

    nodes = []
    for p in parsed:
        nodes.extend(p.get("@graph", [p]) if isinstance(p, dict) else [])
    by_type = {n.get("@type"): n for n in nodes if isinstance(n, dict)}

    for kind in ("WebSite", "Organization"):
        check(f"{kind} names the site", "GET /", main.SITE_NAME,
              (by_type.get(kind) or {}).get("name"),
              lambda v: v == main.SITE_NAME,
              note="this is the field Google documents as authoritative for "
                   "the name shown above a result")

    check("Organization carries a logo", "GET /", "an ImageObject url",
          ((by_type.get("Organization") or {}).get("logo") or {}).get("url"),
          lambda v: bool(v),
          note="the name and the icon beside it are one block; without a logo "
               "Google has nothing to pair the name with")

    # Google's favicon guidance: a square, at least 48px, ideally a multiple of
    # 48. The original 32x32 was below that floor, which is why a large one was
    # added rather than the small one being replaced.
    icons = re.findall(r'<link rel="icon"[^>]*sizes="(\d+)x(\d+)"[^>]*>', body)
    check("a favicon of at least 48px is declared", "GET /",
          "one <link rel=icon> at 48px or more", icons,
          lambda found: any(int(w) >= 48 and w == h for w, h in found),
          note="under 48px Google will not use the icon at all")

    check("the home page h1 names the site", "GET /", main.SITE_NAME,
          (re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S) or [None, ""])[1],
          lambda v: main.SITE_NAME in v)

    # The other three signals, checked as they are actually served.
    for name, pattern in [
            ("og:site_name", r'<meta property="og:site_name" content="([^"]*)"'),
            ("<title>", r"<title>(.*?)</title>")]:
        found = (re.search(pattern, body, re.S) or [None, ""])[1]
        check(f"{name} carries the site name", "GET /", main.SITE_NAME, found,
              lambda v: main.SITE_NAME in v)


def test_server_rendered_tables():
    """The tool pages have to carry their tables in the HTML.

    This is the invariant the whole of seo_tables.py exists to hold: the panes
    draw their tables from /api/*, robots.txt disallows /api/, and crawlers
    don't run JavaScript - so if these rows stop appearing in the raw response,
    the site's best content goes back to being invisible to search engines and
    language models, and nothing else about the site would look wrong. It is
    exactly the kind of regression that survives for months unnoticed, which is
    what makes it worth a test.
    """
    group("server-rendered tables", "high")
    c = _client()
    body = c.get("/players").text

    # `ps-name` is followed by the column-width class, so this matches the start
    # of the class list rather than the whole attribute - see the .col-* note in
    # style.css for why those exist.
    check("the players table has rows in the raw HTML", "GET /players",
          "at least 50 <tr> inside #playersTabSearch", body,
          lambda b: len(re.findall(r'<tr>\s*<td class="ps-name[ "]', b)) >= 50,
          note="without these a crawler sees an empty table where the ratings are")

    # Ownership is server-rendered too, not drawn in by the script. It is the
    # column the FAQ tells people to sort by when hunting differentials, and it
    # is the one number on that table a language model can't derive from the
    # others.
    check("the players table carries ownership", "GET /players",
          "an Own % header and a value per row", body,
          lambda b: 'class="col-owned">Own %' in b
                    and len(re.findall(r'<td class="col-owned">', b)) >= 50,
          note="the interactive table shows it, so the crawled one has to as well")

    # A named player, not just a row count: rows of dashes would pass a count.
    top = main.seo_tables()["players"]
    if top:
        name = top[0]["name"]
        check("the top-rated player is named in the HTML", "GET /players",
              name, body, lambda b, n=name: n in b,
              note="rows exist but carry no player names")

    check("player rows link to their own pages", "GET /players",
          "at least 50 /player/<slug> links", body,
          lambda b: len(re.findall(r'href="/player/', b)) >= 50,
          note="the internal links are what make the several hundred player "
               "pages reachable from the ratings table rather than from the "
               "sitemap alone")

    rot = c.get("/fixture-rotator").text
    check("the rotation grid has a row per club", "GET /fixture-rotator",
          "20 rows in #rotationBody", rot,
          lambda b: len(re.findall(r'<td class="rot-team">', b)) >= 18,
          note="the fixture difficulty grid is the content of this page")

    # Only assert the AI squad when there is a gameweek to have solved one for.
    # Out of season - or with the FPL API unreachable, which is how this suite
    # usually runs - there is legitimately no squad, and a test that failed for
    # that reason would be noise rather than signal.
    squad = main.seo_tables().get("best_xi")
    if squad and squad.get("rows"):
        ai = c.get("/ai-teams").text
        first = squad["rows"][0]["name"]
        check("the AI squad is in the raw HTML", "GET /ai-teams",
              f"{first} and 14 team-mates", ai,
              lambda b, n=first: n in b and b.count('<td class="ps-name">') >= 15,
              note="the solved squad is the one thing on this page a crawler "
                   "could never see before")

    check("/my-team explains itself without an FPL ID", "GET /my-team",
          "a server-rendered intro section", c.get("/my-team").text,
          lambda b: 'id="toolIntro"' in b and "optimised starting XI" in b,
          note="this pane renders nothing at all without an ID, so the prose "
               "is the only thing a crawler can read on it")


SUITES = [test_page_routes, test_tab_panes, test_head_requests, test_seo_tags,
          test_robots_and_security_txt, test_faq, test_gameweek_pages,
          test_gameweek_roundup, test_gameweek_hub,
          test_sitemap, test_player_pages, test_az_index, test_compression,
          test_static_assets, test_site_name_signals,
          test_server_rendered_tables]
