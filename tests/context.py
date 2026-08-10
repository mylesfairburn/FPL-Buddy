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
