"""Tiny test harness.

pytest isn't installed in this environment and the report the suite has to
produce - every case with its INPUT and its OUTPUT side by side - isn't
something pytest prints anyway. So the runner is 120 lines of its own.

A test is a function that calls check()/expect() as many times as it likes.
Each call records one row. Rows carry:

    group     which suite it came from
    name      what is being asserted
    inp       the exact input (a URL, a payload, an argument)
    expected  what should have happened
    actual    what did happen
    ok        pass/fail
    severity  how much a failure matters - see SEVERITY

Nothing here is FPL-specific; the suites are.
"""

import json
import time
import traceback

# Ordered worst-first, so a report can sort by it.
SEVERITY = ["critical", "high", "medium", "low", "info"]

ROWS = []
_current = {"group": "?", "severity": "medium"}


def group(name, default_severity="medium"):
    _current["group"] = name
    _current["severity"] = default_severity


def _trim(value, limit=220):
    """Report cells hold one line. Long payloads get elided in the middle so
    both ends stay visible - the end is usually where the interesting part of
    an HTML response or a stack trace is."""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            value = repr(value)
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    keep = (limit - 5) // 2
    return f"{value[:keep]} ... {value[-keep:]}"


def record(name, inp, expected, actual, ok, severity=None, note=""):
    ROWS.append({
        "group": _current["group"],
        "name": name,
        "input": _trim(inp),
        "expected": _trim(expected),
        "actual": _trim(actual),
        "ok": bool(ok),
        "severity": severity or _current["severity"],
        "note": note,
    })
    return ok


def safe(fn, *args, **kwargs):
    """Call fn and return its result, or the exception as a string.

    Needed because the value passed to check() is evaluated before check()
    runs, so a function that raises would escape the harness and abort the
    whole suite instead of recording one failed row."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def check(name, inp, expected_desc, actual_value, predicate, severity=None, note=""):
    """Assert predicate(actual_value). Never raises - a failed case is data,
    not an abort, because the point of the run is the whole table."""
    try:
        ok = bool(predicate(actual_value))
    except Exception as e:
        ok = False
        actual_value = f"{type(e).__name__}: {e}"
    return record(name, inp, expected_desc, actual_value, ok, severity, note)


def expect(name, inp, expected, actual, severity=None, note=""):
    """Equality shorthand."""
    return record(name, inp, expected, actual, actual == expected, severity, note)


def run(suites):
    """Run every suite function, catching anything that escapes so one broken
    suite can't take the report down with it."""
    started = time.time()
    for fn in suites:
        try:
            fn()
        except Exception:
            group(getattr(fn, "__name__", "unknown"))
            record("suite crashed", getattr(fn, "__name__", "?"),
                   "suite runs to completion",
                   _trim(traceback.format_exc(), 400), False, "critical")
    return time.time() - started


def summary():
    total = len(ROWS)
    failed = [r for r in ROWS if not r["ok"]]
    by_group = {}
    for r in ROWS:
        g = by_group.setdefault(r["group"], {"total": 0, "failed": 0})
        g["total"] += 1
        if not r["ok"]:
            g["failed"] += 1
    by_sev = {s: len([r for r in failed if r["severity"] == s]) for s in SEVERITY}
    return {
        "total": total,
        "passed": total - len(failed),
        "failed": len(failed),
        "by_group": by_group,
        "failures_by_severity": by_sev,
    }
