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
