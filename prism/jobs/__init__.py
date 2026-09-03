"""Long work, done in slices that fit inside a serverless invocation."""
from .queue import CANCELLED, DONE, FAILED, LIVE, QUEUED, RUNNING, Job, Jobs, jobs
from .runner import SliceResult, drain, run_slice

__all__ = ["Job", "Jobs", "jobs", "run_slice", "drain", "SliceResult",
           "QUEUED", "RUNNING", "DONE", "FAILED", "CANCELLED", "LIVE"]
