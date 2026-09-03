"""
Test session setup.

`prism.config.SETTINGS` is a snapshot taken when the module is first imported,
which means any test module that sets PRISM_HOME at import time is racing every
other test module for who imports first. That worked while there was one test
file and broke the moment there were four. Configuring the environment here --
before pytest imports a single test module -- is the fix, and it also keeps the
suite from ever touching a real ~/.prism.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

_HOME = tempfile.mkdtemp(prefix="sunroom-test-")

os.environ.update(
    PRISM_HOME=_HOME,
    PRISM_PROVIDER="mock",
    SUNROOM_ENV="test",
    SUNROOM_STORE="sqlite",
    # Long enough for the key vault; fixed so an encrypted value written by one
    # test can be read by another in the same run.
    SUNROOM_SECRET_KEY="test-secret-key-that-is-long-enough-for-hkdf-0123456789",
    SUNROOM_WORKER_SECRET="test-worker-secret",
    # No background worker during tests.
    #
    # The app starts one whenever the process is long-lived, which includes
    # every TestClient -- so a thread was draining the shared queue behind the
    # whole suite. Tests that expect to drive a job would sometimes find it
    # already claimed: /api/worker/run answered "ran: 0", the loop exited, and
    # the job was still mid-slice in another thread. That is a test failing at
    # random, in a different place each run, for a reason nothing in the test
    # points at.
    #
    # Tests call drain() when they want work done, which is also the only way
    # a test can honestly claim the thing it drove is the thing that ran.
    SUNROOM_INLINE_WORKER="0",
)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DATABASE_URL", None)

from prism import config  # noqa: E402

config.reload()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


@pytest.fixture()
def settings(monkeypatch):
    """Mutate settings for one test without leaking into the next."""
    from prism.config import SETTINGS
    return SETTINGS


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """A fresh PRISM_HOME (and therefore a fresh SQLite file) for one test."""
    from prism.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "home", tmp_path)
    return tmp_path
