"""
The real Anthropic client path, exercised without a key.

This is the code that has never run against a live model, so it is the code
most worth testing. The tests inject a transport in place of the SDK and drive
`AnthropicClient`'s actual logic -- request assembly, tool_use extraction,
retry decisions, usage accounting -- against recorded-shape responses. A
hand-written stand-in client would only prove the stand-in works.
"""
from __future__ import annotations

import types

import pytest

from prism.llm import (
    AnthropicClient,
    LLMError,
    MeteredClient,
    MockClient,
    TokenUse,
    get_client,
)

SCHEMA = {"type": "object", "properties": {"nodes": {"type": "array"}},
          "required": ["nodes"]}


# -- response shapes, as the SDK returns them ------------------------------

def block(**kw):
    return types.SimpleNamespace(**kw)


def response(*, tool_input=None, text=None, stop_reason="end_turn",
             in_tok=1000, out_tok=250):
    content = []
    if tool_input is not None:
        content.append(block(type="tool_use", name="emit", input=tool_input))
    if text is not None:
        content.append(block(type="text", text=text))
    return types.SimpleNamespace(
        content=content, stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))


class Transport:
    """Stands in for `anthropic.Anthropic`, recording what it was asked."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls: list[dict] = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._queue.pop(0) if self._queue else response(tool_input={"nodes": []})
        if isinstance(item, Exception):
            raise item
        return item


class ApiError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        headers = {"retry-after": str(retry_after)} if retry_after else {}
        self.response = types.SimpleNamespace(status_code=status, headers=headers)


# -- request assembly ------------------------------------------------------

def test_structured_forces_the_tool():
    """
    Structure comes from forced tool use, never from parsing prose. If this
    regresses to a text call the extractor starts hallucinating JSON.
    """
    t = Transport(response(tool_input={"nodes": [{"label": "x"}]}))
    c = AnthropicClient(model="m", transport=t)
    out = c.structured(system="sys", prompt="hello", schema=SCHEMA)
    assert out == {"nodes": [{"label": "x"}]}

    sent = t.calls[0]
    assert sent["model"] == "m"
    assert sent["system"] == "sys"
    assert sent["tool_choice"] == {"type": "tool", "name": "emit"}
    assert sent["tools"][0]["input_schema"] == SCHEMA
    assert sent["messages"] == [{"role": "user", "content": "hello"}]


def test_text_returns_only_text_blocks():
    t = Transport(response(text="hello ", tool_input=None))
    c = AnthropicClient(model="m", transport=t)
    assert c.text(system="s", prompt="p") == "hello "


def test_text_ignores_non_text_blocks():
    r = response(text="visible", tool_input={"nodes": []})
    c = AnthropicClient(model="m", transport=Transport(r))
    assert c.text(system="s", prompt="p") == "visible"


# -- retries ---------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_retries_transient_failures(status):
    """529 (overloaded) and 429 are normal under the concurrency we use."""
    slept: list[float] = []
    t = Transport(ApiError(status), ApiError(status),
                  response(tool_input={"nodes": [1]}))
    c = AnthropicClient(model="m", transport=t, max_retries=3,
                        sleep=slept.append)
    assert c.structured(system="s", prompt="p", schema=SCHEMA) == {"nodes": [1]}
    assert len(t.calls) == 3
    assert len(slept) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_does_not_retry_permanent_failures(status):
    """Retrying a 401 four times just delays a clear error by half a minute."""
    slept: list[float] = []
    t = Transport(ApiError(status))
    c = AnthropicClient(model="m", transport=t, max_retries=3, sleep=slept.append)
    with pytest.raises(LLMError) as e:
        c.structured(system="s", prompt="p", schema=SCHEMA)
    assert e.value.retryable is False
    assert e.value.status == status
    assert len(t.calls) == 1 and not slept


def test_honours_retry_after():
    slept: list[float] = []
    t = Transport(ApiError(429, retry_after=7), response(tool_input={"nodes": []}))
    AnthropicClient(model="m", transport=t, max_retries=2,
                    sleep=slept.append).structured(
        system="s", prompt="p", schema=SCHEMA)
    assert slept == [7.0]


def test_retry_after_is_capped():
    """A server asking us to wait an hour should not wedge a worker slice."""
    slept: list[float] = []
    t = Transport(ApiError(429, retry_after=3600), response(tool_input={"nodes": []}))
    AnthropicClient(model="m", transport=t, max_retries=2,
                    sleep=slept.append).structured(
        system="s", prompt="p", schema=SCHEMA)
    assert slept == [60.0]


def test_connection_errors_are_retried():
    """No status at all is a transport failure: DNS, reset socket, timeout."""
    slept: list[float] = []
    t = Transport(ConnectionError("reset by peer"),
                  response(tool_input={"nodes": []}))
    c = AnthropicClient(model="m", transport=t, max_retries=2, sleep=slept.append)
    assert c.structured(system="s", prompt="p", schema=SCHEMA) == {"nodes": []}
    assert len(t.calls) == 2


def test_gives_up_and_says_so():
    t = Transport(ApiError(529), ApiError(529), ApiError(529))
    c = AnthropicClient(model="m", transport=t, max_retries=2, sleep=lambda _: None)
    with pytest.raises(LLMError) as e:
        c.structured(system="s", prompt="p", schema=SCHEMA)
    assert e.value.retryable is True
    assert len(t.calls) == 3


# -- failure modes that used to be silent ----------------------------------

def test_truncated_structured_output_raises():
    """
    Hitting max_tokens mid-tool-call used to return {} -- which the pipeline
    read as "this chunk contained nothing", producing a document with a hole in
    it and no error anywhere.
    """
    t = Transport(response(tool_input=None, stop_reason="max_tokens"))
    c = AnthropicClient(model="m", transport=t)
    with pytest.raises(LLMError, match="max_tokens"):
        c.structured(system="s", prompt="p", schema=SCHEMA)


def test_no_tool_block_without_truncation_returns_empty():
    t = Transport(response(tool_input=None, text="sorry", stop_reason="end_turn"))
    assert AnthropicClient(model="m", transport=t).structured(
        system="s", prompt="p", schema=SCHEMA) == {}


# -- metering --------------------------------------------------------------

def test_usage_is_reported_per_call():
    seen: list[tuple] = []
    t = Transport(response(tool_input={"nodes": []}, in_tok=1234, out_tok=567))
    c = AnthropicClient(model="m", transport=t,
                        meter=lambda kind, model, use: seen.append((kind, model, use)))
    c.structured(system="s", prompt="p", schema=SCHEMA, kind="extract")
    assert seen == [("extract", "m", TokenUse(1234, 567))]


def test_usage_counted_once_even_with_retries():
    """A retried call bills for the attempt that succeeded, not every attempt."""
    seen: list[TokenUse] = []
    t = Transport(ApiError(529), response(tool_input={"nodes": []},
                                          in_tok=100, out_tok=10))
    AnthropicClient(model="m", transport=t, max_retries=2, sleep=lambda _: None,
                    meter=lambda k, m, u: seen.append(u)).structured(
        system="s", prompt="p", schema=SCHEMA)
    assert seen == [TokenUse(100, 10)]


def test_failed_calls_are_not_billed():
    seen: list[TokenUse] = []
    t = Transport(ApiError(400))
    with pytest.raises(LLMError):
        AnthropicClient(model="m", transport=t, max_retries=0,
                        meter=lambda k, m, u: seen.append(u)).structured(
            system="s", prompt="p", schema=SCHEMA)
    assert seen == []


# -- the metered wrapper ---------------------------------------------------

class FakeAccounts:
    def __init__(self):
        self.rows: list[dict] = []

    def record(self, user_id, **kw):
        self.rows.append({"user_id": user_id, **kw})


def test_metered_client_records_and_tallies(monkeypatch):
    fake = FakeAccounts()
    monkeypatch.setattr("prism.accounts.accounts", lambda: fake)
    monkeypatch.setattr("prism.accounts.check", lambda *a, **k: None)

    inner = AnthropicClient(model="m", transport=Transport(
        response(tool_input={"nodes": []}, in_tok=10, out_tok=5),
        response(tool_input={"nodes": []}, in_tok=20, out_tok=7)))
    c = MeteredClient(inner, user_id="u1", byo=False, job_id="j1")
    c.structured(system="s", prompt="p", schema=SCHEMA)
    c.structured(system="s", prompt="p", schema=SCHEMA)

    assert c.spent == TokenUse(30, 12)
    assert [r["input_tokens"] for r in fake.rows] == [10, 20]
    assert all(r["job_id"] == "j1" and r["byo"] is False for r in fake.rows)


def test_metered_client_checks_quota_before_every_call(monkeypatch):
    """
    Once per job is not enough. An estimate that came in low would otherwise
    let a single job overrun the budget by however much it was wrong by.
    """
    calls = {"n": 0}

    def check(*a, **k):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("over budget")

    monkeypatch.setattr("prism.accounts.accounts", lambda: FakeAccounts())
    monkeypatch.setattr("prism.accounts.check", check)

    c = MeteredClient(MockClient(), user_id="u1", byo=False)
    c.text(system="s", prompt="p")
    c.text(system="s", prompt="p")
    with pytest.raises(RuntimeError, match="over budget"):
        c.text(system="s", prompt="p")
    assert calls["n"] == 3


def test_byo_key_skips_the_quota(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("quota must not be consulted for a BYO account")

    monkeypatch.setattr("prism.accounts.accounts", lambda: FakeAccounts())
    monkeypatch.setattr("prism.accounts.check", boom)
    c = MeteredClient(MockClient(), user_id="u1", byo=True)
    c.text(system="s", prompt="p")


def test_a_lost_usage_row_does_not_lose_the_work(monkeypatch):
    """Metering is bookkeeping. It must never be the reason an ingest fails."""
    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("database is down")

    monkeypatch.setattr("prism.accounts.accounts", lambda: Broken())
    monkeypatch.setattr("prism.accounts.check", lambda *a, **k: None)
    c = MeteredClient(MockClient(), user_id="u1", byo=False)
    assert c.text(system="s", prompt="hello") == "hello"
    assert c.spent.total > 0          # the in-process tally still works


# -- provider selection ----------------------------------------------------

def test_production_refuses_to_fall_back_to_the_mock(monkeypatch):
    """
    Silently serving heuristic output to someone who is paying is worse than an
    error. In development the fallback is a convenience; in production it is a
    lie about what the product does.
    """
    from prism.config import SETTINGS
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(SETTINGS, "env", "production")
    monkeypatch.setattr(SETTINGS, "provider", "auto")
    with pytest.raises(LLMError, match="Refusing to fall back"):
        get_client()


def test_development_falls_back_quietly(monkeypatch):
    from prism.config import SETTINGS
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(SETTINGS, "env", "development")
    monkeypatch.setattr(SETTINGS, "provider", "auto")
    assert isinstance(get_client(), MockClient)


def test_explicit_anthropic_without_a_key_is_an_error(monkeypatch):
    from prism.config import SETTINGS
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(SETTINGS, "provider", "anthropic")
    with pytest.raises(LLMError, match="no API key"):
        get_client()
