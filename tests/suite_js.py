"""The JavaScript suites, folded into this report.

The front end had no automated tests at all: `compare.js` and the watchlist
were verified by driving a real browser, which catches things a stub never
will but is a manual step and therefore not a regression guard. This runs
tests/js/run.js under Node and copies its rows into the same table everything
else here writes to, so a broken front end fails the same gate.

Why the rows are ingested rather than the JS being run as a separate command:
a second command is a command that stops being run. The CI job invokes
`python tests/run_tests.py` and nothing else, so anything not reachable from
here is not actually gating a deploy.

Node is not a hard requirement. A machine without it records one `info` row
saying the JS suites were skipped, rather than failing a Python developer's
run for a toolchain they may not have - but see the note on that row: CI does
have Node, so a skip there would be hiding something.
"""

import json
import os
import shutil
import subprocess

from harness import ROWS, check, expect, group, record

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNNER = os.path.join(HERE, "js", "run.js")

# Generous: the whole JS run is a few hundred milliseconds, so anything near
# this means something is hanging rather than working slowly.
TIMEOUT_S = 120


def _node():
    """The node executable, or None. `shutil.which` rather than trying to run
    it and catching, so a missing interpreter is not confused with a crash."""
    return shutil.which("node")


def test_javascript_suites():
    group("javascript", "high")

    node = _node()
    if not node:
        record("the JavaScript suites ran", "node tests/js/run.js",
               "node is available", "node not found on PATH", True, "info",
               note="skipped locally; CI has Node, so a skip THERE would mean "
                    "the front-end tests silently stopped gating a deploy")
        return

    check("the JS test runner exists", RUNNER, "tests/js/run.js present",
          os.path.exists(RUNNER), lambda v: v is True, severity="high")
    if not os.path.exists(RUNNER):
        return

    try:
        proc = subprocess.run([node, RUNNER], capture_output=True, text=True,
                              cwd=ROOT, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        record("the JavaScript suites complete", "node tests/js/run.js",
               f"finishes inside {TIMEOUT_S}s", "timed out", False, "critical")
        return

    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        record("the JavaScript suites emit readable results",
               "node tests/js/run.js", "JSON on stdout",
               (proc.stdout or proc.stderr or "")[:400], False, "critical",
               note="the runner is expected to print one JSON object; anything "
                    "else means it died before it could")
        return

    rows = payload.get("rows") or []
    check("the JavaScript suites produced results", "node tests/js/run.js",
          "at least one row", len(rows), lambda n: n > 0, severity="high",
          note="zero rows means the suites did not run, which is not the same "
               "as everything passing")

    # Copied in with their own group names, so a failure reads as
    # "compare: safety" in the report rather than as one opaque JS row.
    for row in rows:
        ROWS.append({
            "group": row.get("group", "javascript"),
            "name": row.get("name", "?"),
            "input": row.get("input", ""),
            "expected": row.get("expected", ""),
            "actual": row.get("actual", ""),
            "ok": bool(row.get("ok")),
            "severity": row.get("severity", "medium"),
            "note": row.get("note", ""),
        })

    group("javascript", "high")
    expect("node exits cleanly", "node tests/js/run.js", 0, proc.returncode,
           severity="medium",
           note="the rows above carry the detail; this catches the runner "
                "itself falling over after printing them")


def test_javascript_sources_parse():
    """Every shipped script is syntactically valid.

    Cheap, and it catches the one class of front-end break that takes the whole
    page down rather than one feature: a syntax error means the browser runs
    none of the file, so a stray bracket in the watchlist would silently stop
    the pitch rendering too.
    """
    group("javascript syntax", "critical")

    node = _node()
    if not node:
        record("shipped scripts parse", "node --check", "node available",
               "node not found on PATH", True, "info")
        return

    static = os.path.join(ROOT, "python", "static")
    scripts = sorted(f for f in os.listdir(static) if f.endswith(".js"))
    check("there are scripts to check", static, "at least one .js file",
          len(scripts), lambda n: n > 0)

    for name in scripts:
        proc = subprocess.run([node, "--check", os.path.join(static, name)],
                              capture_output=True, text=True, timeout=60)
        expect(f"{name} parses", f"node --check static/{name}", 0,
               proc.returncode, severity="critical",
               note=(proc.stderr or "").strip().split("\n")[0][:180])


SUITES = [test_javascript_suites, test_javascript_sources_parse]
