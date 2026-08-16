"""Pull Search Console, Bing Webmaster and PageSpeed data into a dated file.

    python seo_report.py              # fetch everything, write state/seo/<date>.json
    python seo_report.py --dry-run    # fetch and print a summary, write nothing
    python seo_report.py --only gsc   # one source (gsc | bing | psi)

Why this exists: the alternative is screenshots. A screenshot of the Search
Console Performance tab shows the ten rows that fitted on screen, with no
history; this writes every row to disk every night, so questions like "which
queries moved this week" become answerable instead of guessed at.

Each source is fetched independently and a failure in one is recorded in the
output rather than raised. A night where Bing's API is down should still
capture Google's data - a report with a hole in it is worth having, and a job
that exits non-zero on a partial failure is a job whose log nobody reads.

Nothing here writes to the app's database or touches its in-memory state. Like
indexnow.py this is a standalone script: no model load, no pipeline, a few
seconds of HTTP.

Credentials, all optional - an unset source is skipped with a note:

    GSC_KEY             path to a Google service-account JSON (NOT the key
                        itself). The service account's client_email must also
                        be added as a user on the property in Search Console;
                        creating it grants no access on its own.
    BINGWEBMASTER_KEY   Bing Webmaster Tools -> Settings -> API access
    PSI_KEY             Google Cloud API key with the PageSpeed Insights API
                        enabled. Optional even when the rest is configured;
                        without it the Core Web Vitals section is skipped.
"""

import argparse
import json
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import requests

SITE_URL = os.environ.get("FPL_SITE_URL", "https://fpl.mfhost.co.uk").rstrip("/")

# How Search Console identifies the property, which is not always the site's
# own URL. A URL-prefix property is "https://fpl.mfhost.co.uk/" - trailing
# slash included, and it must match what is verified exactly. A domain property
# is the different-looking "sc-domain:mfhost.co.uk". Wrong either way, the API
# returns 403 rather than "no such property", which reads like a permissions
# problem and sends you looking in the wrong place.
# `or` rather than a get() default: docker-compose sets a variable it names to
# an empty string when .env doesn't define it, and an empty string is present as
# far as os.environ.get is concerned - so the default would never apply and the
# property would be "". Falling back on falsiness covers both spellings of unset.
GSC_PROPERTY = os.environ.get("GSC_PROPERTY", "").strip() or SITE_URL + "/"

GSC_KEY = os.environ.get("GSC_KEY", "").strip()
BING_KEY = os.environ.get("BINGWEBMASTER_KEY", "").strip()
PSI_KEY = os.environ.get("PSI_KEY", "").strip()

GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_API = "https://searchconsole.googleapis.com/webmasters/v3"
BING_API = "https://ssl.bing.com/webmaster/api.svc/json"
PSI_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Search Console data lags by 2-3 days and the most recent day is always
# partial. Ending the window three days back means a row never changes after
# it's been captured, so two reports can be compared without the difference
# being an artefact of when they ran.
GSC_LAG_DAYS = 3
GSC_WINDOW_DAYS = 28

# The API caps a single response at 25,000 rows. Nothing here is close, and
# asking for the maximum costs nothing when there is less.
GSC_ROW_LIMIT = 5000

# The pages worth tracking speed on: the landing page, the two most valuable
# tool pages, and one gameweek briefing as a representative server-rendered
# page. Each is a separate PSI call taking 10-30 seconds, so this is a short
# list on purpose.
PSI_PAGES = ["/", "/players", "/ai-teams", "/gameweek"]

TIMEOUT = 90


def log(msg):
    print(msg, flush=True)


def out_dir():
    """state/seo/ beside the database, so reports land on the mounted volume
    and survive a redeploy. Derived from FPL_DB_PATH rather than configured
    separately - there is one writable persistent directory in this deployment
    and a second setting would be a second thing to get out of sync."""
    import db
    path = os.path.join(os.path.dirname(os.path.abspath(db.db_path())), "seo")
    os.makedirs(path, exist_ok=True)
    return path


# ---- Google Search Console -------------------------------------------------

