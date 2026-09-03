"""
LLM provider abstraction.

Three reasons this is an interface rather than a direct SDK call:

  1. Per-medium routing -- nobody is best at every modality. The renderer that
     writes narration and the one that generates images should be free to use
     different models.
  2. The pipeline must run offline. MockClient does heuristic extraction so the
     full ingest -> IR -> render loop is testable with no key and no spend.
  3. Every call has to be metered and every account's spend capped, and the only
     way to guarantee that is to make it impossible to reach the SDK without
     passing through here.

The retry policy deserves a note. Anthropic's overload signal (529) and rate
limit (429) are both normal operating conditions under concurrency, not
exceptions: an extraction pass fires several chunks at once by design. Retrying
them with backoff and honouring `retry-after` is the difference between "the
ingest took a bit longer" and "the ingest failed at chunk 7 of 22".
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from .config import SETTINGS


@dataclass(frozen=True)
class TokenUse:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


#: Called after every model call. `kind` is what the call was for.
Meter = Callable[[str, str, TokenUse], None]


class LLMError(RuntimeError):
    """A model call failed in a way the caller should surface, not swallow."""

    def __init__(self, message: str, *, retryable: bool = False,
                 status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class LLMClient(Protocol):
    name: str

    def structured(self, *, system: str, prompt: str, schema: dict[str, Any],
                   max_tokens: int = 4096,
                   kind: str = "extract") -> dict[str, Any]: ...

    def text(self, *, system: str, prompt: str, max_tokens: int = 2048,
             kind: str = "write") -> str: ...


# --------------------------------------------------------------------------

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def _sleep_for(attempt: int, retry_after: Optional[float]) -> float:
    """Exponential backoff with full jitter, and the server's word over ours."""
    if retry_after and retry_after > 0:
        return min(retry_after, 60.0)
    # Jitter, not a secret. A predictable backoff is only a problem if someone
    # can profit from predicting it, and nobody can profit from predicting when
    # we retry our own API call.
    return min(60.0, random.uniform(0.5, 2 ** attempt))         # noqa: S311


