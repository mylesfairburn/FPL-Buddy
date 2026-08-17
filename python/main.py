"""
FastAPI backend for FPL Companion.

Run locally with:
    uvicorn main:app --reload
"""

import hmac
import json
import math
import os
import re
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape

import pandas as pd
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import ai_manager
import ai_team
import db
import drafts
import gameweek as gw_clock
import gw_report
import kits
import manager_history
import ops
import player_pages
import seasons
import seo_tables as seo_tables_builder
import social
from pipeline import run_pipeline
from rating_model import StaleModelError, get_rated_position_dfs
from fixture_rotator import (get_rotation_data, rank_rotation_pairs, recommend_pair_players, team_fixture_map)
from search import search_player
from squad_optimiser import DEFAULT_BUDGET, OptimisationError
from team_service import (get_team_view, get_league_standings, get_all_players, get_player_summary,
                          get_news_feed, get_underperforming_players,
                          get_overperforming_players, team_name_map,
                          _team_short_map as get_team_short_map)
from gameweek import detect_mode

# Set this in production. Once /api/refresh drives DB writes and a minute of
# pipeline work, leaving it open to the internet is a free denial-of-service;
# unset (local dev) it stays open so nothing breaks for a local run.
REFRESH_TOKEN = os.environ.get("FPL_REFRESH_TOKEN", "")

# Absolute origin, needed for canonical URLs, Open Graph tags and the sitemap -
# all of which have to be absolute to work at all. Overridable so a staging
# host doesn't advertise the production URL to crawlers.
SITE_URL = os.environ.get("FPL_SITE_URL", "https://fpl.mfhost.co.uk").rstrip("/")
SITE_NAME = "FPL Buddy"

# IndexNow: push changed URLs to Bing, Yandex, Seznam and Naver instead of
# waiting to be crawled. Google has never adopted the protocol, so this does
# nothing for Google rankings.
#
# Ownership is proved by serving the key back as plain text at /<key>.txt, so
# the route's path IS the key and the route can only be declared once one is
# configured. The file has to be at the root: a key hosted under a subdirectory
# limits submissions to URLs beneath that directory, which would exclude
# everything except the directory itself.
INDEXNOW_KEY = os.environ.get("FPL_INDEXNOW_KEY", "").strip()
# Validated rather than trusted, because the value is interpolated into a route
# path. The spec allows 8-128 characters of a-z, A-Z, 0-9 and dashes; anything
# else would either fail verification or register a nonsense route.
if INDEXNOW_KEY and not re.fullmatch(r"[A-Za-z0-9-]{8,128}", INDEXNOW_KEY):
    print("FPL_INDEXNOW_KEY is not a valid IndexNow key (8-128 chars of "
          "a-z, A-Z, 0-9, -); the key route will not be served.")
    INDEXNOW_KEY = ""

# Lets the gameweek social drafts be read from a phone. The drafts are also
# served at /api/social/<gw> behind the X-Refresh-Token header, which is the
# stronger protection but is unusable on a phone: no mobile browser can set a
# custom header, so that endpoint needs curl and a laptop.
#
# So this is a capability URL - the secret is the path itself, and anyone
# holding the link can read it. That is genuinely weaker than a header: it
# lands in browser history, and a link tapped from it could leak the path in a
# Referer. The trade is deliberate and narrow. What's behind it is draft post
# text assembled from a page anybody can already read, so the realistic cost of
# a leak is that a stranger sees next week's tweet early.
#
# Set it to something unguessable and treat it as a password anyway:
#   openssl rand -hex 32
#
# Unset, the route doesn't exist. Deliberately under /api/, which robots.txt
# already disallows, so no crawler follows it even if the URL escapes.
SOCIAL_KEY = os.environ.get("FPL_SOCIAL_KEY", "").strip()
# Same reasoning as the IndexNow key: this value is interpolated into a route
# path, so it's validated rather than trusted. 24 characters minimum because a
# capability URL with a short key is a URL someone can guess.
if SOCIAL_KEY and not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", SOCIAL_KEY):
    print("FPL_SOCIAL_KEY must be 24-128 chars of a-z, A-Z, 0-9, - or _ "
          "(try: openssl rand -hex 32); the phone route will not be served.")
    SOCIAL_KEY = ""

# Role addresses rather than personal ones, so they can be reassigned without
# rewriting the site. Kept server-side and rendered obfuscated - see the note in
# the contact template about scrapers.
# Ko-fi. Set FPL_KOFI_HANDLE to the name in your Ko-fi URL - if your page is
# ko-fi.com/mylesf, the handle is "mylesf". Left unset the button simply isn't
# rendered, which is deliberate: a hard-coded guess would send anyone who
# clicked it to a stranger's donation page.
KOFI_HANDLE = os.environ.get("FPL_KOFI_HANDLE", "mylesfairburn").strip().strip("/")
KOFI_URL = f"https://ko-fi.com/{KOFI_HANDLE}" if KOFI_HANDLE else ""

EMAILS = {
    "general": "hello@fpl.mfhost.co.uk",
    "contact": "contact@fpl.mfhost.co.uk",
    "security": "security@fpl.mfhost.co.uk",
    "abuse": "abuse@fpl.mfhost.co.uk",
}

# The four tabs used to be fragments of one document (#pane-players and so on).
# A fragment is discarded before a request is ever made, so to a crawler they
# were all the same URL - four different tools competing to rank as one page,
# and that page's server-rendered HTML was barely forty words. Each pane now has
# a real path that renders the same shell with that tab already open, and the
# tables inside it come down as HTML too - see seo_tables.py.
#
# This is the one definition of the tab bar: the routes below, the links the
# template renders and the pane app.js opens all come from here, so a fifth tab
# is one entry rather than four edits in three files.
TABS = [
    {"path": "/my-team", "pane": "pane-team", "label": "My Team"},
    {"path": "/ai-teams", "pane": "pane-ai-teams", "label": "AI Teams"},
    {"path": "/players", "pane": "pane-players", "label": "Players"},
    {"path": "/fixture-rotator", "pane": "pane-rotator", "label": "Fixture Rotator"},
]
TAB_PANES = {t["path"]: t["pane"] for t in TABS}


def tab_links():
    """The tab bar, with each tab's title and <h1> alongside its label.

    app.js reads `title` and `h1` so a tab switch can rewrite document.title and
    the heading without a page load - otherwise the browser tab, and anything
    bookmarked from it, keeps the name of whichever page you happened to arrive
    on. They're carried on the prose pages too, where nothing reads them, so
    there is one shape of tab rather than two.

    Rendered on every page except the home page. The four tools are what the
    site is for, and a reader who has finished the FAQ or a player's page
    shouldn't have to go back through the logo to reach one - previously these
    pages offered the two reading links and nothing else."""
    return [dict(t, title=PAGES[t["path"]]["title"], h1=PAGES[t["path"]]["h1"])
            for t in TABS]

# The two pages that are reading rather than tools. Both were reachable only
# from the footer, which is the one place on a page a reader never looks and a
# crawler weights least - and they are the site's only accumulating content
# (38 dated editions a season) and its index of several hundred player pages.
# Being one click from every page is worth more to them than to anything else
# on the site.
#
# Rendered at the far right of the same bar as the four tool tabs, styled
# lighter: they are a different kind of destination, and the tools should still
# read as the primary choice. Same list feeds the prose pages, where there are
# no tool tabs and these sit on their own at the left.
#
# `short` is the label used below 992px, where six full-length items stop
# fitting on one row. Both labels are rendered and CSS picks between them, so
# the choice can't depend on a viewport a crawler doesn't have.
# "This gameweek" rather than "Gameweek briefings", which is what this said
# when the page listed briefings and nothing else. It now carries two things -
# the upcoming briefing and the last completed roundup - and a label naming one
# of them would send anyone looking for the other past it.
CONTENT_LINKS = [
    {"path": "/gameweek", "label": "Gameweek Tips", "short": "Gameweek"},
    {"path": "/injuries", "label": "Injury News", "short": "Injuries"},
    {"path": "/players/a-z", "label": "Players A–Z", "short": "A–Z"},
]

