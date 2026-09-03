"""
vercel.json, checked against the platform's actual rules.

None of this can be caught by running the app: it is only discovered by pushing
to Vercel and reading a build error, which is a slow and public way to learn.
Each assertion here corresponds to a real failed deployment.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg() -> dict:
    return json.loads((ROOT / "vercel.json").read_text())


def test_functions_do_not_name_a_runtime(cfg):
    """`runtime` is for versioned community runtimes (@vercel/python@4.3.0).

    Naming a built-in one -- "python3.12" -- fails the build with the
    memorably unhelpful "Function Runtimes must have a valid version, for
    example `now-php@1.0.0`". Built-in runtimes are inferred from the file
    extension, so the correct value is no key at all.
    """
    named = {k: v["runtime"] for k, v in cfg["functions"].items() if "runtime" in v}
    assert not named, (
        f"remove the runtime key: {named}. Version is set by .python-version.")


def test_functions_do_not_set_memory(cfg):
    """Memory cannot be configured from vercel.json on any plan.

    It warns at build time and is ignored; Pro sets it in the dashboard and
    Hobby is fixed at 2 GB / 1 vCPU. A number here is a lie about what the
    deployment does.
    """
    sized = {k: v["memory"] for k, v in cfg["functions"].items() if "memory" in v}
    assert not sized, f"memory is not settable in vercel.json: {sized}"


def test_max_duration_is_within_the_hobby_ceiling(cfg):
    # Hobby: 300s is both the default and the maximum, with fluid compute.
    over = {k: v["maxDuration"] for k, v in cfg["functions"].items()
            if v.get("maxDuration", 0) > 300}
    assert not over, f"exceeds the Hobby maximum of 300s: {over}"


def test_the_cron_runs_at_most_once_a_day(cfg):
    """Hobby allows one cron a day and REFUSES THE DEPLOYMENT if asked for more.

    Not a warning, not a silent downgrade -- the whole deploy fails. Anything
    with a wildcard in the minute, hour, or day-of-month field runs more often
    than daily.
    """
    for cron in cfg.get("crons", []):
        minute, hour, dom, month, dow = cron["schedule"].split()
        assert minute.isdigit(), f"{cron['schedule']}: minute must be fixed"
        assert hour.isdigit(), f"{cron['schedule']}: hour must be fixed"
        assert dom == "*" or dom.isdigit(), cron["schedule"]


def test_a_slice_fits_inside_the_worker_invocation(cfg):
    """A slice that outlives its function is killed mid-write every time."""
    from prism.config import SETTINGS
    limit = cfg["functions"]["api/worker.py"]["maxDuration"]
    assert SETTINGS.slice_seconds < limit, (
        f"SUNROOM_SLICE_SECONDS={SETTINGS.slice_seconds} >= maxDuration={limit}")


def test_the_poll_nudge_fits_inside_a_request(cfg):
    from prism.config import SETTINGS
    limit = cfg["functions"]["api/index.py"]["maxDuration"]
    assert SETTINGS.poll_nudge_seconds < limit


def test_every_declared_function_exists(cfg):
    for path in cfg["functions"]:
        assert (ROOT / path).is_file(), f"vercel.json names a missing file: {path}"


def test_the_entrypoints_expose_what_vercel_looks_for(cfg):
    # index.py must expose `app` (ASGI); worker.py a `handler` class.
    assert "from prism.web.api import app" in (ROOT / "api/index.py").read_text()
    assert "class handler" in (ROOT / "api/worker.py").read_text()


def test_the_python_version_is_pinned_and_supported(cfg):
    # Vercel offers 3.12 (default), 3.13, 3.14. Unpinned means "whatever the
    # default is today", which is a silent upgrade waiting to happen.
    pin = (ROOT / ".python-version").read_text().strip()
    assert pin in {"3.12", "3.13", "3.14"}, f"unsupported on Vercel: {pin}"
