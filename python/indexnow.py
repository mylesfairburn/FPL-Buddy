"""Push every published URL to IndexNow.

IndexNow is supported by Bing, Yandex, Seznam and Naver. Google has never
adopted it, so running this does nothing for Google - use Search Console's URL
Inspection for that, one URL at a time.

    FPL_INDEXNOW_KEY=<key> python indexnow.py            # submit everything
    FPL_INDEXNOW_KEY=<key> python indexnow.py --dry-run  # print, submit nothing
    FPL_INDEXNOW_KEY=<key> python indexnow.py https://fpl.mfhost.co.uk/players

The URL list is read from the live sitemap rather than built by importing the
app. That keeps the script cheap - no model load, no data pipeline - and means
it can only ever submit URLs the site is actually publishing, which is the
thing IndexNow rejects a whole batch for.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

SITE_URL = os.environ.get("FPL_SITE_URL", "https://fpl.mfhost.co.uk").rstrip("/")
KEY = os.environ.get("FPL_INDEXNOW_KEY", "").strip()

# The site sits behind Cloudflare, which 403s urllib's default User-Agent
# ("Python-urllib/3.13") as a bot. Nothing is wrong with the request - the same
# URL returns 200 the moment a real one is sent - so every outbound call here
# identifies itself properly.
USER_AGENT = "FPLBuddy-IndexNow/1.0 (+https://fpl.mfhost.co.uk)"

# Set inside the container, so a run via `docker exec` can read the sitemap
# from the app directly instead of leaving the host and coming back in through
# the CDN. Unset (a laptop run) the public URL is used instead.
APP_URL = os.environ.get("FPL_APP_URL", "").rstrip("/")

# api.indexnow.org forwards to every participating engine, so submitting once
# here is the same as submitting to each of them separately. Overridable
# because the shared endpoint and Bing's own endpoint do not always agree about
# a key: when one returns 403 it is worth asking the other before assuming the
# key file is wrong.
#   https://www.bing.com/indexnow
#   https://yandex.com/indexnow
ENDPOINT = os.environ.get("FPL_INDEXNOW_ENDPOINT",
                          "https://api.indexnow.org/indexnow")

# The protocol's own cap on one request. The site is well under it today; the
# batching below exists so that stays true without anyone having to notice.
BATCH = 10000

# What the response codes actually mean. IndexNow returns 200 with an empty
# body on success, so without this table a failure is indistinguishable from a
# success at a glance.
MEANING = {
    200: "OK - URLs received.",
    202: "Accepted - received, but the key is still being validated. "
         "Check the key file is reachable.",
    400: "Bad request - malformed JSON or a missing field.",
    403: "Forbidden - the key file did not verify. Confirm it is served at the "
         "root of the host and contains exactly the key.",
    422: "Unprocessable - the URLs do not all belong to the host, or the key "
         "does not match the one in the key file.",
    429: "Too many requests - slow down and retry later.",
}


def fetch(url, data=None, content_type=None):
    """urlopen with a User-Agent Cloudflare will accept."""
    headers = {"User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers), timeout=60)


def sitemap_urls():
    """Every <loc> in the live sitemap.

    Read over APP_URL when there is one: the <loc> values are absolute and
    built from SITE_URL either way, so the URLs submitted are identical, but
    the request never leaves the host."""
    base = APP_URL or SITE_URL
    with fetch(f"{base}/sitemap.xml") as fh:
        xml = fh.read().decode("utf-8")
    return re.findall(r"<loc>(.*?)</loc>", xml)


def submit(urls):
    """POST one batch. Returns the HTTP status."""
    host = SITE_URL.split("//", 1)[-1]
    # No keyLocation. It exists to point at a key file kept somewhere other
    # than the default, and ours is at exactly the default - the root of the
    # host, named <key>.txt. Sending it anyway is one more field for a receiver
    # to disagree about while telling us only that "the key did not verify".
    payload = json.dumps({
        "host": host,
        "key": KEY,
        "urlList": urls,
    }).encode("utf-8")
    try:
        with fetch(ENDPOINT, data=payload,
                   content_type="application/json; charset=utf-8") as response:
            return response.status
    except urllib.error.HTTPError as e:
        # A non-2xx is the normal way IndexNow reports a bad key, so it's a
        # result to report rather than a crash.
        return e.code


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv

    if not KEY:
        sys.exit("FPL_INDEXNOW_KEY is not set. Generate a key in Bing Webmaster "
                 "Tools (IndexNow section) and export it before running.")

    urls = args or sitemap_urls()
    if not urls:
        sys.exit("No URLs to submit.")

    print(f"{len(urls)} URLs, key file {SITE_URL}/{KEY}.txt")

    # Submitting a key that isn't being served is the single most common way
    # this fails, and it fails with a 403 that reads like a bad key rather than
    # a missing file. Checking first turns that into a clear message.
    # Checked over the public URL, never APP_URL: what matters is what the
    # search engine can fetch, and an internal request proves nothing about it.
    try:
        with fetch(f"{SITE_URL}/{KEY}.txt") as fh:
            served = fh.read().decode("utf-8").strip()
        if served != KEY:
            sys.exit(f"Key file does not contain the key (got {served!r}).")
        print("Key file verified.")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit("Key file 404s. FPL_INDEXNOW_KEY is not reaching the "
                     "container - check `docker compose config | grep INDEXNOW` "
                     "and that the container was recreated, not just started.")
        # Anything else is the CDN talking, not the app: a 403 here means the
        # request was blocked before it reached the origin, which says nothing
        # about whether the key file exists. Worth reporting, not worth
        # refusing to submit over - the engine fetches it from its own network.
        print(f"Could not verify the key file ({e.code}) - most likely the CDN "
              f"blocking this request rather than a missing file. Continuing.")

    if dry_run:
        for u in urls[:10]:
            print("  ", u)
        if len(urls) > 10:
            print(f"   ... and {len(urls) - 10} more")
        print("Dry run - nothing submitted.")
        return

    failed = False
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i + BATCH]
        status = submit(batch)
        print(f"batch {i // BATCH + 1}: {len(batch)} URLs -> HTTP {status} "
              f"{MEANING.get(status, '')}")
        if status not in (200, 202):
            failed = True

    # Non-zero on failure so this is safe to put in cron without the failure
    # being invisible - the same trap the deadline-watch job fell into.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