# Per-route metadata. Every indexable page needs its own title and description:
# without them Google writes the snippet itself, usually by grabbing whatever
# text it finds first. `priority`/`changefreq` feed the sitemap; both default
# below, so a route only states them when it differs.
#
# Descriptions are kept to 160 characters. That is Bing's stated limit - it
# reports anything longer as an error - and Google truncates around the same
# point, so the tail of a longer one is written for nobody. The check in
# tests/suite_routes.py fails the build if one drifts back over.
#
# `lastmod` is the date the PROSE was last edited, and only the prose pages
# carry one. Everything else here is rebuilt from the nightly data pull, so its
# real modification date is that data's - see sitemap() for where that comes
# from. Bump this by hand when you change the words on one of these pages; a
# stale date costs a recrawl, an always-today date costs the whole file's
# credibility (again, see sitemap()).
PAGES = {
    "/": {
        "title": "FPL Buddy — Fantasy Premier League ratings and AI teams",
        "description": ("Free FPL tools: player ratings from a trained points model, two "
                        "AI-picked squads each gameweek, a fixture rotation planner and "
                        "your own team analysed."),
        "priority": "1.0",
        "changefreq": "weekly",
    },
    "/my-team": {
        "title": "My FPL team — squad, lineup and transfer analysis | FPL Buddy",
        "h1": "My FPL team",
        "description": ("Load your FPL squad by ID: predicted points for every player, an "
                        "optimised starting XI, transfer suggestions, chip advice and live "
                        "gameweek scoring."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/ai-teams": {
        "title": "AI Fantasy Premier League teams — AI Manager and best XI",
        "h1": "AI Fantasy Premier League teams",
        "description": ("Two AI-picked FPL squads: a manager bot that plays every gameweek "
                        "with real transfers and chips, and the highest-scoring XV for the "
                        "upcoming gameweek."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/players": {
        "title": "FPL player ratings and predicted points | FPL Buddy",
        "h1": "FPL player ratings",
        "description": ("Every FPL player rated by a trained points model, with predicted "
                        "points, price, form, ownership and the players underperforming "
                        "their underlying numbers."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/fixture-rotator": {
        "title": "FPL fixture rotation planner — best team pairs | FPL Buddy",
        "h1": "FPL fixture rotation planner",
        "description": ("Find FPL defence and attack rotation pairs: two clubs whose "
                        "fixtures alternate so one always has a good game, ranked over the "
                        "next eight gameweeks."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    # The one page here whose content genuinely turns over on its own schedule
    # rather than the pipeline's - a flag can change at any hour of any day, and
    # this is the page people arrive at from a search made minutes after the
    # news broke. Priority sits with the tool pages for that reason.
    "/injuries": {
        # No apostrophe, deliberately. Titles are escaped on the way into the
        # <title> tag, so "who's" renders as "who&#39;s" and stops matching the
        # string here - which the SEO metadata suite checks and which would
        # otherwise ship a title with an HTML entity visible in it.
        "title": "FPL injury news — injured, suspended and doubtful players",
        "h1": "FPL injury news",
        "description": ("Every flagged Fantasy Premier League player: who is injured, "
                        "suspended or doubtful, their chance of playing, and what FPL "
                        "says about each one."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/players/a-z": {
        "title": "Every Fantasy Premier League player A-Z | FPL Buddy",
        "description": ("A complete list of every Fantasy Premier League player, grouped by "
                        "surname, each linking to their price, ownership, form and predicted "
                        "points."),
        "priority": "0.8",
        "changefreq": "weekly",
    },
    "/about": {
        "title": "About FPL Buddy — how the ratings and AI squads work",
        "description": ("How FPL Buddy rates players, how the two AI squads are picked, "
                        "and what the numbers on the site actually mean."),
        "lastmod": "2026-08-14",
    },
    "/gameweek": {
        "title": "This FPL gameweek — briefing and roundup | FPL Buddy",
        "description": ("The FPL gameweek in two write-ups: who to captain and who is in "
                        "form before the deadline, then who scored and who blanked "
                        "after it."),
        # Daily rather than weekly now. The page holds whichever briefing and
        # roundup are current, so its content genuinely turns over twice a week
        # rather than gaining an entry at the top of a list.
        "priority": "0.8",
        "changefreq": "daily",
    },
    "/faq": {
        "title": "FPL Buddy FAQ — ratings, AI squads and your FPL ID",
        "description": ("Answers to the common questions: where to find your FPL ID, how "
                        "the predicted points are worked out, how accurate they are, and "
                        "what the AI squads do."),
        "priority": "0.7",
        "changefreq": "monthly",
        "lastmod": "2026-08-17",
    },
    # `yearly` on both, against a default of `monthly`. Google ignores
    # changefreq outright; Bing does read it, and a notice that changes when the
    # law does is not a monthly page.
    "/privacy": {
        "title": "Privacy policy — FPL Buddy",
        "description": ("What FPL Buddy stores, why, how long for, and how to have it "
                        "deleted."),
        "lastmod": "2026-08-10",
        "changefreq": "yearly",
    },
    "/contact": {
        "title": "Contact FPL Buddy",
        "description": "How to report a bug, ask a question, or raise a rights or security issue.",
        "lastmod": "2026-08-10",
        "changefreq": "yearly",
    },
}

# The FAQ, as data rather than as markup.
#
# One list feeding both the visible page and the FAQPage structured data is the
# point: Google treats an answer in the JSON-LD that doesn't appear in the
# readable HTML as a spam signal, and the usual way that happens is two copies
# drifting apart after an edit. Written once, they can't.
#
# Worth being honest about what this earns. Google restricted FAQ rich results
# to government and health sites in 2023, so this will not put a dropdown under
# the search result. It's here because the markup is still read by Bing and,
# more to the point, because a question-and-answer block in plain server-side
# HTML is the format language models quote from most readily - and unlike the
# four tool pages, this content is fully visible without JavaScript.
#
# Answers may contain inline HTML; the same string is used in both places, so
# `text` in the JSON-LD carries the same markup, which the spec allows.
FAQ_ITEMS = [
    {
        "q": "What is FPL Buddy?",
        "a": ("A free set of Fantasy Premier League tools. It rates every player with a "
              "trained points model, picks two AI squads each gameweek, finds pairs of "
              "clubs whose fixtures rotate well, and analyses your own squad from your "
              "FPL ID. It is an unofficial fan project with no connection to the "
              "Premier League."),
    },
    {
        "q": "How do I find my FPL ID?",
        "a": ("Log in at fantasy.premierleague.com, open the Points tab, and look at the "
              "address bar. The URL reads <code>/entry/1234567/event/5</code> and your ID "
              "is the number after <code>/entry/</code>. It is public, it is not a "
              "password, and it is safe to share."),
    },
    {
        "q": "Does FPL Buddy need my Fantasy Premier League password?",
        "a": ("No, and it never will. Everything here uses the public read-only FPL API, "
              "which needs nothing but your numeric team ID. That also means the site "
              "cannot make your transfers for you — it can only recommend them, and you "
              "apply them yourself in the official app."),
    },
    {
        "q": "How are the predicted points calculated?",
        "a": ("Two gradient-boosted models per position, because goalkeepers, defenders, "
              "midfielders and forwards earn points in different ways — and because "
              "\"how many will he score\" is really two questions. Just over half of all "
              "gameweek rows are players who didn't play, so one model across that "
              "mixture mostly learns who plays and flattens everything else. Instead: a "
              "classifier for whether he starts, and a Poisson model for what he scores "
              "when he does, fitted only on appearances. Inputs are FPL's own per-gameweek "
              "columns over a three- and a six-gameweek window plus a season-to-date rate, "
              "with home or away and opponent strength. All of them are shifted back a "
              "gameweek so a player's own result can never leak into the features "
              "predicting it. <a href=\"/about\">Full detail on the about page</a>."),
    },
    {
        "q": "How accurate are the predictions?",
        "a": ("Useful for comparing players against each other; poor as a forecast of any "
              "single score. Football over ninety minutes is not very predictable, and a "
              "model working from a handful of inputs will be wrong about individual "
              "players most weeks. The honest use is \"who is the better pick\", not "
              "\"how many will he get\"."),
    },
    {
        "q": "My team rating went up but I scored fewer points. Why?",
        "a": ("Because the rating isn't a points forecast. A player's rating out of 100 "
              "is his projected points ranked against the other players in his "
              "position, so the best goalkeeper and the best forward both read 100 "
              "while being worth very different scores. Your team rating averages that "
              "across the starting eleven, which measures squad quality and has no "
              "points meaning. It also describes the players as rated today rather than "
              "for one gameweek, and it ignores your captain. The predicted points "
              "figure next to it is the number that answers \"how many this week\". "
              "<a href=\"/about\">More on the about page</a>."),
    },
    {
        "q": "What is the difference between AI Best XI and AI Manager?",
        "a": ("AI Best XI is rebuilt from scratch every gameweek with no memory: the "
              "highest-scoring legal £100m squad for the week ahead, spending almost "
              "nothing on the bench because it has no transfers to plan for. AI Manager "
              "plays the actual game — one squad across the season, a bank balance, free "
              "transfers, four-point hits when a move earns one, and a single set of "
              "chips — so it judges players over several gameweeks rather than one."),
    },
    {
        "q": "Why do the two AI squads disagree?",
        "a": ("Because they are answering different questions. Buying the best captain "
              "for one Saturday and selling him a fortnight later costs a hit and a free "
              "transfer, which Best XI never pays and AI Manager always does. When they "
              "agree on a player, that is a stronger signal than either on its own."),
    },
    {
        "q": "How does the fixture rotator work?",
        "a": ("It looks for pairs of clubs whose fixtures alternate, so that in most "
              "gameweeks at least one of the two has a favourable game. Own a defender "
              "from each and you can start whichever has the better fixture that week. "
              "Pairs are ranked over the next eight gameweeks, separately for defence "
              "and attack."),
    },
    {
        "q": "What does \"underperforming\" mean on the players page?",
        "a": ("A player whose returns are behind their underlying numbers: attackers "
              "scoring fewer goals than their expected goals suggest, or goalkeepers and "
              "defenders conceding more than their expected goals conceded. It is a list "
              "of players who may be due a correction, not a list of players who "
              "definitely are."),
    },
    {
        "q": "Is a particular player injured, and when is he back?",
        "a": ("The <a href=\"/injuries\">injury news page</a> lists every player Fantasy "
              "Premier League currently flags — injured, suspended or doubtful — with the "
              "game's own chance-of-playing percentage where it gives one. Return dates "
              "are not guessed at: if FPL hasn't published a number, neither does this "
              "site. Each player's own page carries the same status alongside his form "
              "and projections."),
    },
    {
        "q": "What does \"overperforming\" mean on the players page?",
        "a": ("The same comparison the other way round: attackers scoring more than their "
              "expected goals, or defences conceding fewer than their expected goals "
              "conceded. It is not a sell list. A player can stay ahead of his underlying "
              "numbers for a long time — good finishers do it for whole seasons — so read "
              "it as a run that may not hold rather than one that is about to end."),
    },
    {
        "q": "What is a differential, and how do I find one?",
        "a": ("A player owned by a small share of managers, so that when they return "
              "points you gain ground on almost everyone. Sort the "
              "<a href=\"/players\">player ratings</a> by predicted points and look for "
              "low ownership — a high projection at low ownership is the combination "
              "worth having."),
    },
    {
        "q": "How often is the data updated?",
        "a": ("The full pipeline runs nightly at 03:00 UK time: prices, ownership, form, "
              "injuries, fixtures and every projection. A lighter check runs hourly to "
              "catch deadlines and freeze the AI squads' predictions before each one. "
              "Live scores update during matches."),
    },
    {
        "q": "Is FPL Buddy free?",
        "a": ("Yes, entirely, with no account and no sign-up. There is a Ko-fi link in "
              "the footer if you want to help with the server bill, but nothing on the "
              "site is behind it."),
    },
]

app = FastAPI()


class HeadAsGet:
    """Answer HEAD wherever GET is answered.

    Starlette adds HEAD to any route that declares GET. FastAPI's `@app.get`
    does not, so every page here - the tabs, the several hundred player pages,
    robots.txt, sitemap.xml - answered GET and returned 405 to HEAD. That is
    wrong on its own terms: HTTP defines HEAD as GET without the body, and a
    resource that exists is not entitled to refuse it.

    It also matters for crawling. Bingbot uses HEAD to decide whether a URL is
    worth fetching and whether it has changed since last time, and a 405 is not
    a "nothing changed" answer - it is the origin saying the method isn't
    allowed, on every URL on the site at once.

    Done as one ASGI middleware rather than by adding methods=["GET", "HEAD"] to
    thirty-odd decorators: the player pages are generated from a routing table,
    so a per-route change is both more edits and easier to forget on the next
    route added.

    RFC 9110 requires the headers to be the ones GET would have sent, so they go
    through untouched - Content-Length included. Only the body is dropped. That
    does mean the response is built in full and thrown away, which is the price
    of not maintaining a second code path that could disagree with the first;
    HEAD is a rounding error in traffic.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            return await self.app(scope, receive, send)

        body_sent = False

        async def send_headers_only(message):
            nonlocal body_sent
            if message["type"] == "http.response.body":
                # A streamed response sends several body messages. Exactly one
                # empty terminal message goes out for all of them: sending
                # another after more_body=False is a protocol violation, and
                # some servers close the connection on it.
                if body_sent:
                    return
                body_sent = True
                message = {"type": "http.response.body", "body": b"",
                           "more_body": False}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_headers_only)


# app.js is ~107 KB and style.css another ~60 KB uncompressed, and the JSON from
# /api/all_players is larger than both. Gzip takes roughly 70-80% off all three.
# minimum_size skips the small responses, where the compression work and the
# lost chance of a byte-range request cost more than the saved bytes.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Outermost, so the method is already GET by the time anything else - routing,
# the security headers, gzip - looks at the request. Added last because Starlette
# applies middleware in reverse order of registration.
app.add_middleware(HeadAsGet)


def content_security_policy(nonce):
    """The policy, built around a per-request nonce.

    `script-src` takes the nonce and nothing else - no 'unsafe-inline', which
    would make the whole exercise decorative. That is only possible because
    every inline on* attribute has been removed: script-src blocks those
    regardless of where the markup came from, including strings this app's own
    JavaScript builds and assigns through innerHTML.

    `style-src` still carries 'unsafe-inline', and that is a real gap rather
    than an oversight. The front end sets element.style directly in a number of
    places (the pitch layout, the chip cards, sizing that depends on measured
    widths), and a nonce cannot cover a style property assigned from script.
    Measured rather than assumed: /fixture-rotator alone renders 739 elements
    carrying a style attribute, /ai-teams 499. Closing it means moving those to
    classes. Worth doing; it is not what protects against script injection,
    which is what this policy is for.

    Every directive is 'self' now that Bootstrap is vendored into /static -
    there is no third-party origin left to allow. `font-src` needs nothing
    beyond 'self' because Bootstrap 5.3 ships a system font stack and requests
    no webfont, and `img-src` keeps `data:` for the 23 inline SVGs its form
    controls and accordion arrows are drawn with.

    `object-src 'none'` and `base-uri 'self'` are the two cheap ones people
    forget: the first kills a class of plugin-based bypass, the second stops an
    injected <base> tag repointing every relative URL on the page.
    """
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers a browser needs in order to defend the page.

    The nonce is minted BEFORE the handler runs, not after, because the
    templates have to render it into their <script> tags - a value generated
    once the response body exists would be too late to appear in it. It rides on
    request.state, which is where page_context() reads it from.

    HSTS is only sent over HTTPS. Sent on a plain-HTTP local run it would pin
    the developer's browser to https://localhost and make the app unreachable.
    """
    request.state.csp_nonce = secrets.token_urlsafe(16)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Only on documents. A JSON response carries no scripts to govern, and a
    # policy on /static would apply to assets the policy itself permits.
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault(
            "Content-Security-Policy", content_security_policy(request.state.csp_nonce))
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

templates = Jinja2Templates(directory="templates")

# Read-only endpoints derived from the in-memory rated pool. That pool only
# changes when load_data() runs - the nightly job, or a manual /api/refresh - so
# a browser or CDN reusing a response for a few minutes can't show anything the
# server wouldn't have said anyway. It's `public` because none of it is
# per-user: the same bytes go to everyone, which is what lets the Cloudflare
# edge answer instead of this process.
API_CACHE = "public, max-age=300"


def _json_safe(value):
    """Recursively replace NaN/Infinity with None.

    pandas uses NaN for "no number here", and NaN is not valid JSON: the
    serialiser raises rather than emitting it, so one missing value turns the
    whole response into a 500. That is not an edge case here - preseason,
    roughly two in five rated players have no predicted_points at all, and the
    per-gameweek dicts inside next_gameweeks can carry NaN of their own, which
    is why this walks nested structures instead of just the top-level row.

    The endpoints that build their payload through team_service already come
    out clean; these are the two that hand a DataFrame straight to FastAPI.
    """
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def records(df):
    """DataFrame rows as JSON-safe dicts."""
    return [_json_safe(row) for row in df.to_dict(orient="records")]


class CachedStatic(StaticFiles):
    """Static files with an explicit, bounded cache lifetime.

    Without a Cache-Control header the browser applies its own heuristics, and
    mobile Safari in particular will hold a stylesheet well past a deploy - even
    through a manual cache clear, because the cached HTML keeps asking for the
    same URL. A day is short enough that an unversioned asset (an icon, a chip
    image) can't go stale for long, and style.css/app.js don't rely on it: their
    URLs carry a version token that changes the moment the file does.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response


app.mount("/static", CachedStatic(directory="static"), name="static")
templates.env.cache = None  # workaround for a Jinja2/Starlette/Python 3.14 bug


def obf_mail(address):
    """Render an address as "user (at) domain" with the parts in data
    attributes.

    A bare mailto: in the HTML is harvested by scrapers within days. The layout
    script reassembles this into a real link on load, so a human sees a normal
    clickable address and a crawler reading the raw markup does not."""
    from markupsafe import Markup, escape
    user, _, domain = str(address).partition("@")
    return Markup(
        f'<a class="obf-mail" href="#" data-u="{escape(user)}" data-d="{escape(domain)}">'
        f'{escape(user)} <span aria-hidden="true">(at)</span> {escape(domain)}</a>')


templates.env.globals["obf_mail"] = obf_mail


def asset_version():
    """Short token that changes whenever the CSS or JS changes on disk.

    Appended to those URLs so a deploy produces a URL the browser has never
    seen, which is the only reliable way to retire a cached copy - "clear cache"
    on a phone is not something you can ask a user to do, and it doesn't always
    work anyway."""
    import hashlib
    h = hashlib.sha1()
    for name in ("static/style.css", "static/app.js", "static/kits.js"):
        try:
            st = os.stat(name)
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:10]

state = {"mode": "preseason", "position_dfs": None, "rotation_df": None}


def load_data(mode=None):
    """Load rated data for `mode`, or auto-detect it from the first gameweek
    deadline when no mode is given (the normal path - there's no manual toggle).

    'inseason' needs current-season gameweek history to exist; if the deadline
    has passed but that data hasn't been pulled yet, fall back to preseason
    ratings rather than failing to start. The app still works, just off last
    season's form, and the next refresh picks up inseason once the data lands."""
    if mode is None:
        mode = detect_mode()
    print(f"Loading FPL data ({mode})...")
    data = run_pipeline()

    try:
        position_dfs = get_rated_position_dfs(data["position_dfs"], mode=mode)
    except ValueError as e:
        if mode != "inseason":
            raise
        print(f"Can't use inseason ratings yet ({e}) - falling back to preseason.")
        mode = "preseason"
        position_dfs = get_rated_position_dfs(data["position_dfs"], mode=mode)
    except StaleModelError as e:
        # The model bundles on the volume are older than this code. Serve the
        # site without ratings rather than refusing to start.
        #
        # This ran as a hard failure exactly once, which was enough. A deploy
        # never ships new bundles - they are gitignored, so the image has none,
        # and ensure_seeded() only copies files that are MISSING, so it cannot
        # replace the ones already on the volume. The first deploy after the
        # model changed therefore met bundles it could not read, raised out of
        # startup(), and uvicorn exited. The container crash-looped, cloudflared
        # had nothing to forward to, and the whole site returned a Cloudflare
        # 502 - prose pages, gameweek archive and all - because the projections
        # could not be computed.
        #
        # Refusing to print wrong numbers was right. Refusing to serve anything
        # was not: `state["position_dfs"] = None` is a state every consumer
        # already handles (see /api/ratings), so the tables come back empty and
        # everything that isn't a projection carries on working.
        print(f"MODEL BUNDLES ARE STALE: {e}\n"
              "Serving without ratings. Retrain to restore them:\n"
              "  docker exec fpl-buddy python train_model.py")
        state["mode"] = mode
        state["position_dfs"] = None
        state["rotation_df"] = None
        state["degraded"] = str(e)
        clear_preview_cache()
        return

    state.pop("degraded", None)
    rotation_df = get_rotation_data(mode=mode, n_gameweeks=8)

    state["mode"] = mode
    state["position_dfs"] = position_dfs
    state["rotation_df"] = rotation_df
    clear_preview_cache()   # ratings moved; any cached AI squad is now stale

    # Build the server-rendered tables now rather than leaving them to whoever
    # asks first. Assembling them solves the Best-XI ILP, and the first request
    # after a refresh is as likely to be a crawler as a person - making a bot
    # wait on a linear program is how a crawl budget gets spent on timeouts.
    # Non-fatal by design: these are an addition to the pages, never a
    # precondition for serving them.
    try:
        seo_tables()
    except Exception as e:
        print(f"couldn't prebuild the server-rendered tables ({e}); "
              "the pages will render without them.")

    print(f"Ready ({mode}).")


# ---------------------------------------------------------------------------
#  Warm-up
# ---------------------------------------------------------------------------
# load_data() used to run inside startup(), which meant uvicorn answered
# nothing until it finished - the pipeline, rating the whole pool, the rotation
# table and the Best-XI ILP behind seo_tables(). Locally that is 15 seconds; on
# the VM it is longer, and it was most of the minute the site was unreachable
# on every watchtower deploy. The container swap itself is a few seconds.
#
# So the load moved to a background thread and startup() returns immediately.
# The app is up and serving in about as long as it takes to import pandas.
#
# This is only safe because serving without ratings is a state the app already
# supports deliberately - see the StaleModelError branch above, where
# `position_dfs = None` leaves every non-projection page working. The warm-up
# window is the same state, reached for a different reason and lasting seconds.
READY = threading.Event()

# One load at a time. The nightly cron jobs poke /api/refresh, and without this
# a job firing during a deploy would run a second full pipeline concurrently
# with the warm-up - two rating passes competing for the same CPU, then racing
# to assign `state`.
_LOAD_LOCK = threading.Lock()


def load_data_serialised(mode=None):
    """load_data() with the lock held, and READY set once it has run."""
    with _LOAD_LOCK:
        try:
            load_data(mode)
        finally:
            READY.set()


def _warm_up():
    """The startup load, in the background.

    Swallows failures on purpose. On the startup path an exception killed
    uvicorn and the container crash-looped, taking down prose pages and the
    gameweek archive along with the projections; in a thread the same
    exception would silently kill the thread and leave READY unset, which
    would 503 the whole site forever. Neither is acceptable, so a failure is
    recorded where /api/ai/status will show it and the app serves what it can.
    The next nightly refresh retries from scratch.
    """
    try:
        load_data_serialised()
    except Exception as e:
        print(f"WARNING: the initial data load failed ({e}). Serving without "
              "ratings - the next /api/refresh or nightly job will retry.")
        state["degraded"] = f"initial load failed: {e}"


# Paths that mean something without ratings, and so stay up during the warm-up
# window. An allowlist rather than a list of pages to block, deliberately: a
# route added later and forgotten then answers 503 for twenty seconds, which is
# a temporary and self-correcting mistake. The other way round it would serve a
# crawler an empty table and invite it to index that, which is neither.
_WARMUP_SAFE_PREFIXES = (
    "/static/",
    "/about", "/privacy", "/faq", "/contact",       # prose, no projections
    "/robots.txt", "/llms.txt", "/.well-known/",
    "/api/ai/status",                               # the ops check must not 503
)


def _serves_without_ratings(path):
    if INDEXNOW_KEY and path == f"/{INDEXNOW_KEY}.txt":
        return True
    return path.startswith(_WARMUP_SAFE_PREFIXES)


@app.middleware("http")
async def warmup_gate(request: Request, call_next):
    """503 + Retry-After while the ratings are still being built.

    A 503 rather than an empty page because this is a crawler-facing site:
    Google is documented to treat 503 as "come back later" and leaves the
    indexed version alone, whereas a 200 carrying an empty table is an
    invitation to index the empty version. Retry-After tells it how long.

    `no-store` matters more than it looks. Cloudflare sits in front of this,
    and a cached 503 would outlive the twenty seconds that justified it.
    """
    if READY.is_set() or _serves_without_ratings(request.url.path):
        return await call_next(request)

    headers = {"Retry-After": "20", "Cache-Control": "no-store"}
    if request.url.path.startswith("/api/"):
        return Response(
            content=json.dumps({"detail": "Starting up - ratings are still "
                                          "being built. Retry shortly."}),
            status_code=503, media_type="application/json", headers=headers)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Starting up</title>"
        "<meta http-equiv=refresh content=20>"
        "<style>body{font-family:system-ui,sans-serif;margin:4rem auto;"
        "max-width:32rem;padding:0 1rem;line-height:1.5}</style>"
        "<h1>Just a moment</h1><p>FPL Buddy is starting up and rebuilding its "
        "projections. This page will reload itself in a few seconds.</p>",
        status_code=503, headers=headers)


def _player_pool():
    """Flat rated player pool from the in-memory state, for DB read-joins."""
    if state["position_dfs"] is None:
        return []
    return get_all_players(state["position_dfs"]).get("players", [])


# Solving an ILP per page view would make the AI tabs feel slow and burn CPU
# re-deriving an answer that only changes when the underlying ratings do. These
# hold the live preview for the upcoming gameweek; load_data() clears them, so
# a refresh (or the nightly job) is what invalidates, not a timer.
_preview_cache = {"best_xi": {}, "manager": {}}


def clear_preview_cache():
    _preview_cache["best_xi"].clear()
    _preview_cache["manager"].clear()
    _player_pages["index"] = None
    _seo_tables["data"] = None


# Several hundred page records, each walking every rated DataFrame row. Building
# that per request would be absurd when the answer only changes when the ratings
# do, so it's built once per data load and thrown away by clear_preview_cache().
_player_pages = {"index": None}


def player_page_index():
    """code -> page record for every player in the pool."""
    if _player_pages["index"] is None:
        pages = player_pages.build_index(state["position_dfs"])
        try:
            pages = player_pages.attach_team_names(
                pages, team_name_map(), get_team_short_map())
        except Exception as e:
            # A club-name lookup failing shouldn't cost us the pages; the
            # records carry a neutral fallback.
            print(f"couldn't attach club names to player pages: {e}")
            pages = player_pages.attach_team_names(pages, {}, {})
        _player_pages["index"] = pages
    return _player_pages["index"]


# The server-rendered tables for /players, /ai-teams and /fixture-rotator.
#
# Built on first request after a data load and cleared by clear_preview_cache()
# with everything else, so it can never describe a different set of ratings than
# the API does. Lazy rather than built inside load_data(): assembling it solves
# an ILP and asks the season clock what gameweek it is, and neither belongs on
# the path that has to complete before the app can serve anything.
_seo_tables = {"data": None}

# The empty shape, so a template can always ask for a key. Falling back to this
# rather than to None means a build failure costs the tables and nothing else -
# the pages still render, exactly as they did before any of this existed.
_EMPTY_SEO_TABLES = {"players": [], "rotation": None, "best_xi": None,
                     "best_xi_history": [], "manager_history": []}


def seo_tables():
    """Display-ready rows for the tables the panes otherwise draw in JS.

    Every input is fetched defensively. This runs on the four pages a crawler
    hits most, and none of it is load-bearing for a reader - so a database
    fault, an unreachable season clock or an unsolvable squad has to cost the
    server-rendered table only, never the page."""
    if _seo_tables["data"] is None:
        players = []
        try:
            pool = _player_pool()
            paths = {code: rec["path"] for code, rec in player_page_index().items()}
            players = [dict(p, path=paths.get(p.get("code"))) for p in pool]
        except Exception as e:
            print(f"couldn't build the server-rendered player table: {e}")

        best_xi = None
        try:
            target = gw_clock.next_gameweek()
            if target is not None and players:
                stored = ai_team.get_snapshot(target, players)
                best_xi = stored or cached_best_xi(target)
        except Exception as e:
            print(f"couldn't build the server-rendered AI squad: {e}")

        def _safe(fn, label):
            try:
                return fn()
            except Exception as e:
                print(f"couldn't build the server-rendered {label}: {e}")
                return []

        _seo_tables["data"] = seo_tables_builder.build(
            players=players,
            rotation_df=state["rotation_df"],
            best_xi=best_xi,
            snapshots=_safe(ai_team.list_snapshots, "Best XI track record"),
            mgr_history=_safe(ai_manager.history, "AI Manager track record"),
        )
    return _seo_tables["data"] or _EMPTY_SEO_TABLES


def cached_best_xi(gameweek, budget=DEFAULT_BUDGET):
    key = (gameweek, budget)
    if key not in _preview_cache["best_xi"]:
        _preview_cache["best_xi"][key] = ai_team.build_best_xi(
            _player_pool(), gameweek, budget=budget)
    return _preview_cache["best_xi"][key]


def cached_manager_preview(gameweek):
    if gameweek not in _preview_cache["manager"]:
        _preview_cache["manager"][gameweek] = ai_manager.run_gameweek(
            _player_pool(), gameweek, persist=False)
    return _preview_cache["manager"][gameweek]


def require_refresh_token(token):
    if REFRESH_TOKEN and token != REFRESH_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing refresh token.")


@app.on_event("startup")
def startup():
    # Creating the schema is idempotent, and doing it here means a fresh
    # container comes up with a usable DB without a manual init step.
    try:
        seasons.ensure_seeded()
    except Exception as e:
        print(f"WARNING: couldn't seed the data volume ({e}).")
    try:
        print(f"SQLite: {db.init_db()}")
    except Exception as e:
        print(f"WARNING: couldn't initialise SQLite ({e}); AI tabs will be unavailable.")
    # Deliberately NOT awaited: see the warm-up block above. Startup returns in
    # milliseconds, uvicorn starts answering, and the ratings land underneath.
    # daemon=True so a shutdown mid-load doesn't hold the process open.
    threading.Thread(target=_warm_up, name="fpl-warmup", daemon=True).start()

def page_context(request, path, meta=None, **extra):
    """Shared template context: metadata for `path` plus site-wide values.

    `meta` overrides the PAGES lookup, for routes whose title and description
    are per-record rather than per-route - the player pages, where there are
    several hundred of them and each one describes a different footballer."""
    meta = meta or PAGES.get(path, PAGES["/"])
    return {
        "request": request,
        "asset_version": asset_version(),
        # Minted by the security_headers middleware before this handler ran.
        # Every inline <script> in the templates must carry it or the browser
        # will refuse to run it.
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "site_url": SITE_URL,
        "site_name": SITE_NAME,
        "emails": EMAILS,
        "kofi_url": KOFI_URL,
        "page_title": meta["title"],
        "page_description": meta["description"],
        "canonical": SITE_URL + path,
        # Google reads the WebSite name only from a site's home page, so the
        # structured data block is emitted there and nowhere else. Stated as a
        # flag rather than worked out in the template from request.url.path:
        # this module owns the routing table, and /player/<slug> pages render
        # through the same head partial.
        "is_home": path == "/",
        # Marked active here rather than compared in the template: this module
        # owns the routing table, and `path` is already the canonical answer to
        # "which page is this" - reading it back off request.url.path would be a
        # second source that can disagree (trailing slashes, redirects).
        "content_links": [dict(c, active=c["path"] == path) for c in CONTENT_LINKS],
        # The tool tabs, on every page but the home one. Home is the first-visit
        # page and its whole job is to introduce the four tools in prose; a tab
        # bar above that would be the same four links twice, and the tabs are
        # also what a returning visitor is redirected past. An empty list is
        # falsy, so the partial renders exactly what it did before there.
        #
        # No tab is marked active on a prose page: `initial_pane` is undefined
        # there, and none of the panes is open. tab_response overrides this with
        # its own list - `**extra` lands last in this dict.
        "tabs": [] if path == "/" else tab_links(),
        "today": datetime.now(timezone.utc).strftime("%-d %B %Y")
                 if os.name != "nt" else datetime.now(timezone.utc).strftime("%d %B %Y").lstrip("0"),
        # Club colours for the server-rendered pages. A callable rather than a
        # dict so a template asks for one club instead of carrying all twenty
        # into every render, and so an unknown team_code returns the neutral
        # fallback instead of raising mid-template.
        "club_colours": kits.colours_for,
        "fixture_horizon": gw_report.FIXTURE_HORIZON,
        **extra,
    }


def html_response(template, request, path, meta=None, **extra):
    response = templates.TemplateResponse(template, page_context(request, path, meta=meta, **extra))
    # The document must never be cached: it's what carries the versioned asset
    # URLs, so a stale copy pins the browser to stale CSS and JS no matter what
    # the assets themselves say.
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Landing page: the FPL ID prompt, plus what the four tools actually do.

    This used to be the app shell with the My Team tab open, which meant the
    site's highest-authority URL served almost no text - everything a reader
    (or a crawler) sees there is drawn by JavaScript after the fact. The app now
    lives at /my-team and this page explains it.

    Anyone who has already entered an ID is sent straight to /my-team by a
    script in the template, so this is a first-visit page in practice."""
    return html_response("pages/home.html", request, "/")


# One shell, four URLs. `initial_pane` is the only thing that differs: the
# template hands it to app.js, which opens that tab before first paint rather
# than flashing My Team and then switching.

def tab_response(request, path):
    """Render the app shell with the tab for `path` already open.

    The bar itself is the same `tab_links()` every other page gets; the only
    thing this adds is `initial_pane`, which is what marks one of them active
    and tells app.js which pane to open before first paint.

    All four panes are in the DOM on all four URLs, so the <h1> is rendered once
    here from the routing table rather than once per pane. Four <h1>s - three of
    them hidden - is what a crawler would otherwise be handed."""
    # The tables, server-rendered. All four panes are in the DOM on all four
    # URLs, so this goes out with every one of them rather than only with the
    # tab it belongs to - which is also what a crawler needs, since it has no
    # way to click through to a pane it can't see.
    return html_response("index.html", request, path, mode=state["mode"],
                         initial_pane=TAB_PANES[path],
                         page_h1=PAGES[path]["h1"], ssr=seo_tables())


@app.get("/my-team", response_class=HTMLResponse)
def my_team(request: Request):
    return tab_response(request, "/my-team")


@app.get("/ai-teams", response_class=HTMLResponse)
def ai_teams(request: Request):
    return tab_response(request, "/ai-teams")


@app.get("/players", response_class=HTMLResponse)
def players_page(request: Request):
    return tab_response(request, "/players")


@app.get("/fixture-rotator", response_class=HTMLResponse)
def fixture_rotator_page(request: Request):
    return tab_response(request, "/fixture-rotator")


# =========================================================================
#  Per-player pages
# =========================================================================
# One page per player in the pool. See player_pages.py for why the URL is keyed
# on the season-stable `code` and not `id`.

def _season_label():
    try:
        return seasons.current_season()
    except Exception:
        return ""


def _stats_provenance():
    """Which season the raw totals in the bootstrap actually describe.

    Before the first deadline of a new season, FPL's bootstrap still carries
    LAST season's totals. Printing those under a "2026-27 season totals" heading
    would be wrong on every player at once, for the whole of the summer - which
    is exactly when a new site is trying to get itself indexed. So the pages ask
    the season clock first and label the numbers accordingly."""
    current = _season_label()
    try:
        started = bool(gw_clock.started_gameweeks())
    except Exception:
        # Can't tell: keep the current-season label rather than asserting the
        # data is a year old. Being vague beats being confidently wrong.
        return current, True
    if started:
        return current, True
    try:
        return (seasons.previous_season() or "last season"), False
    except Exception:
        return "last season", False


@app.get("/players/a-z", response_class=HTMLResponse)
def players_index(request: Request):
    """Every player, grouped by surname initial.

    The sitemap gets the player pages crawled; this is what makes them part of
    the site rather than several hundred orphans reachable only from an XML
    file. It's also the page that a search for "fpl player list" wants."""
    pages = player_page_index()
    return html_response("pages/players_az.html", request, "/players/a-z",
                         groups=player_pages.a_to_z(pages), total=len(pages),
                         season_label=_season_label())


@app.get("/injuries", response_class=HTMLResponse)
def injuries(request: Request):
    """Who is injured, suspended or doubtful, rendered on the server.

    The data was already here - /api/news has served FPL's injury blurbs since
    early on - but only ever through JavaScript, into a panel on the My Team
    tab. That meant no crawler had ever seen a word of it, and "is <player> fit
    this week" is one of the most-asked questions in the game. A page that only
    exists after a fetch() cannot answer a search.

    Built from player_page_index() rather than from a fresh sweep of the rated
    pool, for two reasons: the index already carries `status`, `news` and
    `chance_of_playing_next_round` for every player, and it carries the URL of
    that player's own page - so every row here is also an internal link into
    /player/*, which until now had exactly one inbound link each (from the A-Z).

    No `available` flag and no empty-state special case: a week with nothing
    flagged is a real answer to the question this page asks, and the template
    says so in prose."""
    rows = player_pages.injury_list(player_page_index())
    return html_response("pages/injuries.html", request, "/injuries",
                         rows=rows, season_label=_season_label(),
                         updated=data_lastmod())


@app.get("/player/{slug}", response_class=HTMLResponse)
def player_profile(request: Request, slug: str):
    """One player's page.

    The trailing number in the slug is the identifier; the words in front of it
    are for readers and for the keywords they put in the URL. A request whose
    words don't match - an old spelling, a truncated share, a guess - is
    redirected to the canonical form rather than served, so the same page can't
    accumulate rankings under several addresses."""
    m = re.search(r"(\d+)$", slug)
    if not m:
        raise HTTPException(status_code=404, detail="No such player.")
    rec = player_page_index().get(int(m.group(1)))
    if rec is None:
        raise HTTPException(status_code=404, detail="No such player.")
    if slug != rec["slug"]:
        return RedirectResponse(rec["path"], status_code=301)

    season = _season_label()
    stats_season, started = _stats_provenance()
    return html_response(
        "pages/player.html", request, rec["path"],
        meta=player_pages.meta_for(rec, season, stats_season, started),
        player=rec, season_label=season,
        stats_season=stats_season, season_started=started,
        paragraphs=player_pages.describe(rec, season, stats_season, started),
        horizon=player_pages.horizon_points(rec),
        fixture_label=player_pages.fixture_label)


# The content pages exist for three separate reasons, all of which happen to
# want the same thing: AdSense treats About/Privacy/Contact as non-negotiable,
# UK GDPR requires a published privacy notice and a contact point for a site
# storing personal data, and they're the only prose on an otherwise
# entirely numeric site - which is what a search engine has to rank.

@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return html_response("pages/about.html", request, "/about")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return html_response("pages/privacy.html", request, "/privacy",
                         retention_months=db.RETENTION_MONTHS)


@app.get("/faq", response_class=HTMLResponse)
def faq(request: Request):
    return html_response("pages/faq.html", request, "/faq", faq_items=FAQ_ITEMS)


# =========================================================================
#  Gameweek reports
# =========================================================================
# The one part of the site that accumulates. Every other page is a view of
# today's data and is worth exactly one URL forever; these are dated editions,
# so by the end of a season there are 38 of them, each answering a question
# ("FPL GW17 differentials") that a single rotating page can never rank for.
#
# Written by `jobs.py gameweek-report` into SQLite, frozen at the deadline, and
# only read here. Nothing in the request path computes a report - a page that
# built itself on demand would produce a different edition per visitor and
# would put the whole rating pipeline behind a URL a crawler can hit.

def _gw_roundup_meta(gw, roundup):
    """Per-roundup title and description.

    Built from the round's own contents for the same reason the briefing's are:
    38 pages differing only by a number is how a site gets its own pages
    classed as near-duplicates of each other. Naming who actually top scored
    makes each one about something.

    Deliberately different wording from the briefing for the same gameweek -
    "roundup" against "briefing", results against predictions - because the two
    pages sit at adjacent URLs and would otherwise compete with each other for
    the same query."""
    title = f"FPL Gameweek {gw} roundup — top scorers and results | {SITE_NAME}"
    top = [p["name"] for p in roundup.get("top_scorers", [])][:3]
    if top:
        desc = (f"Fantasy Premier League Gameweek {gw} results: {', '.join(top)} "
                "top scored, plus the widely-owned players who blanked, the "
                "results against the table and who is on a run.")
    else:
        desc = (f"The Fantasy Premier League Gameweek {gw} roundup: top scorers, "
                "the blanks that hurt, results against the table and clubs "
                "finding form.")
    return {"title": title, "description": desc[:160]}


def _gw_report_meta(gw, report):
    """Per-edition title and description.

    Built from the edition's own contents rather than a template string,
    because 38 pages differing only by a number is the textbook way to get a
    site's own pages classed as near-duplicates of each other. Naming the
    actual players makes each one about something."""
    title = f"FPL Gameweek {gw} — form, differentials and fixtures | {SITE_NAME}"
    names = [p["name"] for p in report.get("in_form", [])][:3]
    if names:
        desc = (f"Fantasy Premier League Gameweek {gw}: {', '.join(names)} lead on form, "
                "plus the best differentials, the kindest fixture runs and the "
                "fitness flags on widely-owned players.")
    else:
        desc = (f"The Fantasy Premier League Gameweek {gw} briefing: form picks, "
                "differentials, fixture swings and team news, from a trained "
                "points model.")
    return {"title": title, "description": desc[:160]}


@app.get("/gameweek", response_class=HTMLResponse)
def gameweek_hub(request: Request):
    """This week: the upcoming briefing and the last completed roundup.

    This used to list every edition ever published, newest first, which was the
    right page when there were four of them and the wrong one by about November
    - a reader arriving wants this week, and was given a wall of dated links
    with the useful one first and thirty dead ones under it.

    The old editions are NOT gone. Every /gameweek/<n> URL still resolves and
    stays in the sitemap; they are simply no longer listed here. That
    distinction is the whole decision: those pages are the site's only
    accumulating content and are already indexed, so de-listing them costs
    nothing and 404ing them would throw away every ranking they have earned.

    The briefing shown is the highest-numbered one, which is the upcoming
    gameweek's by construction - `build_gameweek_report` always writes for
    `next_gameweek`. The roundup is whichever is newest, which is not always
    the last round to finish: one that ended while the box was down has no
    roundup, and showing the last one that exists beats showing a gap.
    """
    index = db.gw_report_index()
    brief = db.get_gw_report(index[0]["gameweek"]) if index else None
    roundup = db.latest_gw_roundup()
    return html_response("pages/gameweek_index.html", request, "/gameweek",
                         brief=brief, roundup=roundup)


@app.get("/gameweek/feed.xml")
def gameweek_feed():
    """RSS for the editions.

    Declared before /gameweek/{n} because FastAPI matches in declaration order
    and "feed.xml" would otherwise be tried as a gameweek number.

    The safe half of the distribution plan: aggregators and some crawlers poll
    a feed, and unlike an automated post it can't get an account banned."""
    def _rfc822(stamp):
        """RSS wants RFC 822 dates, not the ISO strings stored."""
        try:
            return datetime.fromisoformat(stamp).strftime("%a, %d %b %Y %H:%M:%S +0000")
        except (TypeError, ValueError):
            return ""

    # Briefings and roundups in ONE feed, interleaved by publication time,
    # rather than a second feed for the roundups. A subscriber wants everything
    # this site publishes; two feeds would mean choosing, and the great majority
    # of people who chose would pick one and never learn the other existed.
    entries = []
    for ed in db.gw_report_index()[:20]:
        rec = db.get_gw_report(ed["gameweek"])
        if rec:
            entries.append((ed.get("frozen_at") or ed["generated_at"],
                            f"Gameweek {ed['gameweek']} briefing",
                            f"/gameweek/{ed['gameweek']}",
                            rec["payload"].get("summary", "")))
    for ed in db.gw_roundup_index()[:20]:
        rec = db.get_gw_roundup(ed["gameweek"])
        if rec:
            entries.append((ed["generated_at"],
                            f"Gameweek {ed['gameweek']} roundup",
                            f"/gameweek/{ed['gameweek']}/roundup",
                            rec["payload"].get("summary", "")))

    # Newest first. A missing timestamp sorts last rather than crashing the
    # feed - an item with no date is still worth serving.
    entries.sort(key=lambda e: e[0] or "", reverse=True)

    items = []
    for stamp, title, path, summary in entries[:30]:
        pub = _rfc822(stamp)
        items.append(
            "<item>"
            f"<title>{xml_escape(title)}</title>"
            f"<link>{SITE_URL}{path}</link>"
            f"<guid isPermaLink=\"true\">{SITE_URL}{path}</guid>"
            f"<description>{xml_escape(summary)}</description>"
            + (f"<pubDate>{pub}</pubDate>" if pub else "")
            + "</item>")

    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<rss version="2.0"><channel>'
           f"<title>{xml_escape(SITE_NAME)} — gameweek briefings and roundups</title>"
           f"<link>{SITE_URL}/gameweek</link>"
           "<description>Before each deadline: form, differentials, fixture "
           "swings and team news. After each gameweek: who scored, who blanked "
           "and what went against the table.</description>"
           "<language>en-gb</language>"
           f"{''.join(items)}"
           "</channel></rss>")
    return Response(content=xml, media_type="application/rss+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/gameweek/{gw}", response_class=HTMLResponse)
def gameweek_report_page(request: Request, gw: int):
    """One edition.

    404s rather than generating on demand when an edition doesn't exist: a
    gameweek with no published page is a gameweek nobody wrote about, and
    inventing one at request time would date it wrong."""
    rec = db.get_gw_report(gw)
    if not rec:
        raise HTTPException(status_code=404, detail="No report for that gameweek")
    report = rec["payload"]
    return html_response(
        "pages/gameweek.html", request, f"/gameweek/{gw}",
        meta=_gw_report_meta(gw, report),
        report=report, frozen=bool(rec["frozen"]),
        generated_at=rec["generated_at"], frozen_at=rec.get("frozen_at"),
        # A briefing is published about a week before its own roundup can
        # exist, so the cross-link at the foot of the page is conditional.
        has_roundup=db.get_gw_roundup(gw) is not None)


@app.get("/gameweek/{gw}/roundup", response_class=HTMLResponse)
def gameweek_roundup_page(request: Request, gw: int):
    """One roundup: what actually happened in a settled gameweek.

    404s when there isn't one, same as the briefing. A round with no roundup is
    a round nobody wrote up - most often because it hasn't finished yet - and
    generating one at request time would either report provisional bonus points
    as final or put the whole pipeline behind a URL a crawler can hit."""
    rec = db.get_gw_roundup(gw)
    if not rec:
        raise HTTPException(status_code=404, detail="No roundup for that gameweek")
    roundup = rec["payload"]
    return html_response(
        "pages/gameweek_roundup.html", request, f"/gameweek/{gw}/roundup",
        meta=_gw_roundup_meta(gw, roundup),
        roundup=roundup, generated_at=rec["generated_at"])


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return html_response("pages/contact.html", request, "/contact")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    """Point crawlers at the sitemap and keep them out of the JSON API.

    The /api/* routes return data with no prose, so indexing them wastes crawl
    budget on pages that can never rank and puts machine-readable endpoints in
    search results."""
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n",
        headers={"Cache-Control": "public, max-age=86400"})


# Declared conditionally because the path is the key itself. Unset, the route
# simply doesn't exist and /<anything>.txt 404s as before - no catch-all route
# that has to guess whether a request is an IndexNow probe or a typo.
if INDEXNOW_KEY:
    @app.get(f"/{INDEXNOW_KEY}.txt", response_class=PlainTextResponse)
    def indexnow_key():
        """Proof of ownership for IndexNow: the key, served back as plain text.

        The body must be the key and nothing else - no trailing newline is
        required, and a search engine comparing byte-for-byte will reject a file
        with extra content."""
        return PlainTextResponse(
            INDEXNOW_KEY, headers={"Cache-Control": "public, max-age=86400"})


def data_lastmod(default=None):
    """The date the player data behind most of this site last changed.

    The mtime of the CSV the nightly bootstrap pull rewrites (seasons.players_path()),
    which is where prices, form, ownership and injury news actually arrive. It
    lives on the mounted volume, so unlike anything derived from process start
    or from a file in the image, a redeploy does not move it.

    Falls back rather than raising: a sitemap with a slightly wrong date is
    worth far more than no sitemap at all."""
    try:
        mtime = os.path.getmtime(seasons.players_path())
    except OSError:
        return default
    return datetime.fromtimestamp(mtime, timezone.utc).date().isoformat()


@app.get("/sitemap.xml")
def sitemap():
    """Every indexable URL: the landing page, the four tool pages and the prose
    pages.

    Every `lastmod` here is meant to be TRUE, which is a change from how this
    started. It used to stamp today on all ~580 URLs, every day, and that is
    the one thing Google documents as a reason to ignore <lastmod> for an
    entire sitemap - at which point the file stops being able to say which of
    568 player pages is worth a recrawl, which is the only reason it has dates
    in it. The prose pages were the clearest case: /privacy claiming a fresh
    edit every morning is plainly false, and it was competing for crawl budget
    against pages that had genuinely changed.

    So there are now three honest sources, in the order they appear below:
      * the nightly data pull's own mtime, for anything rebuilt from it,
      * a hand-maintained date in PAGES, for the prose,
      * the frozen/generated timestamps already recorded per gameweek edition.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    # Anything driven by the pipeline shares one date, because it is one pull
    # that rewrites all of them together. `today` only if that file can't be
    # read at all.
    data_mod = data_lastmod(today)
    urls = "".join(
        f"<url><loc>{SITE_URL}{path}</loc>"
        f"<lastmod>{meta.get('lastmod') or data_mod}</lastmod>"
        f"<changefreq>{meta.get('changefreq', 'monthly')}</changefreq>"
        f"<priority>{meta.get('priority', '0.5')}</priority></url>"
        for path, meta in PAGES.items())
    # Plus one entry per player. Several hundred URLs, well inside the 50,000
    # limit for a single sitemap, so this doesn't need splitting into an index.
    # `xml_escape` because a name can legitimately contain an ampersand.
    #
    # `daily` rather than `weekly`: these pages are rebuilt by the same nightly
    # job that sets data_mod, so weekly understated it in the one direction that
    # matters - telling a crawler to come back less often than the numbers move.
    urls += "".join(
        f"<url><loc>{SITE_URL}{xml_escape(rec['path'])}</loc><lastmod>{data_mod}</lastmod>"
        f"<changefreq>daily</changefreq><priority>0.6</priority></url>"
        for rec in player_page_index().values())

    # Plus one per published gameweek edition. These are the only URLs on the
    # site with an honest `lastmod`: a frozen edition genuinely has not changed
    # since the deadline, and saying so is what stops Google recrawling 38 dead
    # pages every day looking for an edit that will never come. `changefreq`
    # follows the same logic - `never` is a real value in the spec and this is
    # what it's for.
    for ed in db.gw_report_index():
        lastmod = (ed.get("frozen_at") or ed["generated_at"] or "")[:10] or today
        freq = "never" if ed["frozen"] else "daily"
        urls += (f"<url><loc>{SITE_URL}/gameweek/{ed['gameweek']}</loc>"
                 f"<lastmod>{lastmod}</lastmod><changefreq>{freq}</changefreq>"
                 f"<priority>0.7</priority></url>")

    # And one per roundup. `never` unconditionally: unlike a briefing, a roundup
    # has no live phase at all - it is written from settled results and is final
    # the moment it exists, so there has never been a version of it to recrawl
    # for.
    for ed in db.gw_roundup_index():
        lastmod = (ed["generated_at"] or "")[:10] or today
        urls += (f"<url><loc>{SITE_URL}/gameweek/{ed['gameweek']}/roundup</loc>"
                 f"<lastmod>{lastmod}</lastmod><changefreq>never</changefreq>"
                 f"<priority>0.7</priority></url>")

    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return Response(content=xml, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    """A map of the site for language models, in Markdown.

    llms.txt is a proposed convention, not a standard, and no model provider
    has committed to reading it. It's here because it costs one route: the
    downside if it's ignored is a file nobody fetches, and the upside if it
    isn't is that a model answering "what is FPL Buddy" gets the answer from
    the site rather than inferring it from whatever page it happened to crawl.

    Deliberately prose rather than a link dump. The tool pages now serve their
    tables in the HTML as well as building them in JavaScript (see
    seo_tables.py), so a model fetching /players gets the top hundred rated
    players rather than an empty <tbody> - but what it gets is a snapshot of one
    view, and prose is still the better way to say what the page is FOR and how
    to read the numbers on it."""
    return PlainTextResponse(
        f"# {SITE_NAME}\n"
        "\n"
        "> Free Fantasy Premier League tools: every player rated by a pair of\n"
        "> gradient-boosted models trained on per-gameweek data, two AI-picked\n"
        "> squads a week, a fixture rotation planner, and your own squad analysed\n"
        "> from your FPL ID. Unofficial fan project, not affiliated with the\n"
        "> Premier League.\n"
        "\n"
        "Data comes from the public Fantasy Premier League API and from ClubElo\n"
        "for team strength. Projections use two models per position rather than\n"
        "one: a classifier for whether a player starts, and a Poisson regressor\n"
        "for what he scores when he does, fitted only on appearances. Just over\n"
        "half of all gameweek rows are players who did not play, and a single\n"
        "model fitted across that mixture learns who plays rather than who\n"
        "scores. Features are FPL's own per-gameweek columns over three- and\n"
        "six-gameweek windows plus a season-to-date rate, with home or away and\n"
        "opponent strength, all shifted back a gameweek so no row sees its own\n"
        "result. Predictions are frozen at each deadline and never recalculated,\n"
        "so the published track record is unedited.\n"
        "\n"
        "## Pages\n"
        "\n"
        f"- [Home]({SITE_URL}/): what the site does and how to find your FPL ID.\n"
        f"- [Player ratings]({SITE_URL}/players): every player with predicted\n"
        "  points for the next eight gameweeks, price, form, ownership, and the\n"
        "  players whose returns are behind their underlying numbers.\n"
        f"- [AI teams]({SITE_URL}/ai-teams): two squads picked by integer linear\n"
        "  programming. AI Best XI is the highest-scoring legal squad for the\n"
        "  coming gameweek, rebuilt weekly. AI Manager keeps one squad across the\n"
        "  season with a real bank balance, free transfers, hits and chips.\n"
        f"- [Fixture rotator]({SITE_URL}/fixture-rotator): pairs of clubs whose\n"
        "  fixtures alternate, so one of the two always has a good game.\n"
        f"- [My team]({SITE_URL}/my-team): enter an FPL ID for predicted points,\n"
        "  an optimised XI, transfer suggestions and live scoring.\n"
        f"- [Injury news]({SITE_URL}/injuries): every flagged player - injured,\n"
        "  suspended, doubtful - with FPL's own chance-of-playing percentage where\n"
        "  it publishes one, and no guessed return dates where it doesn't.\n"
        f"- [Players A-Z]({SITE_URL}/players/a-z): index of every player page.\n"
        f"- [This gameweek]({SITE_URL}/gameweek): the current briefing and the\n"
        "  last completed roundup. A briefing is written before a deadline and\n"
        f"  frozen at it, at {SITE_URL}/gameweek/<n> — captaincy shortlist, form,\n"
        "  differentials, fixture swings and fitness flags. A roundup is written\n"
        f"  after the round is settled, at {SITE_URL}/gameweek/<n>/roundup — top\n"
        "  scorers, widely-owned players who blanked, results against the league\n"
        "  table, clubs on a run, and the AI Best XI's projection for that round\n"
        "  against what it actually scored.\n"
        f"- [About]({SITE_URL}/about): how the model works and what it can't do.\n"
        f"- [FAQ]({SITE_URL}/faq): common questions about the ratings and squads.\n"
        "\n"
        "## For per-player questions\n"
        "\n"
        f"Each player has a page at {SITE_URL}/player/<name-slug> carrying price,\n"
        "ownership, form, expected goals and assists, availability and projected\n"
        f"points. Every one of them is linked from {SITE_URL}/players/a-z.\n"
        "\n"
        "## Reading without JavaScript\n"
        "\n"
        "Every page listed above serves its content in the HTML. The tool pages\n"
        f"additionally build interactive versions in the browser: {SITE_URL}/players\n"
        "carries the hundred highest-rated players with form, price and the next\n"
        f"three fixtures; {SITE_URL}/ai-teams carries the current AI squad and the\n"
        f"predicted-versus-actual record of every past one; {SITE_URL}/fixture-rotator\n"
        "carries all twenty clubs' fixture difficulty over the coming gameweeks.\n"
        "The /api/ endpoints behind those tables are disallowed in robots.txt and\n"
        "should not be fetched; the pages themselves carry the same numbers.\n"
        "\n"
        "## Notes\n"
        "\n"
        "- Projections compare players against each other. Read as a forecast of\n"
        "  any individual score they will be wrong most weeks.\n"
        "- A player with no chance of playing is projected at zero for the rounds\n"
        "  he is out for, not at what he would have scored fit.\n"
        "- The site is read-only. It cannot change anyone's team, and never asks\n"
        "  for a Fantasy Premier League password.\n"
        f"- Corrections: {SITE_URL}/contact\n",
        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
def security_txt():
    """RFC 9116. Gives anyone who finds a vulnerability somewhere to send it
    other than a public issue tracker or your personal inbox."""
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return PlainTextResponse(
        f"Contact: mailto:{EMAILS['security']}\n"
        f"Expires: {expires}\n"
        "Preferred-Languages: en\n"
        f"Canonical: {SITE_URL}/.well-known/security.txt\n",
        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/search")
def search(q: str):
    if not q or state["position_dfs"] is None:
        return {"results": []}
    result = search_player(q, state["position_dfs"])
    if result is None:
        return {"results": []}
    return {"results": records(result)}


# position_dfs is keyed on the long names the rating model uses. Every other
# layer of the app - the pane markup, the player records, get_all_players -
# says GK/DEF/MID/FWD, so a caller reaching for the obvious short code got an
# empty list back rather than an error. Accept both spellings.
POSITION_ALIASES = {
    "GK": "Goalkeeper", "GKP": "Goalkeeper", "DEF": "Defender",
    "MID": "Midfielder", "FWD": "Forward", "FOR": "Forward",
}


@app.get("/api/ratings")
def ratings(response: Response, position: str = "All", top_n: int = 20):
    response.headers["Cache-Control"] = API_CACHE
    position_dfs = state["position_dfs"]
    if position_dfs is None:
        return {"results": []}

    # head(-5) means "all but the last five", which is not what a negative
    # top_n is asking for. Clamp instead, so an out-of-range page size returns
    # nothing rather than almost everything.
    top_n = max(0, top_n)

    if position == "All":
        combined = pd.concat([
            df.assign(position=pos) for pos, df in position_dfs.items()
        ])
    else:
        position = POSITION_ALIASES.get(position.strip().upper(), position)
        if position not in position_dfs:
            return {"results": []}
        combined = position_dfs[position].assign(position=position)

    cols = [c for c in ["web_name", "first_name", "second_name", "team_code",
                        "position", "rating", "predicted_points", "next_gameweeks"] if c in combined.columns]
    top = combined.sort_values("rating", ascending=False).head(top_n)
    return {"results": records(top[cols])}


@app.get("/api/rotation")
def rotation(response: Response, category: str = "defender", n_gameweeks: int = 8,
             exclude_top_n: int = 4):
    response.headers["Cache-Control"] = API_CACHE
    rotation_df = state["rotation_df"]
    if rotation_df is None:
        return {"gameweeks": [], "teams": [], "pairs": []}

    difficulty_col = "defensive_difficulty" if category == "defender" else "attacking_difficulty"

    gameweeks = sorted(int(e) for e in rotation_df["event"].dropna().unique())
    fixture_map = team_fixture_map(rotation_df, difficulty_col)
    averages = rotation_df.groupby("team_name")[difficulty_col].mean().sort_values()

    def clean_fixtures(fixtures):
        return {
            str(gw): {"opponent": f["opponent"], "difficulty": float(f["difficulty"])}
            for gw, f in fixtures.items()
        }

    # short_name -> team_code, so the front end can render shirt images.
    team_code_map = {}
    if "team_code" in rotation_df.columns:
        team_code_map = {
            name: (int(code) if pd.notna(code) else None)
            for name, code in rotation_df.groupby("team_name")["team_code"].first().items()
        }

    teams = [
        {"team_name": team, "team_code": team_code_map.get(team),
         "average": round(float(avg), 1), "fixtures": clean_fixtures(fixture_map.get(team, {}))}
        for team, avg in averages.items()
    ]

    # Ranked rotation pairs: auto-starters (Man City/Arsenal etc) excluded, then
    # scored on never-stuck coverage + genuine week-on-week alternation, so the
    # cheap mid-table pairs a manager actually rotates rise to the top.
    raw_pairs = rank_rotation_pairs(rotation_df, difficulty_col, exclude_top_n=exclude_top_n)
    player_recs = recommend_pair_players(
        raw_pairs, state["position_dfs"], rotation_df, category=category
    ) if state["position_dfs"] is not None else []
    recs_by_pair = {(r["team_a"], r["team_b"]): r for r in player_recs}

    pairs = []
    for _, row in raw_pairs.iterrows():
        rec = recs_by_pair.get((row["team_a"], row["team_b"]), {})
        pairs.append({
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "team_a_code": team_code_map.get(row["team_a"]),
            "team_b_code": team_code_map.get(row["team_b"]),
            "worst_week": round(float(row["worst_week"]), 1),
            "avg_difficulty": round(float(row["avg_difficulty"]), 1),
            "improvement": round(float(row["improvement"]), 1),
            "team_a_fixtures": clean_fixtures(fixture_map.get(row["team_a"], {})),
            "team_b_fixtures": clean_fixtures(fixture_map.get(row["team_b"], {})),
            "position_pairs": rec.get("position_pairs", []),
        })

    return {"gameweeks": gameweeks, "teams": teams, "pairs": pairs}


@app.get("/api/team")
def team(team_id: int, event: int = None):
    """Full Team-tab payload for a manager id (optionally a specific gameweek).
    Read-only: recommendations are returned, but nothing is written back to FPL."""
    view = get_team_view(team_id, event, state["position_dfs"])
    # The gameweek being picked for. Before the season starts there's no
    # current_event at all, so without this the header has nothing to show but
    # the word "Preseason" - which doesn't tell you WHICH gameweek you're
    # picking for.
    try:
        view["next_event"] = gw_clock.next_gameweek()
    except Exception:
        view["next_event"] = None
    # Remember ids that resolve to a real manager, so the snapshot job has a
    # finite list to walk instead of all ~11M FPL entries. Never fatal.
    if view.get("header"):
        try:
            db.record_known_manager(team_id)
        except Exception as e:
            print(f"couldn't record known manager {team_id}: {e}")
    return view


@app.get("/api/news")
def news(response: Response, limit: int = 20):
    """Latest injury/transfer news snippets for the My Team news feed.

    Explicitly uncacheable: the payload is re-fetched from FPL on every call,
    so letting a browser (or any intermediary) reuse an old response is the
    one thing that would make the feed look stale."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return get_news_feed(limit=limit)


@app.get("/api/league/{league_id}")
def league(league_id: int, page: int = 1):
    """Top of a classic league's standings table."""
    return get_league_standings(league_id, page)


@app.get("/api/all_players")
def all_players(response: Response):
    """Full rated player pool - powers search/transfers and building a squad
    from empty pitch slots."""
    response.headers["Cache-Control"] = API_CACHE
    data = get_all_players(state["position_dfs"])
    # The canonical page URL comes from here rather than being assembled in the
    # browser: the slug is an accent-folded version of the full name, and the
    # server is the only place that knows the whole name. Building it twice in
    # two languages is how you end up with links that 301 on every click.
    try:
        paths = {code: rec["path"] for code, rec in player_page_index().items()}
        for p in data["players"]:
            p["path"] = paths.get(p.get("code"))
    except Exception as e:
        print(f"couldn't attach player page paths: {e}")
    return data


@app.get("/api/underperforming")
def underperforming(response: Response, top_n: int = 20):
    """Players whose actual returns lag their underlying xG/xGC numbers."""
    response.headers["Cache-Control"] = API_CACHE
    return get_underperforming_players(state["position_dfs"], top_n=top_n)


@app.get("/api/overperforming")
def overperforming(response: Response, top_n: int = 20):
    """The mirror: returns running AHEAD of the underlying numbers.

    Its own route rather than a `direction` parameter on the one above, because
    the two are cached separately by the CDN and because a typo'd parameter
    would otherwise silently serve the opposite table - a wrong answer that
    looks exactly like a right one."""
    response.headers["Cache-Control"] = API_CACHE
    return get_overperforming_players(state["position_dfs"], top_n=top_n)


@app.get("/api/player/{player_id}")
def player(player_id: int):
    """Recent gameweek performance for a single player (player pop-up)."""
    return get_player_summary(player_id)


@app.get("/api/live/{gameweek}")
def live_scores(gameweek: int, response: Response = None):
    """Per-player points for a gameweek that's underway.

    One call covers every player, so the front end can light up a whole pitch
    from a single request and poll it cheaply while matches are on.

    `provisional` is the important field: FPL's bonus points aren't settled
    until `data_checked` flips, so totals shown before then WILL move. The UI
    labels them rather than presenting a mid-match number as final."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, max-age=0"
    events = gw_clock.get_events()
    started = gw_clock.started_gameweeks(events)
    if not started or gameweek > started[-1]:
        return {"available": False, "gameweek": gameweek,
                "detail": "That gameweek hasn't started yet."}
    points = gw_clock.get_event_live(gameweek)
    if not points:
        return {"available": False, "gameweek": gameweek,
                "detail": "Live data isn't available for that gameweek."}
    settled = gw_clock.gameweek_is_finished(gameweek, events)
    in_progress = gameweek == gw_clock.current_gameweek(events) and not settled
    return {"available": True, "gameweek": gameweek, "points": points,
            "provisional": not settled, "in_progress": in_progress}


# =========================================================================
#  Saved team (draft)
# =========================================================================
# Keyed on FPL id only - the same team follows a manager between devices.
# Deliberately unauthenticated, matching how the rest of the app treats an FPL
# id as the whole identity: anyone who knows the id can read or overwrite that
# draft. Fine while a draft is a scratchpad over already-public picks; revisit
# if it ever holds something private.

@app.get("/api/draft/{fpl_id}")
def get_draft(fpl_id: int):
    try:
        draft = drafts.get_draft(fpl_id, _player_pool())
    except Exception as e:
        return {"available": False, "detail": str(e)}
    if draft is None:
        return {"available": False, "detail": "No saved team for this FPL ID."}
    return {"available": True, **draft}


@app.post("/api/draft/{fpl_id}")
def save_draft(fpl_id: int, payload: dict = Body(...)):
    """Save the working squad. Replaces whatever was stored for this id.

    Unauthenticated, deliberately - see the module docstring in drafts.py for
    why the per-device token that used to gate this was removed. The practical
    consequence to remember when reading this: the last writer wins, and
    anyone can be the last writer.
    """
    # Structure is checked before anything is written. It is now the only check
    # between an arbitrary POST body and a stored row.
    try:
        drafts.validate_picks(payload.get("picks") or [])
    except drafts.DraftError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        result = drafts.save_draft(
            fpl_id, payload.get("picks") or [],
            gameweek=payload.get("gameweek") or gw_clock.next_gameweek(),
            bank=payload.get("bank"))
    except drafts.DraftError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    try:
        db.record_known_manager(fpl_id)
    except Exception:
        pass
    return result


@app.delete("/api/draft/{fpl_id}")
def clear_draft(fpl_id: int):
    """Delete the draft for this id.

    Ungated like the save above. Worth being clear-eyed that this is the call
    that destroys data rather than replacing it - but gating only this one
    would have meant the delete token stranded on a single device, which is the
    problem the whole change set out to remove."""
    return drafts.delete_draft(fpl_id)


# =========================================================================
#  AI Manager
# =========================================================================

@app.get("/api/ai/manager")
def ai_manager_gameweek(gameweek: int = None):
    """The bot's squad for a gameweek, with the transfers and chip it chose.

    Stored weeks are served as recorded. The upcoming gameweek is simulated
    live (not persisted) so you can see what it intends before the deadline -
    the watcher is what commits a decision."""
    target = gameweek or gw_clock.next_gameweek() or gw_clock.current_gameweek()
    if target is None:
        return {"available": False, "detail": "No gameweek found."}
    pool = _player_pool()
    # Same reasoning as the Best XI endpoint: the upcoming gameweek's plan is
    # simulated from the in-memory pool, so a database fault shouldn't blank
    # the tab. Record the fault and carry on.
    stored, db_error = None, None
    try:
        stored = ai_manager.get_gameweek(target, pool)
    except Exception as e:
        db_error = str(e)
    if stored:
        return {"available": True, "stored": True, **stored}

    started = gw_clock.started_gameweeks()
    if started and target <= started[-1]:
        return {"available": False, "gameweek": target,
                "detail": (f"Couldn't read stored gameweeks ({db_error})." if db_error
                           else f"The AI Manager wasn't running for GW{target}.")}
    if not pool:
        return {"available": False, "detail": "Ratings not loaded yet."}
    try:
        preview = cached_manager_preview(target)
    except OptimisationError as e:
        return {"available": False, "gameweek": target, "detail": str(e)}
    except Exception as e:
        return {"available": False, "gameweek": target,
                "detail": f"Couldn't simulate GW{target}: {e}"}
    return {"available": True, "stored": False, "db_error": db_error, **preview}


@app.get("/api/ai/manager/history")
def ai_manager_history():
    try:
        return {"available": True, "history": ai_manager.history()}
    except Exception as e:
        return {"available": False, "detail": str(e), "history": []}


# =========================================================================
#  AI Best XI
# =========================================================================

@app.get("/api/ai/best_xi")
def ai_best_xi(gameweek: int = None, budget: float = DEFAULT_BUDGET):
    """The AI's optimum squad for a gameweek.

    Prefers the FROZEN snapshot taken at that gameweek's deadline. Falls back to
    solving live only for a gameweek that hasn't been snapshotted yet - i.e. the
    upcoming one, which is legitimately still changing as prices and predictions
    move. A past gameweek with no snapshot returns unavailable rather than a
    fabricated squad built from today's numbers."""
    target = gameweek or gw_clock.next_gameweek()
    if target is None:
        return {"available": False, "detail": "No upcoming gameweek found."}

    pool = _player_pool()
    # A database problem must not hide the upcoming gameweek's squad: that one
    # is solved from the in-memory pool and needs no stored data at all. Note
    # the fault, then carry on to the live path below.
    stored, db_error = None, None
    try:
        stored = ai_team.get_snapshot(target, pool)
    except Exception as e:
        db_error = str(e)
    if stored:
        return {"available": True, **stored}

    started = gw_clock.started_gameweeks()
    if started and target <= started[-1]:
        return {"available": False, "gameweek": target, "stored": False,
                "detail": (f"Couldn't read stored snapshots ({db_error})." if db_error
                           else f"GW{target} was never snapshotted - it started "
                                f"before this feature was running.")}

    if not pool:
        return {"available": False, "detail": "Ratings not loaded yet."}
    try:
        result = cached_best_xi(target, budget)
    except OptimisationError as e:
        return {"available": False, "gameweek": target, "detail": str(e)}
    # Live preview: deliberately not persisted. The deadline watcher owns
    # writing snapshots, so a page view can't freeze a half-formed squad.
    return {"available": True, "stored": False, "actual_points": None,
            "db_error": db_error, **result}


# The squad was called Best XV until it was renamed, and this path went out with
# every page for a season. Kept as an alias rather than a redirect: it is read by
# fetch() from our own script, and a 301 costs that caller a second round trip
# for no benefit. Nothing here re-implements the endpoint - it calls the same
# function - so the two cannot answer differently.
@app.get("/api/ai/best_xv", include_in_schema=False)
def ai_best_xv_alias(gameweek: int = None, budget: float = DEFAULT_BUDGET):
    return ai_best_xi(gameweek=gameweek, budget=budget)


@app.get("/api/ai/history")
def ai_history():
    """Predicted vs actual for every frozen AI Best XI snapshot."""
    try:
        return {"available": True, "snapshots": ai_team.list_snapshots()}
    except Exception as e:
        return {"available": False, "detail": str(e), "snapshots": []}


@app.get("/api/ai/status")
def ai_status():
    """DB health + season clock. Makes a failed volume mount visible instead of
    silently writing snapshots into a throwaway file inside the container."""
    events = gw_clock.get_events()
    # The absolute path is the one field here worth withholding: it is the
    # deployment's directory layout, handed to anyone who asks. The filename
    # and the availability flag keep the diagnostic value - a failed volume
    # mount still shows as a different name with an empty row count.
    # Both roots are reported as a bare filename plus a storage verdict rather
    # than an absolute path. The path was the deployment's directory layout
    # handed to anyone who asked; `storage`/`persisted` answer the question the
    # path was actually being used to answer - "will this survive the next
    # deploy?" - and answer it better, because an anonymous volume looks
    # identical to a correct mount if all you have is the path.
    health = db.healthcheck()
    if health.get("path"):
        health = {**health, "path": os.path.basename(health["path"])}
    data = seasons.describe()
    if data.get("data_root"):
        storage = db.storage_kind(data["data_root"])
        data = {**data, "data_root": os.path.basename(data["data_root"]),
                "storage": storage, "persisted": storage in ("bind", "volume")}
    return {
        "db": health,
        "data": data,
        "mode": state["mode"],
        "current_gameweek": gw_clock.current_gameweek(events),
        "next_gameweek": gw_clock.next_gameweek(events),
        "processed_deadlines": db.processed_deadlines(),
        "known_managers": len(db.known_managers()),
        # Whether the cron jobs are still running. Everything else on this
        # endpoint describes the state of the data; this describes whether
        # anything is still updating it, which is the question you actually
        # have when a page looks stale. Guarded because a diagnostic endpoint
        # that can 500 is one you cannot use in the situation it is for.
        "jobs": _jobs_health(),
    }


def _jobs_health():
    try:
        return ops.health()
    except Exception as e:
        return {"available": False, "detail": str(e)}


@app.get("/api/manager/{fpl_id}/history")
def manager_gw_history(fpl_id: int):
    """Captured gameweek history for one manager, with the AI's numbers for the
    same gameweeks alongside."""
    try:
        return {"available": True, "history": manager_history.manager_history(fpl_id)}
    except Exception as e:
        return {"available": False, "detail": str(e), "history": []}


@app.post("/api/mode")
def set_mode(mode: str, x_refresh_token: str = Header(default="")):
    """Manual override for the auto-detected mode. Not used by the UI (the mode
    follows the first gameweek deadline now) - kept for testing and for forcing
    preseason ratings back on if inseason data turns out to be unusable.

    Behind the same token as /api/refresh. This calls load_data() too, so an
    open version of it is the same minute of pipeline CPU per request - and it
    would have made the token on /api/refresh pointless, since an attacker
    could just call this instead. It also silently reshapes what every other
    endpoint returns, which is not something an anonymous caller should do."""
    require_refresh_token(x_refresh_token)
    if mode not in ("preseason", "inseason"):
        return {"status": "error", "detail": "mode must be 'preseason' or 'inseason'"}
    try:
        # Serialised for the same reason /api/refresh is: this is a second full
        # pipeline run, and it must not race the startup warm-up.
        load_data_serialised(mode)
        return {"status": "ok", "mode": state["mode"]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _drafts_or_404(gw=None):
    """The drafts for `gw`, or for the most recent gameweek that has any.

    Defaulting to the latest is what makes one bookmark useful all season: the
    alternative is a URL with a gameweek number in it that has to be edited
    every week, which is exactly the friction that stops the posting routine
    happening at all."""
    if gw is None:
        available = social.list_drafts()
        if not available:
            raise HTTPException(status_code=404,
                                detail="No drafts yet - has the report been built?")
        gw = available[0]
    text = social.read_drafts(gw)
    if text is None:
        raise HTTPException(
            status_code=404,
            detail=f"No drafts for GW{gw} - has the report been built?")
    return _noindex(text)


@app.get("/api/social/{gw}", response_class=PlainTextResponse)
def social_drafts(gw: int, x_refresh_token: str = Header(default="")):
    """The draft post text for a gameweek. Needs a header, so: curl, not a
    phone. See the keyed route below for the phone case."""
    require_refresh_token(x_refresh_token)
    return _drafts_or_404(gw)


# Declared conditionally, like the IndexNow key route: unset, the path simply
# doesn't exist rather than existing and refusing. See FPL_SOCIAL_KEY above for
# why this is a capability URL and what that costs.
def _noindex(text):
    """Belt and braces alongside the Disallow on /api/: robots.txt asks a
    crawler not to fetch, this tells anything that fetched anyway not to
    index."""
    return PlainTextResponse(text, headers={"X-Robots-Tag": "noindex, nofollow"})


def _roundup_drafts_or_404(gw=None):
    """The roundup drafts for `gw`, or for the most recent gameweek with any."""
    if gw is None:
        available = social.list_roundup_drafts()
        if not available:
            raise HTTPException(
                status_code=404,
                detail="No roundup drafts yet - has a gameweek finished?")
        gw = available[0]
    text = social.read_roundup_drafts(gw)
    if text is None:
        raise HTTPException(status_code=404,
                            detail=f"No roundup drafts for GW{gw}")
    return _noindex(text)


def _player_drafts_or_404(day=None):
    """One night's player write-up drafts, or the newest.

    Defaulting to the newest is the whole point: this is read on a phone every
    morning, and a URL with a date in it would have to be edited daily, which
    is exactly the friction that stops the routine happening."""
    if day is None:
        available = social.list_player_drafts()
        if not available:
            raise HTTPException(
                status_code=404,
                detail="No player write-ups yet - has the nightly job run?")
        day = available[0]
    text = social.read_player_drafts(day)
    if text is None:
        raise HTTPException(status_code=404,
                            detail=f"No player write-up for {day}")
    return _noindex(text)


if SOCIAL_KEY:
    @app.get(f"/api/social/{SOCIAL_KEY}/latest", response_class=PlainTextResponse)
    def social_drafts_latest():
        """The newest gameweek's drafts. This is the one to bookmark."""
        return _drafts_or_404()

    @app.get(f"/api/social/{SOCIAL_KEY}/roundup", response_class=PlainTextResponse)
    def social_roundup_latest():
        """The newest roundup's drafts. Declared before the /{gw} route below,
        which would otherwise try to read "roundup" as a gameweek number."""
        return _roundup_drafts_or_404()

    @app.get(f"/api/social/{SOCIAL_KEY}/roundup/{{gw}}",
             response_class=PlainTextResponse)
    def social_roundup_keyed(gw: int):
        return _roundup_drafts_or_404(gw)

    @app.get(f"/api/social/{SOCIAL_KEY}/player", response_class=PlainTextResponse)
    def social_player_index():
        """Every player write-up still on disk, newest first.

        An index rather than a bare list of dates: the whole point of the
        nightly post is deciding each morning whether tonight's is worth
        posting, and a list of filenames doesn't help with that. Each line
        carries the player, the angle and the gameweek it was written for.
        """
        days = social.list_player_drafts()
        if not days:
            raise HTTPException(status_code=404, detail="No player write-ups yet.")

        # The ledger has the player and angle without decoding the payloads.
        # A file with no matching row is still listed - deleting the row and
        # leaving the file is a state worth being able to see.
        ledger = {r["post_date"]: r for r in db.recent_player_posts(limit=400)}
        lines = [f"FPL Buddy — player write-ups ({len(days)} on disk)", "=" * 46, ""]
        for day in days:
            row = ledger.get(day)
            if row:
                post = db.get_player_post(day)
                payload = (post or {}).get("payload") or {}
                lines.append(
                    f"{day}  GW{row['gameweek']:<3} "
                    f"{payload.get('name', '?'):<18} "
                    f"{payload.get('angle_label', row['angle'])}")
            else:
                lines.append(f"{day}  (file only — no ledger row)")
        lines += ["", f"Read one:  /api/social/{SOCIAL_KEY}/player/<date>",
                  f"Newest:    /api/social/{SOCIAL_KEY}/player/latest"]
        return _noindex("\n".join(lines))

    @app.get(f"/api/social/{SOCIAL_KEY}/player/latest",
             response_class=PlainTextResponse)
    def social_player_latest():
        """Tonight's write-up. This is the one to bookmark.

        Declared before the /{day} route so "latest" isn't taken as a date."""
        return _player_drafts_or_404()

    @app.get(f"/api/social/{SOCIAL_KEY}/player/{{day}}",
             response_class=PlainTextResponse)
    def social_player_day(day: str):
        """An earlier night's write-up, by date.

        `day` goes into a filename, so social.read_player_drafts validates it
        against YYYY-MM-DD and returns None for anything else - which lands
        here as a 404 rather than as a path traversal."""
        return _player_drafts_or_404(day)

    @app.get(f"/api/social/{SOCIAL_KEY}/{{gw}}", response_class=PlainTextResponse)
    def social_drafts_keyed(gw: int):
        """A specific gameweek, for going back to an earlier week.

        Declared LAST of the keyed routes. It takes an int, so FastAPI would
        422 rather than match "player" or "roundup" - but relying on that means
        the ordering becomes load-bearing the first time one of those paths
        takes a string segment, which is the kind of thing that breaks quietly."""
        return _drafts_or_404(gw)


# Ko-fi's webhook, relayed into Discord.
#
# Ko-fi can POST to an arbitrary URL on every donation. It cannot post to a
# Discord webhook directly - Discord expects its own JSON shape and would
# reject or mangle Ko-fi's - so this endpoint is the translation, and it is
# also where the two things Ko-fi gets wrong for us are fixed: the payload
# carries the donor's EMAIL, which has no business in a chat channel, and the
# donor's name and message are free text typed by a stranger.
#
# Unset, the route does not exist - the same rule the IndexNow and social key
# routes follow. An endpoint that exists and refuses is one more thing on the
# internet to probe.
KOFI_TOKEN = os.environ.get("FPL_KOFI_TOKEN", "").strip()

# Ko-fi's payloads are small - a few hundred bytes. This is generous enough for
# a long donor message and small enough that the endpoint cannot be used to
# make the app read a large body from an unauthenticated caller.
KOFI_MAX_BODY = 16 * 1024

if KOFI_TOKEN:
    @app.post("/api/kofi", include_in_schema=False)
    async def kofi_webhook(request: Request):
        """Relay a Ko-fi donation into Discord.

        Publicly reachable by necessity - Ko-fi's servers have to be able to
        POST here and they do not authenticate. What makes that safe is the
        `verification_token` Ko-fi includes in every payload, compared here in
        constant time against FPL_KOFI_TOKEN.

        Returns 200 for anything it accepts, including a duplicate. Ko-fi
        retries on a non-2xx, so a genuine delivery that this app has already
        handled must not be reported as a failure or it will be redelivered
        forever.
        """
        body = await request.body()
        if len(body) > KOFI_MAX_BODY:
            raise HTTPException(status_code=413, detail="Payload too large")

        # Ko-fi sends form-encoded with the JSON in a `data` field. Plain JSON
        # is accepted too, so a change at their end doesn't silently drop
        # every donation.
        payload = None
        try:
            text = body.decode("utf-8", "replace")
            fields = urllib.parse.parse_qs(text)
            raw = (fields.get("data") or [None])[0]
            payload = json.loads(raw if raw is not None else text)
        except (ValueError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Unreadable payload")

        # Constant-time, so the endpoint cannot be used to recover the token a
        # character at a time by timing the responses.
        supplied = str(payload.get("verification_token") or "")
        if not hmac.compare_digest(supplied, KOFI_TOKEN):
            raise HTTPException(status_code=403, detail="Bad verification token")

        # Ko-fi retries a delivery it didn't get a 2xx for, and a retry of a
        # message already relayed should not post a second time.
        message_id = str(payload.get("message_id")
                         or payload.get("kofi_transaction_id") or "")
        if message_id and not db.mark_notified("kofi", message_id):
            return {"ok": True, "duplicate": True}

        # Deliberately nothing about the payload is logged. It carries an email
        # address, and a log line is a copy of it that outlives every promise
        # the privacy policy makes.
        sent = ops.notify("kofi", social.channel_kofi(payload))
        return {"ok": True, "notified": bool(sent)}


# A question is one short paragraph. Long enough for a real one with a player
# name and a bit of context; short enough that it cannot be used to push a wall
# of text at whoever reads the channel. Well under ops.DISCORD_LIMIT, which
# leaves room for the prefix rather than relying on that truncation.
QUESTION_MAX_CHARS = 600

# In-process, per-IP, and deliberately not a database table. The whole promise
# of this endpoint is that asking a question stores nothing about you, and a
# rate limiter that persisted addresses would quietly break that promise in
# order to protect it. Lost on restart, which is fine: it exists to stop a
# bored person holding down a button, not a determined flood - that is
# Cloudflare's job and it is already in front of this.
_QUESTION_HITS = {}
QUESTION_WINDOW_S = 600
QUESTION_MAX_PER_WINDOW = 5


def _question_allowed(key):
    """True if this caller may send another. Prunes as it goes, so the dict
    cannot grow without bound on a site with no other per-IP state."""
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - QUESTION_WINDOW_S
    for k, stamps in list(_QUESTION_HITS.items()):
        fresh = [t for t in stamps if t > cutoff]
        if fresh:
            _QUESTION_HITS[k] = fresh
        else:
            del _QUESTION_HITS[k]
    recent = _QUESTION_HITS.get(key, [])
    if len(recent) >= QUESTION_MAX_PER_WINDOW:
        return False
    _QUESTION_HITS[key] = recent + [now]
    return True


@app.post("/api/question")
async def ask_question(request: Request, payload: dict = Body(...)):
    """Relay a question from the FAQ page to whoever reads the questions channel.

    Stores NOTHING. No database row, no address, no log line - the webhook post
    is the entire lifetime of the message. That is the deal the form on /faq
    makes with the reader in as many words, and it is the reason there is no
    reply-to field: collecting one would be collecting an email address, which
    is exactly what this was asked not to do.

    The consequence, stated on the form too: nobody can be answered directly. A
    question that turns out to be common becomes an FAQ entry, which is where
    the answer reaches the person who asked and everyone after them.
    """
    question = payload.get("question")
    if not isinstance(question, str):
        raise HTTPException(status_code=400, detail="Ask a question first.")

    # Control characters stripped rather than escaped. The receiving end is a
    # chat client rendering Markdown, and a newline-stuffed message can push
    # everything before it off the screen. Ordinary newlines survive as spaces.
    question = "".join(ch if ch.isprintable() else " " for ch in question).strip()
    if not question:
        raise HTTPException(status_code=400, detail="Ask a question first.")
    if len(question) > QUESTION_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Please keep it under {QUESTION_MAX_CHARS} characters.")

    # CF-Connecting-IP is the real client behind the tunnel; request.client is
    # Cloudflare. Falls back so a local run is still rate limited rather than
    # silently unlimited.
    caller = (request.headers.get("cf-connecting-ip")
              or (request.client.host if request.client else "unknown"))
    if not _question_allowed(caller):
        raise HTTPException(
            status_code=429,
            detail="That's a few questions in a short time - try again shortly.")

    sent = ops.notify("questions", question, prefix="FAQ question")
    # `sent` False means no webhook is configured, which the reader cannot fix
    # and should not be told about in those terms. Reporting success would be a
    # lie, though - the question went nowhere - so this is honest and vague.
    if not sent:
        raise HTTPException(
            status_code=503,
            detail="Couldn't send that just now. The contact page has an address.")
    return {"ok": True}


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header(default="")):
    """Re-pull everything, re-detecting the mode - so the app crosses over to
    inseason on its own once the first deadline passes, with no redeploy.

    Token-gated when FPL_REFRESH_TOKEN is set: this triggers a full pipeline run
    (~a minute of CPU) and is what the cron jobs call, so it shouldn't be
    anonymously reachable from the internet.

    Serialised against the startup warm-up: a nightly job firing while a deploy
    is still building its ratings would otherwise run a second pipeline beside
    the first. This blocks until the warm-up finishes and then does its own
    pass, which is slower than failing fast but is what the caller asked for.
    """
    require_refresh_token(x_refresh_token)
    load_data_serialised()
    return {"status": "refreshed", "mode": state["mode"]}