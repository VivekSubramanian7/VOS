"""Record/replay log for model calls.

This is the whole observability story for Phase 1, and it was chosen over a tracing
stack deliberately (ADR-007): v1 shipped a proven obs stack and died anyway. A trace
viewer tells you what happened; a cassette lets you *re-run* it.

Three jobs:

  1. Tune the prompt offline. Replay real captured thoughts in seconds with no API
     spend, instead of re-sending messages from your phone.
  2. Evaluate a model swap. Replay the same history against a different `VOS_MODEL`
     and diff the category assignments — the check that makes provider portability
     real rather than nominal.
  3. Meter spend, so `/stats` reports actual cost rather than an estimate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Used only to report and cap spend.
# An unrecognised model is not metered — capture must never be blocked because a
# price is unknown, so the budget guard fails open and says so.
# Gemini prices verified against ai.google.dev/gemini-api/docs/pricing on 2026-08-08.
# Google's pro tiers charge more above a 200k-token prompt; a VOS classification is a
# few hundred tokens, so the low tier always applies and only it is recorded here.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-2.5-pro": (1.25, 10.00),
}


def price(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost in USD, or None when the model has no known price.

    Longest key first, because these are substring matches and model families nest:
    "gemini-3.5-flash" is a substring of "gemini-3.5-flash-lite", which costs a fifth
    as much. Matching by length rather than by dict order means adding a price can
    never silently mis-meter a neighbouring model.
    """
    for name in sorted(PRICING, key=len, reverse=True):
        if name in model:
            pin, pout = PRICING[name]
            return (input_tokens * pin + output_tokens * pout) / 1_000_000
    return None


class CassetteEntry(BaseModel):
    """One model call, recorded verbatim enough to replay and to audit."""

    thought_id: UUID
    model: str
    prompt: str
    response: dict | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReplayMiss(RuntimeError):
    """Raised when replay has no recording for a thought.

    Loud on purpose: a replay that silently fell through to the network would burn
    money and quietly invalidate whatever you were measuring.
    """


class Cassette:
    """One JSONL file per thought; one line per attempt.

    Append-only, like the journal, so re-classifying the same thought against a new
    model keeps the earlier attempts for comparison rather than overwriting them.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, thought_id: UUID) -> Path:
        return self._dir / f"{thought_id}.jsonl"

    def record(self, entry: CassetteEntry) -> None:
        """Never raises. A failed recording must not fail a capture — this is a
        diagnostic channel, not part of the durability path."""
        try:
            with open(self._path(entry.thought_id), "a", encoding="utf-8", newline="\n") as fh:
                fh.write(entry.model_dump_json() + "\n")
        except OSError as exc:  # pragma: no cover - disk-level failure
            log.warning("Could not write cassette for %s: %s", entry.thought_id, exc)

    def entries(self, thought_id: UUID) -> list[CassetteEntry]:
        path = self._path(thought_id)
        if not path.exists():
            return []
        out: list[CassetteEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(CassetteEntry.model_validate_json(line))
                except ValueError as exc:
                    log.warning("Skipping malformed cassette line for %s: %s", thought_id, exc)
        return out

    def latest(self, thought_id: UUID, model: str | None = None) -> CassetteEntry | None:
        candidates = [
            e for e in self.entries(thought_id) if model is None or e.model == model
        ]
        return candidates[-1] if candidates else None

    def require(self, thought_id: UUID, model: str | None = None) -> CassetteEntry:
        entry = self.latest(thought_id, model)
        if entry is None or entry.response is None:
            raise ReplayMiss(
                f"No recorded response for thought {thought_id}"
                + (f" on model {model}" if model else "")
                + ". Replay will not fall back to the network."
            )
        return entry

    def all_entries(self) -> list[CassetteEntry]:
        out: list[CassetteEntry] = []
        for path in sorted(self._dir.glob("*.jsonl")):
            try:
                out.extend(self.entries(UUID(path.stem)))
            except ValueError:
                continue
        return out


class BudgetGuard:
    """Daily spend ceiling.

    On breach, classification is deferred to /pending and capture continues — a
    degradation, never data loss (§9.4).
    """

    def __init__(self, cassette: Cassette, daily_limit_usd: float) -> None:
        self._cassette = cassette
        self._limit = daily_limit_usd

    def spent_today(self) -> float:
        today = datetime.now(UTC).date()
        return sum(
            e.cost_usd or 0.0
            for e in self._cassette.all_entries()
            if e.created_at.astimezone(UTC).date() == today
        )

    def exceeded(self) -> bool:
        if self._limit <= 0:
            return False
        spent = self.spent_today()
        if spent >= self._limit:
            log.warning("Daily budget reached: $%.4f of $%.2f", spent, self._limit)
            return True
        return False


def read_json_safely(raw: str) -> dict | None:  # small helper used by the CLI
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None
