"""
FastAPI backend for FPL Companion.

Run locally with:
    uvicorn main:app --reload
"""

import math
import os
import re
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
import manager_history
import player_pages
import seasons
from pipeline import run_pipeline
from rating_model import get_rated_position_dfs
from fixture_rotator import (get_rotation_data, rank_rotation_pairs, recommend_pair_players, team_fixture_map)
from search import search_player
from squad_optimiser import DEFAULT_BUDGET, OptimisationError
from team_service import (get_team_view, get_league_standings, get_all_players, get_player_summary,
                          get_news_feed, get_underperforming_players, team_name_map,
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
# and that page's server-rendered HTML is barely forty words. Each pane now has
# a real path that renders the same shell with that tab already open.
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

# Per-route metadata. Every indexable page needs its own title and description:
# without them Google writes the snippet itself, usually by grabbing whatever
# text it finds first. `priority`/`changefreq` feed the sitemap; both default
# below, so a route only states them when it differs.
PAGES = {
    "/": {
        "title": "FPL Buddy — Fantasy Premier League ratings and AI teams",
        "description": ("Free Fantasy Premier League tools: player ratings from a trained "
                        "points model, two AI-picked squads each gameweek, a fixture "
                        "rotation planner and your own team analysed. Enter your FPL ID "
                        "to get started."),
        "priority": "1.0",
        "changefreq": "weekly",
    },
    "/my-team": {
        "title": "My FPL team — squad, lineup and transfer analysis | FPL Buddy",
        "h1": "My FPL team",
        "description": ("Load your Fantasy Premier League squad from your FPL ID: predicted "
                        "points for every player, an optimised starting XI, transfer "
                        "suggestions, chip advice and live gameweek scoring."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/ai-teams": {
        "title": "AI Fantasy Premier League teams — AI Manager and best XV",
        "h1": "AI Fantasy Premier League teams",
        "description": ("Two AI-picked Fantasy Premier League squads: a manager bot that "
                        "plays every gameweek with real transfers and chips, and the "
                        "highest-scoring XV for the upcoming gameweek."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/players": {
        "title": "FPL player ratings and predicted points | FPL Buddy",
        "h1": "FPL player ratings",
        "description": ("Every Fantasy Premier League player rated by a trained points model, "
                        "with predicted points for the next gameweeks, price, form, ownership "
                        "and the players underperforming their underlying numbers."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/fixture-rotator": {
        "title": "FPL fixture rotation planner — best team pairs | FPL Buddy",
        "h1": "FPL fixture rotation planner",
        "description": ("Find Fantasy Premier League defence and attack rotation pairs: two "
                        "clubs whose fixtures alternate so one of them always has a good "
                        "game, ranked over the next eight gameweeks."),
        "priority": "0.9",
        "changefreq": "daily",
    },
    "/players/a-z": {
        "title": "Every Fantasy Premier League player A-Z | FPL Buddy",
        "description": ("A complete list of every player in Fantasy Premier League, "
                        "grouped by surname, each linking to their price, ownership, "
                        "form, underlying numbers and predicted points."),
        "priority": "0.8",
        "changefreq": "weekly",
    },
    "/about": {
        "title": "About FPL Buddy — how the ratings and AI squads work",
        "description": ("How FPL Buddy rates players, how the two AI squads are picked, "
                        "and what the numbers on the site actually mean."),
    },
    "/privacy": {
        "title": "Privacy policy — FPL Buddy",
        "description": ("What FPL Buddy stores, why, how long for, and how to have it "
                        "deleted."),
    },
    "/contact": {
        "title": "Contact FPL Buddy",
        "description": "How to report a bug, ask a question, or raise a rights or security issue.",
    },
}

app = FastAPI()

# app.js is ~107 KB and style.css another ~60 KB uncompressed, and the JSON from
# /api/all_players is larger than both. Gzip takes roughly 70-80% off all three.
# minimum_size skips the small responses, where the compression work and the
# lost chance of a byte-range request cost more than the saved bytes.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers a browser needs in order to defend the page.

    Deliberately not including a Content-Security-Policy: the templates carry
    several inline <script> blocks (the initial pane, the landing-page redirect)
    and a real policy needs those moved out or given nonces first. A CSP added
    without that work either does nothing useful or breaks the site, so it's
    tracked as its own job rather than half-done here.

    HSTS is only sent over HTTPS. Sent on a plain-HTTP local run it would pin
    the developer's browser to https://localhost and make the app unreachable.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
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

    rotation_df = get_rotation_data(mode=mode, n_gameweeks=8)

    state["mode"] = mode
    state["position_dfs"] = position_dfs
    state["rotation_df"] = rotation_df
    clear_preview_cache()   # ratings moved; any cached AI squad is now stale
    print(f"Ready ({mode}).")


def _player_pool():
    """Flat rated player pool from the in-memory state, for DB read-joins."""
    if state["position_dfs"] is None:
        return []
    return get_all_players(state["position_dfs"]).get("players", [])


# Solving an ILP per page view would make the AI tabs feel slow and burn CPU
# re-deriving an answer that only changes when the underlying ratings do. These
# hold the live preview for the upcoming gameweek; load_data() clears them, so
# a refresh (or the nightly job) is what invalidates, not a timer.
_preview_cache = {"best_xv": {}, "manager": {}}


def clear_preview_cache():
    _preview_cache["best_xv"].clear()
    _preview_cache["manager"].clear()
    _player_pages["index"] = None


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


def cached_best_xv(gameweek, budget=DEFAULT_BUDGET):
    key = (gameweek, budget)
    if key not in _preview_cache["best_xv"]:
        _preview_cache["best_xv"][key] = ai_team.build_best_xv(
            _player_pool(), gameweek, budget=budget)
    return _preview_cache["best_xv"][key]


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
    load_data()

def page_context(request, path, meta=None, **extra):
    """Shared template context: metadata for `path` plus site-wide values.

    `meta` overrides the PAGES lookup, for routes whose title and description
    are per-record rather than per-route - the player pages, where there are
    several hundred of them and each one describes a different footballer."""
    meta = meta or PAGES.get(path, PAGES["/"])
    return {
        "request": request,
        "asset_version": asset_version(),
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
        "today": datetime.now(timezone.utc).strftime("%-d %B %Y")
                 if os.name != "nt" else datetime.now(timezone.utc).strftime("%d %B %Y").lstrip("0"),
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

    `tabs` carries each tab's title and heading as well as its label, so app.js
    can set document.title and the <h1> when you switch tabs without a page
    load - otherwise the browser tab, and anything bookmarked from it, keeps the
    name of whichever page you happened to arrive on, and the heading describes
    a pane that is no longer on screen.

    All four panes are in the DOM on all four URLs, so the <h1> is rendered once
    here from the routing table rather than once per pane. Four <h1>s - three of
    them hidden - is what a crawler would otherwise be handed."""
    tabs = [dict(t, title=PAGES[t["path"]]["title"], h1=PAGES[t["path"]]["h1"])
            for t in TABS]
    return html_response("index.html", request, path, mode=state["mode"],
                         initial_pane=TAB_PANES[path], tabs=tabs,
                         page_h1=PAGES[path]["h1"])


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


@app.get("/sitemap.xml")
def sitemap():
    """Every indexable URL: the landing page, the four tool pages and the prose
    pages.

    `lastmod` is today for the tool pages because their content genuinely does
    change every day - ratings, predictions and the AI squads all move. The
    prose pages are marked monthly and given a lower priority so crawl budget
    goes to the pages worth recrawling."""
    today = datetime.now(timezone.utc).date().isoformat()
    urls = "".join(
        f"<url><loc>{SITE_URL}{path}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{meta.get('changefreq', 'monthly')}</changefreq>"
        f"<priority>{meta.get('priority', '0.5')}</priority></url>"
        for path, meta in PAGES.items())
    # Plus one entry per player. Several hundred URLs, well inside the 50,000
    # limit for a single sitemap, so this doesn't need splitting into an index.
    # `xml_escape` because a name can legitimately contain an ampersand.
    urls += "".join(
        f"<url><loc>{SITE_URL}{xml_escape(rec['path'])}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>0.6</priority></url>"
        for rec in player_page_index().values())
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return Response(content=xml, media_type="application/xml",
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
    """Save the working squad. Replaces whatever was stored for this id."""
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
    # Same reasoning as the Best-XV endpoint: the upcoming gameweek's plan is
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
#  AI Best-XV
# =========================================================================

@app.get("/api/ai/best_xv")
def ai_best_xv(gameweek: int = None, budget: float = DEFAULT_BUDGET):
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
        result = cached_best_xv(target, budget)
    except OptimisationError as e:
        return {"available": False, "gameweek": target, "detail": str(e)}
    # Live preview: deliberately not persisted. The deadline watcher owns
    # writing snapshots, so a page view can't freeze a half-formed squad.
    return {"available": True, "stored": False, "actual_points": None,
            "db_error": db_error, **result}


@app.get("/api/ai/history")
def ai_history():
    """Predicted vs actual for every frozen AI Best-XV snapshot."""
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
    }


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
        load_data(mode)
        return {"status": "ok", "mode": state["mode"]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header(default="")):
    """Re-pull everything, re-detecting the mode - so the app crosses over to
    inseason on its own once the first deadline passes, with no redeploy.

    Token-gated when FPL_REFRESH_TOKEN is set: this triggers a full pipeline run
    (~a minute of CPU) and is what the cron jobs call, so it shouldn't be
    anonymously reachable from the internet."""
    require_refresh_token(x_refresh_token)
    load_data()
    return {"status": "refreshed", "mode": state["mode"]}