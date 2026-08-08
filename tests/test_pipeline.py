"""Pipeline tests — fully offline.

The model is injected, so the entire classification path runs with no network and no
spend. This is the loop that makes prompt iteration cheap, which is the direct answer
to v1's "nothing to test, no quick feedback".

The most important assertion here is negative: a model failure must *not* propagate.
Capture is already durable by the time this runs, and enrichment failing is a
degradation, not an incident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vos.cassette import BudgetGuard, Cassette, CassetteEntry, ReplayMiss, price
from vos.contracts import CaptureRecord, Classification, ExtractedEntity, SourceRef
from vos.pipeline import ThoughtState, build_pipeline, build_prompt


class StubModel:
    """Stands in for a chat model.

    Deliberately not a langchain fake: `build_pipeline` only ever calls
    `with_structured_output(...).ainvoke(...)`, so this is the entire surface the
    pipeline depends on, and pinning it here keeps the test honest about that.
    """

    def __init__(self, result: Classification | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[list] = []

    def with_structured_output(self, schema):  # noqa: ANN001
        outer = self

        class _Runnable:
            async def ainvoke(self, messages):  # noqa: ANN001
                outer.calls.append(messages)
                if outer.error is not None:
                    raise outer.error
                return outer.result

        return _Runnable()


def _record(text: str = "book flights to Tokyo in October") -> CaptureRecord:
    return CaptureRecord.create(
        chat_id=42, message_id=1, text=text, captured_at=datetime.now(UTC)
    )


def _classification() -> Classification:
    return Classification(
        category="TripPlanning",
        title="Tokyo flights",
        summary="Book flights to Tokyo for October.",
        entities=[
            ExtractedEntity(name="Tokyo", type="place", salience=0.9),
            ExtractedEntity(name="October", type="topic", salience=0.4),
        ],
        confidence=0.92,
    )


# --- prompt ---------------------------------------------------------------- #


def test_prompt_puts_stable_content_first_for_caching():
    """Cache-hostile ordering silently costs money on every call, so it is asserted."""
    messages = build_prompt(_record(), [SourceRef(name="Naval", kind="person")])
    assert len(messages) == 2
    system, human = messages
    assert "You classify short personal thoughts" in system.content
    assert human.content == "book flights to Tokyo in October"


def test_prompt_includes_followed_sources():
    messages = build_prompt(
        _record(),
        [
            SourceRef(name="Paul Graham", kind="person"),
            SourceRef(name="Thinking, Fast and Slow", kind="book", author="Kahneman"),
        ],
    )
    system = messages[0].content
    assert "Paul Graham" in system
    assert "Thinking, Fast and Slow" in system
    assert "Kahneman" in system


def test_prompt_handles_no_follows():
    system = build_prompt(_record(), [])[0].content
    assert "nothing declared yet" in system


# --- happy path ------------------------------------------------------------ #


async def test_pipeline_classifies(tmp_path: Path):
    expected = _classification()
    graph = build_pipeline(StubModel(result=expected), model_name="stub")
    out = await graph.ainvoke(ThoughtState(record=_record()))
    assert out["classification"] == expected
    assert out["error"] is None


async def test_pipeline_passes_followed_into_the_call():
    model = StubModel(result=_classification())
    graph = build_pipeline(model, model_name="stub")
    await graph.ainvoke(
        ThoughtState(record=_record(), followed=[SourceRef(name="Naval", kind="person")])
    )
    assert "Naval" in str(model.calls[0][0].content)


# --- failure isolation (the point) ----------------------------------------- #


async def test_model_failure_does_not_raise():
    """If this ever raises, the capture path takes the blast — the exact coupling
    the architecture removes."""
    graph = build_pipeline(StubModel(error=RuntimeError("provider down")), model_name="stub")
    out = await graph.ainvoke(ThoughtState(record=_record()))
    assert out["classification"] is None
    assert "provider down" in out["error"]


async def test_model_returning_none_is_treated_as_failure():
    graph = build_pipeline(StubModel(result=None), model_name="stub")
    out = await graph.ainvoke(ThoughtState(record=_record()))
    assert out["classification"] is None
    assert "no valid Classification" in out["error"]


async def test_model_returning_wrong_type_is_treated_as_failure():
    graph = build_pipeline(StubModel(result="not a classification"), model_name="stub")  # type: ignore[arg-type]
    out = await graph.ainvoke(ThoughtState(record=_record()))
    assert out["classification"] is None
    assert out["error"]


# --- cassette -------------------------------------------------------------- #


async def test_success_is_recorded(tmp_path: Path):
    cassette = Cassette(tmp_path / "cassettes")
    record = _record()
    graph = build_pipeline(
        StubModel(result=_classification()), model_name="anthropic:claude-opus-5",
        cassette=cassette,
    )
    await graph.ainvoke(ThoughtState(record=record))

    entry = cassette.latest(record.id)
    assert entry is not None
    assert entry.response["category"] == "TripPlanning"
    assert "book flights to Tokyo" in entry.prompt
    assert entry.error is None


async def test_failure_is_recorded_for_diagnosis(tmp_path: Path):
    cassette = Cassette(tmp_path / "cassettes")
    record = _record()
    graph = build_pipeline(
        StubModel(error=ValueError("bad schema")), model_name="stub", cassette=cassette
    )
    await graph.ainvoke(ThoughtState(record=record))

    entry = cassette.latest(record.id)
    assert entry is not None and entry.response is None
    assert "bad schema" in entry.error


def test_cassette_appends_attempts_rather_than_overwriting(tmp_path: Path):
    cassette = Cassette(tmp_path / "c")
    rid = _record().id
    cassette.record(CassetteEntry(thought_id=rid, model="a", prompt="p", response={"x": 1}))
    cassette.record(CassetteEntry(thought_id=rid, model="b", prompt="p", response={"x": 2}))
    assert len(cassette.entries(rid)) == 2
    assert cassette.latest(rid, model="a").response == {"x": 1}
    assert cassette.latest(rid).response == {"x": 2}


def test_replay_miss_is_loud(tmp_path: Path):
    """A replay that fell through to the network would burn money and invalidate
    whatever was being measured."""
    cassette = Cassette(tmp_path / "c")
    with pytest.raises(ReplayMiss):
        cassette.require(_record().id)


# --- cost and budget ------------------------------------------------------- #


def test_price_known_model():
    assert price("anthropic:claude-opus-5", 1_000_000, 0) == pytest.approx(5.00)
    assert price("anthropic:claude-opus-5", 0, 1_000_000) == pytest.approx(25.00)
    assert price("anthropic:claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.00)


def test_price_unknown_model_is_none():
    assert price("ollama:llama3", 1_000, 1_000) is None


def test_budget_guard_trips_at_limit(tmp_path: Path):
    cassette = Cassette(tmp_path / "c")
    rid = _record().id
    cassette.record(CassetteEntry(thought_id=rid, model="m", prompt="p", cost_usd=0.9))
    assert BudgetGuard(cassette, 1.00).exceeded() is False
    cassette.record(CassetteEntry(thought_id=rid, model="m", prompt="p", cost_usd=0.2))
    assert BudgetGuard(cassette, 1.00).exceeded() is True


def test_budget_of_zero_means_unlimited(tmp_path: Path):
    cassette = Cassette(tmp_path / "c")
    cassette.record(
        CassetteEntry(thought_id=_record().id, model="m", prompt="p", cost_usd=99.0)
    )
    assert BudgetGuard(cassette, 0).exceeded() is False


def test_unpriced_model_does_not_block_capture(tmp_path: Path):
    """Fail open: an unknown price must not stop the system working."""
    cassette = Cassette(tmp_path / "c")
    cassette.record(CassetteEntry(thought_id=_record().id, model="ollama:x", prompt="p"))
    assert BudgetGuard(cassette, 0.01).exceeded() is False
