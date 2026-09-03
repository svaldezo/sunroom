"""
Runtime configuration.

Everything is read from the environment, because the two places this runs -- a
Vercel function and a container -- both configure that way and neither has a
writable config file. `SETTINGS` is a snapshot; `reload()` re-reads the
environment into that same object, so callers holding a reference see the
change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

DEV = "development"
PROD = "production"


def _default_home() -> Path:
    # Serverless filesystems are read-only apart from /tmp.
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        # Not a guess: /tmp is the only writable directory in a Vercel or
        # Lambda function, and nothing durable is kept there -- the store is
        # Postgres in that deployment.
        return Path("/tmp/prism")                               # noqa: S108
    return Path(os.environ.get("PRISM_HOME", Path.home() / ".prism"))


# Every setting below is wrapped in a default_factory rather than assigned
# directly. A plain `x: str = os.environ.get(...)` is evaluated once, when the
# class body runs at import -- so constructing a second Settings() would hand
# back the first one's values and `reload()` would read nothing. These helpers
# return factories, so each instantiation genuinely re-reads the environment.
def _str(name: str, default: str = "", *, transform=None):
    def factory() -> str:
        raw = os.environ.get(name, default)
        return transform(raw) if transform else raw
    return field(default_factory=factory)


def _int(name: str, default: int):
    def factory() -> int:
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default
    return field(default_factory=factory)


def _float(name: str, default: float):
    def factory() -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default
    return field(default_factory=factory)


def _csv(name: str, *, lower: bool = False):
    def factory() -> list[str]:
        raw = os.environ.get(name, "")
        out = [p.strip() for p in raw.split(",") if p.strip()]
        return [p.lower() for p in out] if lower else out
    return field(default_factory=factory)


@dataclass
class Settings:
    # -- environment ------------------------------------------------------
    env: str = _str("SUNROOM_ENV", DEV)

    # -- storage ----------------------------------------------------------
    # "sqlite" for local work and tests; "postgres" for Supabase.
    store: str = _str("SUNROOM_STORE")
    database_url: str = _str("DATABASE_URL")
    home: Optional[Path] = None

    # -- model ------------------------------------------------------------
    model: str = _str("PRISM_MODEL", "claude-sonnet-4-5")
    provider: str = _str("PRISM_PROVIDER", "auto")   # auto|anthropic|mock
    chunk_chars: int = _int("PRISM_CHUNK_CHARS", 6000)
    chunk_overlap: int = _int("PRISM_CHUNK_OVERLAP", 400)
    max_concurrency: int = _int("PRISM_CONCURRENCY", 4)
    llm_timeout: float = _float("PRISM_LLM_TIMEOUT", 120.0)
    llm_retries: int = _int("PRISM_LLM_RETRIES", 4)

    # -- Supabase ---------------------------------------------------------
    supabase_url: str = _str("SUPABASE_URL", transform=lambda v: v.rstrip("/"))
    supabase_anon_key: str = _str("SUPABASE_ANON_KEY")
    supabase_service_key: str = _str("SUPABASE_SERVICE_ROLE_KEY")
    supabase_jwt_secret: str = _str("SUPABASE_JWT_SECRET")
    storage_bucket: str = _str("SUNROOM_BUCKET", "sources")

    # -- limits -----------------------------------------------------------
    # A month's tokens for an account with no key of its own. 2M is roughly a
    # dozen substantial documents: generous for a tester, survivable for you.
    default_token_budget: int = _int("SUNROOM_TOKEN_BUDGET", 2_000_000)
    max_upload_bytes: int = _int("SUNROOM_MAX_UPLOAD", 100 * 1024 * 1024)
    max_text_chars: int = _int("SUNROOM_MAX_TEXT", 4_000_000)
    # A worker invocation stops after this long and lets the next one continue,
    # so a job survives any function timeout.
    slice_seconds: float = _float("SUNROOM_SLICE_SECONDS", 35.0)
    max_job_attempts: int = _int("SUNROOM_MAX_JOB_ATTEMPTS", 5)
    # Advance the queue from inside a job-status poll, for deployments with no
    # way to run a worker on a schedule -- Vercel's Hobby plan caps crons at one
    # a day, and refuses the whole deployment if the file asks for more. The
    # browser polls while a job runs, so the work rides along on traffic that is
    # already happening. 0 disables it; keep it well under the function's
    # maxDuration, since it is spent inside a request.
    poll_nudge_seconds: float = _float("SUNROOM_POLL_NUDGE", 0.0)
    rate_limit_per_min: int = _int("SUNROOM_RATE_LIMIT", 120)

    # -- secrets ----------------------------------------------------------
    secret_key: str = _str("SUNROOM_SECRET_KEY")
    worker_secret: str = _str("SUNROOM_WORKER_SECRET")

    # -- misc -------------------------------------------------------------
    allowed_origins: list[str] = _csv("SUNROOM_ORIGINS")
    signup_allowlist: list[str] = _csv("SUNROOM_ALLOWED_EMAIL_DOMAINS", lower=True)

    def __post_init__(self) -> None:
        if self.home is None:
            self.home = _default_home()
        self.home = Path(self.home)
        try:
            self.home.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not self.store:
            self.store = "postgres" if self.database_url else "sqlite"

    # -- derived ----------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.env == PROD

    @property
    def db_path(self) -> Path:
        return self.home / "prism.db"

    @property
    def blobs(self) -> Path:
        p = self.home / "blobs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def multi_user(self) -> bool:
        """True when real accounts are in play, i.e. Supabase is configured."""
        return bool(self.supabase_url and (self.supabase_jwt_secret
                                           or self.supabase_anon_key))

    def preflight(self) -> list[str]:
        """
        Configuration problems that should stop a production boot.

        Returned rather than raised so the caller can report all of them at
        once instead of making someone fix them one redeploy at a time.
        """
        problems: list[str] = []
        if not self.is_production:
            return problems
        if self.store == "sqlite":
            problems.append(
                "SUNROOM_STORE=sqlite in production: a serverless filesystem is "
                "wiped between invocations, so every write would be lost. Set "
                "DATABASE_URL to your Supabase connection string.")
        elif not self.database_url:
            problems.append("DATABASE_URL is required when SUNROOM_STORE=postgres.")
        if not self.multi_user:
            problems.append(
                "No Supabase auth configured (SUPABASE_URL plus "
                "SUPABASE_JWT_SECRET or SUPABASE_ANON_KEY): the API would be "
                "open to anyone who finds the URL.")
        if not self.secret_key:
            problems.append(
                "SUNROOM_SECRET_KEY is required: it encrypts users' own API "
                "keys at rest.")
        elif len(self.secret_key) < 32:
            problems.append("SUNROOM_SECRET_KEY is too short (want 32+ chars).")
        if not self.worker_secret:
            problems.append(
                "SUNROOM_WORKER_SECRET is required: without it anyone can drive "
                "the job worker endpoint.")
        if self.provider == "mock":
            problems.append(
                "PRISM_PROVIDER=mock in production: the heuristic extractor is a "
                "test fixture, not a product.")
        return problems


SETTINGS = Settings()


def reload() -> Settings:
    """
    Re-read the environment, in place.

    Every module does `from .config import SETTINGS`, which binds the object,
    not the name -- so rebinding this global would leave all of them holding the
    old snapshot and make `reload()` a no-op everywhere except this module. That
    was a real bug: a test could set SUNROOM_ENV=production, call reload(), and
    watch the code under test cheerfully read the development settings.
    Mutating the single shared instance is what makes a reload actually reload.
    """
    fresh = Settings()
    for f in fields(fresh):
        object.__setattr__(SETTINGS, f.name, getattr(fresh, f.name))
    return SETTINGS
