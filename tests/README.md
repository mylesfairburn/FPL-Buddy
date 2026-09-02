# Tests

```bash
python tests/run_tests.py
```

Boots the app once (loads and rates the full player pool — about 25s), runs
every suite against an in-process client, and writes three files to
`tests/reports/`:

| File | What it is |
| --- | --- |
| `report.html` | The full table, failures first, styled for reading |
| `report.md` | The same table in Markdown |
| `results.json` | Every row, for a CI gate or a diff between runs |

Exit code is **1** if anything `critical` or `high` failed, **0** otherwise.
`medium`/`low`/`info` failures are reported but don't block — several are known
gaps that are deliberately open (see the notes column).

## Layout

| File | Covers |
| --- | --- |
| `harness.py` | The runner. Records input/expected/actual per case; nothing FPL-specific |
| `context.py` | Boots the app once, against a throwaway SQLite file |
| `suite_units.py` | Pure functions: slugs, prose helpers, draft validation |
| `suite_routes.py` | Routing, HTML, SEO, sitemap, player pages, static assets |
| `suite_api.py` | JSON contracts, parameters, the draft round-trip |
| `suite_security.py` | Injection, XSS, traversal, auth, headers, disclosure, limits |
| `suite_js.py` | Runs the JavaScript suites under Node and folds their rows into this report |
| `js/` | The front-end tests — see below |

## Notes

- `context.py` sets `FPL_DB_PATH` to a temp file before importing `main`, so a
  run never touches `state/fpl_companion.db`.
- The client is built with `raise_server_exceptions=False`. A handler that
  throws is recorded as the 500 a real user would see, rather than aborting the
  run — which is the point of a robustness suite.
- Draft tests use FPL ids in the 999999xxx range, far outside the real range,
  so they can't collide with a genuine manager's saved squad.
- Some endpoints proxy the live FPL API. Those cases assert shape and
  "never 5xx" rather than values, so the suite still passes offline.
- Adding a case: call `check()` or `expect()` from `harness`. Pass the value
  through `safe()` if producing it might raise.

## The JavaScript suites

```bash
node tests/js/run.js        # on their own, printing JSON
python tests/run_tests.py   # normally - suite_js.py folds them into the report
```

`compare.js` and `watchlist.js` are loaded and executed as shipped, in a
minimal DOM (`js/dom.js`, ~200 lines, no dependencies — the project has no
build step and no `node_modules`, and adding one to run three files would be a
larger change to the project than the tests are worth).

| File | Covers |
| --- | --- |
| `js/dom.js` | The stub DOM and browser globals |
| `js/harness.js` | Mirrors `harness.py` — same row shape, same severities |
| `js/test_compare.js` | The comparison page: URL state, the numbers, escaping |
| `js/test_watchlist.js` | The watchlist: loading, toggling, failures, escaping |

**What these do and do not prove.** The elements are real objects with real
state — assigning `innerHTML` stores markup that `querySelectorAll` then
genuinely scans — so a test cannot pass against a page that builds nothing.
What they cannot see is layout, CSS, paint, or real event ordering. Anything
visual still has to be checked in a browser.

**They were mutation-tested.** Twelve plausible mistakes were introduced into
the two source files in turn — escaping removed, the price column marked the
wrong way round, `pushState` for `replaceState`, the URL keyed on `element_id`
instead of `code`, a server refusal swallowed, departed players silently
dropped — and each was caught by the case that should catch it. A suite that
passes on its first run has not yet proved it can fail.

**Node is optional locally, required in CI.** Without it `suite_js.py` records
one `info` row and moves on, so a Python developer is not blocked. The CI job
installs Node explicitly (`actions/setup-node`) rather than relying on the
runner image, because that skip is quiet by design and would otherwise hide the
front-end tests silently ceasing to gate a deploy.
