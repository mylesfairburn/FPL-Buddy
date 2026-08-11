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

# api.indexnow.org forwards to every participating engine, so submitting once
# here is the same as submitting to each of them separately.
ENDPOINT = "https://api.indexnow.org/indexnow"

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


def sitemap_urls():
    """Every <loc> in the live sitemap."""
    with urllib.request.urlopen(f"{SITE_URL}/sitemap.xml", timeout=60) as fh:
        xml = fh.read().decode("utf-8")
    return re.findall(r"<loc>(.*?)</loc>", xml)


def submit(urls):
    """POST one batch. Returns the HTTP status."""
    host = SITE_URL.split("//", 1)[-1]
    payload = json.dumps({
        "host": host,
        "key": KEY,
        "keyLocation": f"{SITE_URL}/{KEY}.txt",
        "urlList": urls,
    }).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
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
    try:
        with urllib.request.urlopen(f"{SITE_URL}/{KEY}.txt", timeout=30) as fh:
            served = fh.read().decode("utf-8").strip()
        if served != KEY:
            sys.exit(f"Key file does not contain the key (got {served!r}).")
        print("Key file verified.")
    except urllib.error.HTTPError as e:
        sys.exit(f"Key file not reachable ({e.code}). Is FPL_INDEXNOW_KEY set "
                 f"on the server and the container restarted?")

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
