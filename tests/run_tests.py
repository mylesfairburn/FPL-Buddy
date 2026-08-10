"""Run every suite and write the report.

    python tests/run_tests.py

Outputs, all into tests/reports/:

    report.md     the table, grouped, failures first
    report.html   the same thing styled, for reading or screenshotting
    results.json  machine-readable, for a CI gate

Exit code is 1 if anything critical or high failed, 0 otherwise - medium and
low failures are reported but don't block, because several of them are known
hardening gaps rather than defects.
"""

import datetime
import html
import json
import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))
# Relative paths inside main.py (templates/, static/, ../state/) are resolved
# from the process cwd, so the run has to happen from python/.
os.chdir(os.path.join(ROOT, "python"))

import harness  # noqa: E402
from harness import ROWS, SEVERITY  # noqa: E402

REPORTS = os.path.join(HERE, "reports")

SEV_COLOUR = {
    "critical": "#b91c1c", "high": "#c2410c", "medium": "#a16207",
    "low": "#3f6212", "info": "#475569",
}


def _load_suites():
    import context  # boots the app  # noqa: F401
    import suite_units, suite_routes, suite_api, suite_security
    return (suite_units.SUITES + suite_routes.SUITES
            + suite_api.SUITES + suite_security.SUITES), context


def _md_cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def write_markdown(path, meta, summary):
    lines = [
        "# FPL Buddy — test report",
        "",
        f"**Run:** {meta['when']} · **Duration:** {meta['duration']:.1f}s · "
        f"**Python:** {meta['python']} · **Platform:** {meta['platform']}",
        "",
        f"**{summary['passed']} passed · {summary['failed']} failed · "
        f"{summary['total']} total**",
        "",
        "## Failures by severity",
        "",
        "| Severity | Failures |",
        "| --- | --- |",
    ]
    for sev in SEVERITY:
        lines.append(f"| {sev} | {summary['failures_by_severity'][sev]} |")

    lines += ["", "## By group", "", "| Group | Passed | Failed | Total |",
              "| --- | ---: | ---: | ---: |"]
    for g, s in summary["by_group"].items():
        lines.append(f"| {g} | {s['total'] - s['failed']} | {s['failed']} | {s['total']} |")

    failures = [r for r in ROWS if not r["ok"]]
    if failures:
        failures.sort(key=lambda r: SEVERITY.index(r["severity"]))
        lines += ["", "## Failures", "",
                  "| # | Severity | Group | Test | Input | Expected | Actual | Note |",
                  "| ---: | --- | --- | --- | --- | --- | --- | --- |"]
        for i, r in enumerate(failures, 1):
            lines.append("| {} | **{}** | {} | {} | `{}` | {} | {} | {} |".format(
                i, r["severity"], _md_cell(r["group"]), _md_cell(r["name"]),
                _md_cell(r["input"]), _md_cell(r["expected"]),
                _md_cell(r["actual"]), _md_cell(r["note"])))

    lines += ["", "## All cases", "",
              "| # | Result | Severity | Group | Test | Input | Expected | Actual |",
              "| ---: | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(ROWS, 1):
        lines.append("| {} | {} | {} | {} | {} | `{}` | {} | {} |".format(
            i, "PASS" if r["ok"] else "**FAIL**", r["severity"],
            _md_cell(r["group"]), _md_cell(r["name"]), _md_cell(r["input"]),
            _md_cell(r["expected"]), _md_cell(r["actual"])))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_html(path, meta, summary):
    def esc(v):
        return html.escape(str(v))

    rows = sorted(ROWS, key=lambda r: (r["ok"], SEVERITY.index(r["severity"])))
    body = []
    for i, r in enumerate(rows, 1):
        cls = "pass" if r["ok"] else "fail"
        body.append(
            f'<tr class="{cls}"><td>{i}</td>'
            f'<td class="res">{"PASS" if r["ok"] else "FAIL"}</td>'
            f'<td><span class="sev" style="background:{SEV_COLOUR[r["severity"]]}">'
            f'{esc(r["severity"])}</span></td>'
            f'<td>{esc(r["group"])}</td><td>{esc(r["name"])}</td>'
            f'<td><code>{esc(r["input"])}</code></td>'
            f'<td>{esc(r["expected"])}</td><td><code>{esc(r["actual"])}</code></td>'
            f'<td class="note">{esc(r["note"])}</td></tr>')

    sev_rows = "".join(
        f'<tr><td><span class="sev" style="background:{SEV_COLOUR[s]}">{s}</span></td>'
        f'<td class="num">{summary["failures_by_severity"][s]}</td></tr>'
        for s in SEVERITY)
    group_rows = "".join(
        f'<tr><td>{esc(g)}</td><td class="num">{s["total"] - s["failed"]}</td>'
        f'<td class="num {"bad" if s["failed"] else ""}">{s["failed"]}</td>'
        f'<td class="num">{s["total"]}</td></tr>'
        for g, s in summary["by_group"].items())

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>FPL Buddy — test report</title>
<style>
 body {{ font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
        margin: 0; padding: 32px; background: #f8fafc; color: #0f172a; }}
 h1 {{ margin: 0 0 4px; font-size: 26px; }}
 h2 {{ margin: 32px 0 10px; font-size: 18px; }}
 .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
 .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }}
 .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
          padding: 14px 20px; min-width: 110px; }}
 .card .n {{ font-size: 28px; font-weight: 700; }}
 .card .l {{ color: #64748b; font-size: 12px; text-transform: uppercase;
             letter-spacing: .04em; }}
 .card.ok .n {{ color: #15803d; }} .card.no .n {{ color: #b91c1c; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff;
          border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden;
          font-size: 13px; }}
 th {{ background: #f1f5f9; text-align: left; padding: 9px 10px;
       font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
       color: #475569; position: sticky; top: 0; }}
 td {{ padding: 8px 10px; border-top: 1px solid #f1f5f9; vertical-align: top; }}
 td.num {{ text-align: right; }} td.num.bad {{ color: #b91c1c; font-weight: 700; }}
 code {{ font: 12px ui-monospace, Consolas, monospace; color: #334155;
         word-break: break-word; }}
 tr.fail {{ background: #fef2f2; }}
 tr.fail td.res {{ color: #b91c1c; font-weight: 700; }}
 tr.pass td.res {{ color: #15803d; font-weight: 600; }}
 .sev {{ color: #fff; padding: 1px 7px; border-radius: 99px; font-size: 11px;
         text-transform: uppercase; letter-spacing: .03em; }}
 .note {{ color: #64748b; max-width: 280px; }}
 .wrap {{ max-width: 1600px; margin: 0 auto; }}
</style></head><body><div class="wrap">
<h1>FPL Buddy — test report</h1>
<div class="meta">{esc(meta['when'])} · {meta['duration']:.1f}s ·
 Python {esc(meta['python'])} · {esc(meta['platform'])}</div>
<div class="cards">
 <div class="card ok"><div class="n">{summary['passed']}</div><div class="l">Passed</div></div>
 <div class="card no"><div class="n">{summary['failed']}</div><div class="l">Failed</div></div>
 <div class="card"><div class="n">{summary['total']}</div><div class="l">Total</div></div>
 <div class="card"><div class="n">{len(summary['by_group'])}</div><div class="l">Groups</div></div>
</div>
<h2>Failures by severity</h2>
<table><tr><th>Severity</th><th>Failures</th></tr>{sev_rows}</table>
<h2>By group</h2>
<table><tr><th>Group</th><th>Passed</th><th>Failed</th><th>Total</th></tr>{group_rows}</table>
<h2>Every case <span style="font-weight:400;color:#64748b">(failures first)</span></h2>
<table><tr><th>#</th><th>Result</th><th>Severity</th><th>Group</th><th>Test</th>
<th>Input</th><th>Expected</th><th>Actual</th><th>Note</th></tr>{''.join(body)}</table>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def main_():
    os.makedirs(REPORTS, exist_ok=True)
    print("Booting the app (loads and rates the full player pool)...")
    suites, context = _load_suites()
    print(f"Running {len(suites)} suites...")
    duration = harness.run(suites)
    context.teardown()

    summary = harness.summary()
    meta = {
        "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration": duration,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
    }

    write_markdown(os.path.join(REPORTS, "report.md"), meta, summary)
    write_html(os.path.join(REPORTS, "report.html"), meta, summary)
    with open(os.path.join(REPORTS, "results.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "summary": summary, "rows": ROWS}, fh, indent=2)

    print(f"\n{summary['passed']} passed, {summary['failed']} failed, "
          f"{summary['total']} total, in {duration:.1f}s")
    for sev in SEVERITY:
        n = summary["failures_by_severity"][sev]
        if n:
            print(f"  {sev}: {n} failed")
    print(f"\nReports in {REPORTS}")

    blocking = (summary["failures_by_severity"]["critical"]
                + summary["failures_by_severity"]["high"])
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main_())