def _status_of(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_of(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        return float(raw) if raw is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


class AnthropicClient:
    """
    Strict JSON via forced tool use -- no parsing prose for structure.

    `transport` exists so tests can drive this class's real logic (schema
    assembly, tool_use extraction, retry decisions, usage accounting) against
    recorded responses. Testing a hand-written stand-in instead would only ever
    prove that the stand-in works.
    """

    name = "anthropic"

    def __init__(self, model: Optional[str] = None, *,
                 api_key: Optional[str] = None,
                 meter: Optional[Meter] = None,
                 transport: Any = None,
                 max_retries: Optional[int] = None,
                 timeout: Optional[float] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.model = model or SETTINGS.model
        self.meter = meter
        self.max_retries = (SETTINGS.llm_retries if max_retries is None
                            else max_retries)
        self.timeout = SETTINGS.llm_timeout if timeout is None else timeout
        self._sleep = sleep
        if transport is not None:
            self._client = transport
        else:
            try:
                import anthropic
            except ImportError as exc:      # pragma: no cover
                raise LLMError(
                    "the anthropic package is not installed: "
                    "pip install 'sunroom[anthropic]'") from exc
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise LLMError("no Anthropic API key available")
            # The SDK retries too; turn that off so there is one policy, here,
            # that the meter and the job's time budget both know about.
            self._client = anthropic.Anthropic(
                api_key=key, max_retries=0, timeout=self.timeout)

    # -- the one place a request actually goes out -------------------------
    def _call(self, kind: str, **kwargs) -> Any:
        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.messages.create(**kwargs)
            except Exception as exc:
                status = _status_of(exc)
                # A missing status is a transport-level failure (DNS, reset
                # socket, timeout) and worth retrying. An explicit 400 or 401 is
                # not, and retrying it only delays a clear error.
                retryable = status in RETRY_STATUS or status is None
                if not retryable or attempt >= self.max_retries:
                    raise LLMError(f"model call failed: {exc}",
                                   retryable=retryable, status=status) from exc
                last = exc
                self._sleep(_sleep_for(attempt, _retry_after_of(exc)))
                continue

            usage = getattr(resp, "usage", None)
            use = TokenUse(int(getattr(usage, "input_tokens", 0) or 0),
                           int(getattr(usage, "output_tokens", 0) or 0))
            if self.meter:
                self.meter(kind, self.model, use)
            return resp
        raise LLMError(f"model call failed after retries: {last}", retryable=True)

    def structured(self, *, system: str, prompt: str, schema: dict[str, Any],
                   max_tokens: int = 4096,
                   kind: str = "extract") -> dict[str, Any]:
        resp = self._call(
            kind,
            model=self.model, max_tokens=max_tokens, system=system,
            tools=[{"name": "emit",
                    "description": "Emit the structured result.",
                    "input_schema": schema}],
            tool_choice={"type": "tool", "name": "emit"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        # Forced tool use means this should not happen. When it does, the caller
        # needs to know the model ran out of room rather than get a silent {}
        # and a document with no nodes in it.
        if getattr(resp, "stop_reason", "") == "max_tokens":
            raise LLMError(
                "the model hit max_tokens before finishing the structured "
                "result; the chunk is too large for this token budget",
                retryable=False)
        return {}

    def text(self, *, system: str, prompt: str, max_tokens: int = 2048,
             kind: str = "write") -> str:
        resp = self._call(
            kind, model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text")


# --------------------------------------------------------------------------

# Do not split after the "1." of a numbered list, or after an initial.
# Evaluation finding: naive splitting shredded every numbered procedure, so
# "1. Record the bag number" lost its marker, no STEP nodes were produced, and
# the flowchart renderer silently fell back to a mind map.
# The lookbehinds must span TWO characters: "(?<![0-9])(?<=[.])" both anchor at
# the same position and so never see the digit in "1.". Spelling it as
# "(?<![0-9][.!?])" is what actually protects numbered list markers.
SENT = re.compile(
    r"(?<![0-9][.!?])(?<![A-Z][.!?])(?<=[.!?])\s+(?=[A-Z0-9\"“])"
)
BLOCK = re.compile(r"\n\s*\n|\n(?=#{1,6}\s)|\n(?=\s*[-*]\s)|\n(?=\s*\d+[.)]\s)")
DEFN = re.compile(
    r"^(?P<term>[A-Z][\w\s\-']{2,50}?)\s+(?:is|are|refers to|means|is defined as|denotes)\s+(?P<def>.+)$"
)
STEP = re.compile(r"^\s*(?:(\d+)[.)]\s+|(?:first|second|third|fourth|fifth|next|then|finally|lastly|afterwards?)[,\s]+)", re.I)
QUANT = re.compile(r"\b\d[\d,.]*\s*(?:%|percent|years?|people|km|kg|miles?|dollars?|\$)\b", re.I)
CAUSE = re.compile(r"\b(?:because|therefore|causes?|leads? to|results? in|so that|due to)\b", re.I)
CONCRETE = re.compile(
    r"\b(?:tool|building|hand|water|tree|animal|machine|body|food|road|house|river|"
    r"stone|fire|book|map|city|field|boat|door|table|plant|bone|cloth|metal)\w*\b", re.I
)


def _blocks(text: str) -> list[str]:
    """
    Split into extraction blocks on blank lines, markdown headings, list
    markers, and unmarked heading lines (the PDF case).
    """
    from .ingest.base import looks_like_heading

    out: list[str] = []
    for chunk in BLOCK.split(text):
        lines = chunk.split("\n")
        buf: list[str] = []
        for i, line in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if looks_like_heading(line, nxt):
                if buf:
                    out.append("\n".join(buf))
                out.append(line)
                buf = []
            else:
                buf.append(line)
        if buf:
            out.append("\n".join(buf))
    return [b for b in out if b.strip()]


class MockClient:
    """
    Deterministic, offline, dependency-free approximation of the extractor.
    Good enough to exercise every downstream path; not good enough to ship,
    which is why `Settings.preflight` refuses production with it selected.
    """
    name = "mock"

    def __init__(self, meter: Optional[Meter] = None):
        self.meter = meter

    def _meter(self, kind: str, text: str) -> None:
        if self.meter:
            # Plausible numbers, so quota logic is exercised offline too.
            self.meter(kind, "mock", TokenUse(len(text) // 4, len(text) // 12))

    def structured(self, *, system: str, prompt: str, schema: dict[str, Any],
                   max_tokens: int = 4096,
                   kind: str = "extract") -> dict[str, Any]:
        self._meter(kind, prompt)
        if "nodes" not in schema.get("properties", {}):
            return {}
        body = prompt.split("<text>", 1)[-1].split("</text>", 1)[0].strip()
        return {"nodes": self._extract(body), "edges": []}

    def _extract(self, body: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        # Blank lines and heading markers are hard sentence boundaries. Without
        # this, "## Procedure" glued the heading to the previous sentence and
        # swallowed the "1." marker of the first list item.
        sentences: list[str] = []
        for block in _blocks(body):
            sentences.extend(SENT.split(block))
        for raw in sentences:
            sent = raw.strip()
            if len(sent) < 25:
                offset += len(raw) + 1
                continue
            start = body.find(sent, offset)
            if start < 0:
                start = offset
            end = start + len(sent)
            offset = end

            kind_, label = "claim", " ".join(sent.split()[:8]).rstrip(",;:")
            step_match = STEP.match(sent)
            m = DEFN.match(sent)
            if step_match:
                kind_ = "step"
                label = " ".join(STEP.sub("", sent).split()[:8]).rstrip(",;:")
            elif m:
                kind_, label = "definition", m.group("term").strip()
            elif QUANT.search(sent):
                kind_ = "quantity"

            concrete = min(1.0, 0.25 + 0.2 * len(CONCRETE.findall(sent)))
            out.append({
                "kind": kind_,
                "label": label,
                "body": sent,
                "start": start,
                "end": end,
                "salience": round(min(0.95, 0.35 + len(sent) / 400), 2),
                "difficulty": 0.5,
                "concreteness": round(concrete, 2),
                "confidence": 0.55,
                "causal": bool(CAUSE.search(sent)),
                "order": int(step_match.group(1)) if step_match and step_match.group(1) else None,
            })
        return out

    def text(self, *, system: str, prompt: str, max_tokens: int = 2048,
             kind: str = "write") -> str:
        self._meter(kind, prompt)
        return prompt.strip()[:max_tokens]


# --------------------------------------------------------------------------

class MeteredClient:
    """
    Wraps a client so that no call can happen without a budget check, and no
    call can complete without being recorded.

    The check runs *before every call*, not once per job. An estimate that came
    in low must not be able to overspend by however much it was wrong by.
    """

    def __init__(self, inner: LLMClient, *, user_id: str, byo: bool,
                 job_id: Optional[str] = None):
        self.inner = inner
        self.user_id = user_id
        self.byo = byo
        self.job_id = job_id
        self.name = getattr(inner, "name", "metered")
        self.spent = TokenUse()
        self._lock = threading.Lock()
        try:
            inner.meter = self._record       # type: ignore[attr-defined]
        except Exception:                    # pragma: no cover  # noqa: S110
            pass

    def _record(self, kind: str, model: str, use: TokenUse) -> None:
        from .accounts import accounts
        with self._lock:
            self.spent = TokenUse(self.spent.input_tokens + use.input_tokens,
                                  self.spent.output_tokens + use.output_tokens)
        try:
            accounts().record(self.user_id, kind=kind, model=model,
                              input_tokens=use.input_tokens,
                              output_tokens=use.output_tokens,
                              byo=self.byo, job_id=self.job_id)
        except Exception:                                       # noqa: S110
            # Losing a usage row must not lose the user's work. The in-process
            # tally still stops a runaway job inside this invocation.
            pass

    def _guard(self) -> None:
        if self.byo:
            return
        from .accounts import check
        check(self.user_id)

    def structured(self, **kw) -> dict[str, Any]:
        self._guard()
        return self.inner.structured(**kw)

    def text(self, **kw) -> str:
        self._guard()
        return self.inner.text(**kw)


def get_client(provider: Optional[str] = None, *,
               api_key: Optional[str] = None,
               meter: Optional[Meter] = None) -> LLMClient:
    """
    The client for a given provider.

    `auto` prefers Anthropic when a key is present and falls back to the mock.
    That fallback is a development convenience and a production hazard, so it
    is refused outright when the environment says production: silently serving
    heuristic output to a paying user is worse than an error.
    """
    provider = provider or SETTINGS.provider
    if provider == "mock":
        return MockClient(meter=meter)

    if provider in ("auto", "anthropic"):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return AnthropicClient(api_key=key, meter=meter)
        if provider == "anthropic":
            raise LLMError(
                "PRISM_PROVIDER=anthropic but no API key is available. Set "
                "ANTHROPIC_API_KEY, or have the account add its own key.")
        if SETTINGS.is_production:
            raise LLMError(
                "No Anthropic API key is configured. Refusing to fall back to "
                "the heuristic extractor in production -- it would produce "
                "plausible-looking output that nobody asked for.")
    return MockClient(meter=meter)


def client_for(user_id: str, *, job_id: Optional[str] = None) -> LLMClient:
    """
    The metered client for one account.

    An account with its own key spends its own money and is not metered against
    the shared budget; everyone else goes through the quota.
    """
    from .accounts import accounts

    try:
        own_key = accounts().api_key(user_id)
    except Exception:
        own_key = None

    inner = get_client(api_key=own_key) if own_key else get_client()
    return MeteredClient(inner, user_id=user_id, byo=bool(own_key), job_id=job_id)
