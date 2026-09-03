"""
The job worker, as a serverless function.

Two things drive it:

  * **Cron**, on the schedule in vercel.json. That is the floor -- it guarantees
    a stalled or newly queued job is picked up eventually, even if nothing else
    happens.
  * **Chaining.** After a slice, if the job still has work to do, this function
    calls itself again. That is what turns "one minute of compute per
    invocation" into "however long the document needs", without anyone waiting
    for the next cron tick.

The chain is fire-and-forget with a short timeout: we are not waiting for the
next slice to finish, only asking for it to start. And it is bounded by the
job's own attempt ceiling, so a job that fails forever cannot become an
invocation loop that bills forever.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prism.config import SETTINGS  # noqa: E402
from prism.jobs.runner import run_slice  # noqa: E402


def _self_url() -> str:
    host = os.environ.get("VERCEL_URL") or os.environ.get("SUNROOM_HOST", "")
    if not host:
        return ""
    if not host.startswith("http"):
        host = "https://" + host
    return f"{host}/api/worker"


def _chain() -> None:
    """Ask for another slice. Failure here is not an error: cron is the floor."""
    url = _self_url()
    if not url:
        return
    try:
        import httpx
        httpx.post(url, timeout=2.0, trust_env=False,
                   headers={"x-worker-secret": SETTINGS.worker_secret})
    except Exception:                                          # noqa: S110
        # Chaining is an optimisation over the cron schedule. If it does not
        # go through, the job is picked up on the next tick regardless.
        pass


def handle() -> dict:
    result = run_slice()
    if result is None:
        return {"ran": 0, "idle": True}
    if result.more_to_do:
        _chain()
    return {"ran": 1, "result": result.to_dict()}


class handler(BaseHTTPRequestHandler):        # noqa: N801  (Vercel's contract)
    def _authorized(self) -> bool:
        import hmac
        expected = SETTINGS.worker_secret
        if not expected:
            return False
        supplied = (self.headers.get("x-worker-secret")
                    or (self.headers.get("authorization") or "")
                    .removeprefix("Bearer ").strip())
        return bool(supplied) and hmac.compare_digest(str(supplied), str(expected))

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:                 # noqa: N802
        if not self._authorized():
            return self._reply(401, {"detail": "Not authorized."})
        try:
            self._reply(200, handle())
        except Exception as exc:               # noqa: BLE001
            self._reply(500, {"detail": f"{type(exc).__name__}"})

    def do_GET(self) -> None:                  # noqa: N802
        # Vercel Cron issues GETs.
        self.do_POST()

    def log_message(self, *args) -> None:      # keep the platform log readable
        pass
