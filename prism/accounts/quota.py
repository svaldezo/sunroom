"""
What a job will cost, and whether this account can afford it.

Two separate jobs, and conflating them is how a budget gets blown:

  * **estimate** runs before anything is spent. It is shown to the person, and
    it is what a refusal is based on, so it has to be honest about being an
    estimate -- it reads the source's length, not the model's mind.
  * **enforce** runs on every single call, against tokens actually recorded.
    An estimate that was too low must not be able to overrun the budget by
    however much it was wrong by.

Both are bypassed entirely when the account brought its own key, because then
the money is theirs and a quota would just be us being officious.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import SETTINGS
from .store import Accounts, Usage, accounts

# Characters per token, English prose. The real ratio for Claude is ~3.6-4.0;
# 3.6 is deliberately pessimistic so an estimate errs toward warning someone.
CHARS_PER_TOKEN = 3.6

# The extraction prompt wraps each chunk in instructions and a schema, and the
# model writes structured nodes back. Measured against the mock corpus and
# rounded up: output runs about 45% of input for extraction.
PROMPT_OVERHEAD_TOKENS = 900
OUTPUT_RATIO = 0.45

# Rough public list price, USD per million tokens, for showing a number a
# person can reason about. Wrong the day pricing changes; better than nothing,
# and only ever used for display.
USD_PER_MTOK_IN = 3.0
USD_PER_MTOK_OUT = 15.0


class QuotaExceeded(RuntimeError):
    """This account has spent its budget for the month."""

    def __init__(self, usage: Usage, needed: int = 0):
        self.usage = usage
        self.needed = needed
        over = f"; this needs about {needed:,} more" if needed else ""
        super().__init__(
            f"Monthly limit reached: {usage.billable_tokens:,} of "
            f"{usage.budget:,} tokens used{over}. Add your own API key in "
            f"Settings to keep going, or wait for the reset.")


@dataclass
class Estimate:
    chars: int
    chunks: int
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def usd(self) -> float:
        return (self.input_tokens / 1e6 * USD_PER_MTOK_IN
                + self.output_tokens / 1e6 * USD_PER_MTOK_OUT)

    def to_dict(self) -> dict:
        return {"chars": self.chars, "chunks": self.chunks,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "usd": round(self.usd, 3)}


def estimate_ingest(text_chars: int) -> Estimate:
    """What understanding a source of this length will cost, roughly."""
    chars = max(0, int(text_chars))
    step = max(1, SETTINGS.chunk_chars - SETTINGS.chunk_overlap)
    chunks = max(1, -(-chars // step)) if chars else 0
    body_tokens = int(chars / CHARS_PER_TOKEN)
    input_tokens = body_tokens + chunks * PROMPT_OVERHEAD_TOKENS
    output_tokens = int(body_tokens * OUTPUT_RATIO)
    return Estimate(chars=chars, chunks=chunks,
                    input_tokens=input_tokens, output_tokens=output_tokens)


def check(user_id: str, *, needed: int = 0,
          acc: Optional[Accounts] = None) -> Usage:
    """
    Raise if this account cannot afford `needed` more tokens.

    Called before a job is queued (with an estimate) and again before each model
    call (with zero), so a wildly wrong estimate cannot become a wildly wrong
    bill: the per-call check stops the job the moment the meter actually hits
    the limit.
    """
    acc = acc or accounts()
    usage = acc.usage(user_id)
    if usage.byo:
        return usage
    if usage.billable_tokens + max(0, needed) > usage.budget:
        raise QuotaExceeded(usage, needed=needed)
    return usage


def would_exceed(usage: Usage, needed: int) -> bool:
    if usage.byo:
        return False
    return usage.billable_tokens + max(0, needed) > usage.budget
