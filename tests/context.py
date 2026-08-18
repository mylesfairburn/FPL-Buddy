"""Shared app fixture.

Imported by the suites for `client`. Boot happens once for the whole run: the
startup event loads the pipeline and rates every player, which takes tens of
seconds, and doing it per suite would make the run useless as a pre-commit gate.

The test database is a throwaway file. Without this the draft round-trip and
the injection cases would be writing into the real state/ volume.
"""

import os
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "fpl_test_state.db")
os.environ["FPL_DB_PATH"] = _TMP_DB

import main  # noqa: E402  - must come after FPL_DB_PATH is set
from fastapi.testclient import TestClient  # noqa: E402

# TestClient only fires startup when used as a context manager, and without
# startup the player pool is empty and most of the suite is meaningless.
#
# raise_server_exceptions=False is the important flag: by default TestClient
# re-raises an unhandled handler exception in the caller, which aborts the
# suite. What we want is what a real client would see - a 500 - recorded as a
# failed row alongside everything else.
_ctx = TestClient(main.app, raise_server_exceptions=False)
client = _ctx.__enter__()

# Startup does not load the ratings - it kicks off a background thread and
# returns, so the container answers within seconds of a deploy instead of being
# unreachable for a minute (see main.READY). So the app is LISTENING here but
# not yet READY, and every route needing projections answers 503 until it is.
# A blocking startup would have given the suite this wait for free.
#
# Failing loudly on a timeout rather than carrying on: a run that proceeded
# would report several hundred spurious 503s, which reads as "the app is
# broken" instead of "the fixture didn't wait".
_WARMUP_TIMEOUT = 300
if not main.READY.wait(timeout=_WARMUP_TIMEOUT):
    raise RuntimeError(
        f"The app was still warming up after {_WARMUP_TIMEOUT}s. Every rated "
        "route would answer 503, so the suite would be measuring the fixture "
        "rather than the app.")


def teardown():
    try:
        _ctx.__exit__(None, None, None)
    except Exception:
        pass
    try:
        os.remove(_TMP_DB)
    except OSError:
        pass


def db_path():
    return _TMP_DB