def gsc_token():
    """An access token from the service-account key.

    google-auth does the RS256 JWT signing and the token exchange. It's the one
    dependency this script adds, and it is the right place to draw the line:
    hand-rolling RSA signing to avoid a well-maintained library would be a poor
    trade. The much heavier google-api-python-client is deliberately NOT used -
    the two endpoints below are plain REST and `requests` already ships here."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        GSC_KEY, scopes=[GSC_SCOPE])
    creds.refresh(Request())
    return creds.token


def gsc_sites(token):
    """Every property this service account can actually see.

    The diagnostic for a 403. Search Console returns the same "User does not
    have sufficient permission for site X" whether the service account was
    never added to the property OR the property string is wrong - a URL-prefix
    property is "https://example.com/" while a domain property is
    "sc-domain:example.com", and neither error mentions the other exists.

    This asks the opposite question: not "may I read this site" but "what sites
    are there". An empty list means the account is authorised nowhere, so the
    fix is in Search Console's Users and permissions. A non-empty list whose
    entries don't match GSC_PROPERTY means the account is fine and the string
    is wrong - and prints the exact value to use."""
    r = requests.get(f"{GSC_API}/sites",
                     headers={"Authorization": f"Bearer {token}"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("siteEntry", [])


def diagnose_gsc():
    """Print why Search Console is refusing, and what to do about it."""
    if not GSC_KEY:
        log("GSC_KEY is not set.")
        return 1
    if not os.path.isfile(GSC_KEY):
        log(f"GSC_KEY points at {GSC_KEY}, which does not exist.")
        return 1

    with open(GSC_KEY, encoding="utf-8") as fh:
        email = json.load(fh).get("client_email", "(no client_email in the file)")
    log(f"Service account: {email}")
    log(f"Looking for property: {GSC_PROPERTY}")

    try:
        sites = gsc_sites(gsc_token())
    except requests.RequestException as exc:
        log(f"Could not list sites: {_http_detail(exc)}")
        return 1

    if not sites:
        log("")
        log("This service account can see NO properties at all.")
        log("Fix: Search Console -> Settings -> Users and permissions ->")
        log(f"     Add user -> {email} -> permission Full.")
        return 1

    log("")
    log("Properties this account CAN see:")
    for s in sites:
        marker = "  <- matches GSC_PROPERTY" if s.get("siteUrl") == GSC_PROPERTY else ""
        log(f"  {s.get('permissionLevel','?'):<22} {s.get('siteUrl')}{marker}")

    if not any(s.get("siteUrl") == GSC_PROPERTY for s in sites):
        log("")
        log(f"None of these is {GSC_PROPERTY}.")
        log("Fix: set GSC_PROPERTY in .env to one of the values above,")
        log("     copied exactly - the trailing slash is part of it.")
        return 1

    log("")
    log("Property matches and is readable. If queries still 403, the")
    log("permission level above is too low - it needs Full or Owner.")
    return 0


def gsc_query(token, dimensions, start, end, row_limit=GSC_ROW_LIMIT):
    """One searchAnalytics query. `dimensions` is e.g. ["query"] or
    ["query", "page"]."""
    prop = urllib.parse.quote(GSC_PROPERTY, safe="")
    r = requests.post(
        f"{GSC_API}/sites/{prop}/searchAnalytics/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"startDate": start, "endDate": end, "dimensions": dimensions,
              "rowLimit": row_limit, "type": "web"},
        timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json().get("rows", [])
    # `keys` comes back as a positional list matching `dimensions`. Zipping it
    # into named fields here means the stored file is readable on its own,
    # rather than needing this script to interpret it months later.
    return [{**dict(zip(dimensions, row.get("keys", []))),
             "clicks": row.get("clicks"), "impressions": row.get("impressions"),
             "ctr": row.get("ctr"), "position": row.get("position")}
            for row in rows]


def fetch_gsc():
    if not GSC_KEY:
        return {"skipped": "GSC_KEY not set"}
    if not os.path.isfile(GSC_KEY):
        return {"error": f"GSC_KEY points at {GSC_KEY}, which does not exist. "
                         "It must be a PATH to the service-account JSON."}

    end = date.today() - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=GSC_WINDOW_DAYS)
    s, e = start.isoformat(), end.isoformat()

    token = gsc_token()
    out = {"property": GSC_PROPERTY, "start": s, "end": e}

    # query x page is the one that earns its place: it shows which page is
    # ranking for which term, and therefore where a page is ranking for
    # something it isn't actually about.
    for name, dims in (("queries", ["query"]), ("pages", ["page"]),
                       ("query_page", ["query", "page"]), ("dates", ["date"]),
                       ("devices", ["device"]), ("countries", ["country"])):
        try:
            out[name] = gsc_query(token, dims, s, e)
            log(f"  gsc {name}: {len(out[name])} rows")
        except requests.RequestException as exc:
            out[name] = {"error": _http_detail(exc)}
            log(f"  gsc {name} FAILED: {out[name]['error']}")

    try:
        prop = urllib.parse.quote(GSC_PROPERTY, safe="")
        r = requests.get(f"{GSC_API}/sites/{prop}/sitemaps",
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        out["sitemaps"] = r.json().get("sitemap", [])
        log(f"  gsc sitemaps: {len(out['sitemaps'])}")
    except requests.RequestException as exc:
        out["sitemaps"] = {"error": _http_detail(exc)}

    return out


def _http_detail(exc):
    """The API's own message, not just the status code.

    Google returns a JSON body explaining a 403 - "User does not have
    sufficient permission for site X" is the difference between a wrong
    property string and a service account nobody added to the property, and
    without the body both look identical.

    Not every RequestException has a response: a timeout, a DNS failure or a
    TLS error never got one. Those report the exception itself, because
    "HTTP ?:" with an empty body would throw away the only description of the
    fault there is."""
    response = getattr(exc, "response", None)
    if response is None:
        return f"{type(exc).__name__}: {exc}"
    try:
        body = json.dumps(response.json().get("error", {}))[:300]
    except ValueError:
        body = (response.text or "")[:300]
    return f"HTTP {response.status_code}: {body}"


# ---- Bing Webmaster Tools --------------------------------------------------

def bing_call(method):
    r = requests.get(f"{BING_API}/{method}",
                     params={"apikey": BING_KEY, "siteUrl": SITE_URL},
                     timeout=TIMEOUT)
    r.raise_for_status()
    # Bing wraps every response in {"d": ...}.
    return r.json().get("d")


def fetch_bing():
    if not BING_KEY:
        return {"skipped": "BINGWEBMASTER_KEY not set"}

    out = {"site": SITE_URL}
    # GetCrawlStats is the one that matters most here: it reports blocked-by-
    # robots counts and 4xx/5xx, which is what would have diagnosed the
    # "Discovered but not crawled" problem directly instead of inferring it
    # from Cloudflare's sampled logs.
    for name, method in (("crawl_stats", "GetCrawlStats"),
                         ("rank_traffic", "GetRankAndTrafficStats"),
                         ("queries", "GetQueryStats"),
                         ("submission_quota", "GetUrlSubmissionQuota")):
        try:
            out[name] = bing_call(method)
            n = len(out[name]) if isinstance(out[name], list) else 1
            log(f"  bing {name}: {n} record(s)")
        except requests.RequestException as exc:
            out[name] = {"error": _http_detail(exc)}
            log(f"  bing {name} FAILED: {out[name]['error']}")
    return out


# ---- PageSpeed Insights ----------------------------------------------------

def fetch_psi():
    if not PSI_KEY:
        return {"skipped": "PSI_KEY not set"}

    out = {}
    for path in PSI_PAGES:
        url = SITE_URL + path
        try:
            r = requests.get(PSI_API, timeout=TIMEOUT, params={
                "url": url, "strategy": "mobile", "category": "performance",
                "key": PSI_KEY})
            r.raise_for_status()
            lh = r.json()["lighthouseResult"]
            audits = lh["audits"]
            # Only the handful of numbers worth trending. The full response is
            # ~400 KB per page, and storing four of those nightly would be
            # 500 MB a year to answer questions nobody asks.
            out[path] = {
                "score": round(lh["categories"]["performance"]["score"] * 100),
                **{k: audits.get(k, {}).get("numericValue")
                   for k in ("first-contentful-paint", "largest-contentful-paint",
                             "total-blocking-time", "cumulative-layout-shift",
                             "speed-index")},
            }
            log(f"  psi {path}: score {out[path]['score']}, "
                f"CLS {out[path]['cumulative-layout-shift']:.3f}")
        except (requests.RequestException, KeyError, ValueError) as exc:
            detail = _http_detail(exc) if isinstance(exc, requests.HTTPError) else f'{type(exc).__name__}: {exc}'
            out[path] = {"error": detail}
            log(f"  psi {path} FAILED: {detail}")
    return out


# ---- assembly --------------------------------------------------------------

def build(only=None):
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "site": SITE_URL}
    for name, fn in (("gsc", fetch_gsc), ("bing", fetch_bing), ("psi", fetch_psi)):
        if only and only != name:
            continue
        log(f"{name}:")
        try:
            report[name] = fn()
        except Exception as exc:
            # A whole source failing must not cost the others. This catches
            # broadly on purpose: an auth library raising something unexpected
            # should still leave Bing's data on disk.
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
            log(f"  {name} FAILED: {report[name]['error']}")
        # A source that returned nothing has to say so on the way past. Stored
        # in the file but silent in the log, a misconfigured credential looks
        # exactly like a quiet night with no data - and this runs unattended.
        result = report.get(name)
        if isinstance(result, dict):
            if result.get("skipped"):
                log(f"  skipped - {result['skipped']}")
            elif result.get("error"):
                log(f"  ERROR - {result['error']}")
    return report


def summarise(report):
    """A few lines for the cron log, so a run is legible without opening the
    JSON."""
    lines = []
    gsc = report.get("gsc") or {}
    if isinstance(gsc.get("queries"), list):
        rows = gsc["queries"]
        clicks = sum(r.get("clicks") or 0 for r in rows)
        impressions = sum(r.get("impressions") or 0 for r in rows)
        lines.append(f"GSC {gsc.get('start')}..{gsc.get('end')}: "
                     f"{len(rows)} queries, {clicks:.0f} clicks, "
                     f"{impressions:.0f} impressions")
        for r in sorted(rows, key=lambda r: -(r.get("impressions") or 0))[:5]:
            lines.append(f"   {r.get('impressions'):>6.0f} imp  "
                         f"pos {r.get('position', 0):>5.1f}  {r.get('query')}")
    psi = report.get("psi") or {}
    for path, v in psi.items():
        if isinstance(v, dict) and "score" in v:
            lines.append(f"PSI {path}: {v['score']}  "
                         f"CLS {v['cumulative-layout-shift']:.3f}")
    return lines


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _totals_rows(gsc):
    """The rows to add up for the headline figures, and where they came from.

    This exists because of a genuinely misleading bug. The digest used to sum
    the `queries` rows, which sounds like the obvious choice and is wrong:
    Google anonymises rare search terms and OMITS them from any response
    dimensioned by query. On a small site that is most of the traffic. From one
    of this site's own stored reports, same property, same 28-day window:

        queries    1 row    ->  0 clicks,  1 impression
        pages      5 rows   ->  3 clicks, 64 impressions
        dates      3 rows   ->  3 clicks, 64 impressions
        devices    2 rows   ->  3 clicks, 64 impressions
        countries 13 rows   ->  3 clicks, 64 impressions

    The digest reported "0 clicks from 1 impression" for a week that had 3 and
    64. Any dimension other than query totals correctly, so `dates` is used -
    one row per day, the smallest of them, and the one whose row count is a
    sanity check in itself.

    No extra API call: fetch_gsc already requests all six dimensions and stores
    them. The correct numbers were in the file the whole time.
    """
    for name in ("dates", "pages", "devices", "countries", "queries"):
        rows = gsc.get(name)
        if isinstance(rows, list) and rows:
            return rows, name
    return [], None


def _totals(rows):
    """(clicks, impressions, mean position) across `rows`."""
    clicks = sum(r.get("clicks") or 0 for r in rows)
    impressions = sum(r.get("impressions") or 0 for r in rows)
    # Weighted by impressions, not a mean of the positions: an average over
    # 400 rows is dominated by the long tail nobody searches, and would report
    # a healthy site as sitting on page four.
    position = (sum((r.get("position") or 0) * (r.get("impressions") or 0)
                    for r in rows) / impressions) if impressions else 0
    return clicks, impressions, position


def digest(report):
    """The weekly Discord message: how search is going, in a dozen lines.

    A different thing from `summarise` above, which is written for a log and
    prints whatever it has. This is written for someone reading a phone on a
    Monday and answers one question - is this growing or not - so it leads with
    the totals and follows with the queries worth knowing about.
    """
    lines = ["📈 **Weekly search digest**"]

    gsc = report.get("gsc") or {}
    rows = gsc.get("queries") if isinstance(gsc.get("queries"), list) else []
    totals_rows, totals_from = _totals_rows(gsc)
    if rows or totals_rows:
        clicks, impressions, position = _totals(totals_rows)
        lines += ["", f"**Google** ({gsc.get('start')} → {gsc.get('end')})",
                  f"• {clicks:.0f} {_plural(clicks, 'click')} from "
                  f"{impressions:.0f} {_plural(impressions, 'impression')}",
                  f"• {len(rows)} named {_plural(len(rows), 'query', 'queries')}, "
                  f"average position {position:.1f}"]
        # Named against the totals, whenever the two disagree. They almost
        # always do, and the gap is the single most confusing thing this
        # message prints - see _totals_rows.
        named_clicks, named_impressions, _pos = _totals(rows)
        if rows and (named_impressions < impressions or named_clicks < clicks):
            lines.append(
                f"• {'that query accounts' if len(rows) == 1 else 'those queries account'} "
                f"for {named_clicks:.0f} {_plural(named_clicks, 'click')} and "
                f"{named_impressions:.0f} "
                f"{_plural(named_impressions, 'impression')} — "
                "Google hides the rest")
        elif totals_from == "queries":
            lines.append("• totals are query-level only; Google's own figure "
                         "will be higher")

        top = sorted(rows, key=lambda r: -(r.get("clicks") or 0))[:5]
        if any((r.get("clicks") or 0) > 0 for r in top):
            lines += ["", "**Bringing clicks**"]
            for r in top:
                if (r.get("clicks") or 0) <= 0:
                    continue
                lines.append(f"• {r.get('clicks'):.0f} — {r.get('query')} "
                             f"(pos {r.get('position', 0):.1f})")

        # High impressions and no clicks is the actionable case: the page ranks
        # and the title or description isn't earning the click.
        near = [r for r in rows
                if (r.get("impressions") or 0) >= 50 and not (r.get("clicks") or 0)]
        near.sort(key=lambda r: -(r.get("impressions") or 0))
        if near:
            lines += ["", "**Seen but not clicked** (worth a title rewrite)"]
            for r in near[:5]:
                lines.append(f"• {r.get('impressions'):.0f} imp, "
                             f"pos {r.get('position', 0):.1f} — {r.get('query')}")
    else:
        lines += ["", "**Google:** no query data in this report."]

    bing = report.get("bing") or {}
    if isinstance(bing, dict) and bing.get("clicks") is not None:
        lines += ["", f"**Bing:** {bing.get('clicks')} clicks, "
                      f"{bing.get('impressions')} impressions"]

    psi = report.get("psi") or {}
    scores = [f"{path} {v['score']}" for path, v in psi.items()
              if isinstance(v, dict) and "score" in v]
    if scores:
        lines += ["", "**PageSpeed:** " + " · ".join(scores)]

    return "\n".join(lines)


def send_weekly_digest(report, today=None):
    """Push the digest once a week, on the first run of each ISO week.

    Keyed on the ISO week rather than on the weekday, so a Monday where the box
    was down doesn't skip that week - the Tuesday run sends it instead. Weekly
    rather than daily because Search Console only updates once a day and lags
    two to three days behind, so a daily digest would report the same numbers
    with a different date on them.
    """
    import db
    import ops
    today = today or date.today()
    year, week, _day = today.isocalendar()
    ref = f"{year}-W{week:02d}"
    if ops.notify_once("seo", "seo_digest", ref, digest(report)):
        log(f"  weekly digest pushed ({ref})")
        return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print, write nothing")
    parser.add_argument("--digest", action="store_true",
                        help="push the weekly Discord digest now, ignoring "
                             "whether one has already gone this week")
    parser.add_argument("--only", choices=("gsc", "bing", "psi"),
                        help="fetch a single source")
    parser.add_argument("--diagnose", action="store_true",
                        help="explain a Search Console 403 and exit")
    args = parser.parse_args(argv)

    if args.diagnose:
        return diagnose_gsc()

    if not (GSC_KEY or BING_KEY or PSI_KEY):
        log("No credentials set (GSC_KEY, BINGWEBMASTER_KEY, PSI_KEY) - "
            "nothing to fetch.")
        return 1

    report = build(only=args.only)

    for line in summarise(report):
        log(line)

    if args.dry_run:
        log("--dry-run: not written.")
        return 0

    path = os.path.join(out_dir(), f"{date.today().isoformat()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, separators=(",", ":"))
    log(f"-> {path} ({os.path.getsize(path)} bytes)")

    # Once a week, on the first run of each ISO week. Rides on this job rather
    # than having a weekly cron line of its own - the data only arrives here,
    # and a second entry is another thing that can silently not be installed.
    try:
        if args.digest:
            import ops
            log("  --digest: pushing now")
            ops.notify("seo", digest(report))
        else:
            send_weekly_digest(report)
    except Exception as e:
        log(f"  weekly digest FAILED: {e}")

    # Exit 0 even when a source failed. The report records the failure, and a
    # non-zero exit from cron on a partial success trains you to ignore it.
    return 0


if __name__ == "__main__":
    # See the note in indexnow.py: this records a heartbeat so `jobs.py status`
    # can tell "the nightly SEO pull is fine" from "it has not run since March".
    import ops
    sys.exit(ops.tracked("seo-report", main))
