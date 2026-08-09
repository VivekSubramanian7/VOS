"""Shopping extraction pipeline tests — offline, with a stub model.

The node's job is to be harmless when the model misbehaves. A capture is already
durable and filed by the time this runs, so every failure here must come back as a
value in the state rather than an exception climbing into the capture path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vos.cassette import Cassette
from vos.contracts import CaptureRecord, ShoppingExtraction, ShoppingItem
from vos.pipeline import ShoppingState, build_shopping_pipeline


def _record(text: str = "need bananas and 2L oat milk") -> CaptureRecord:
    return CaptureRecord.create(
        chat_id=42, message_id=1, text=text, captured_at=datetime(2026, 8, 9, tzinfo=UTC)
    )


class StubModel:
    """Returns one queued result per structured call, or raises."""

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0
        self.messages: list = []

    def with_structured_output(self, schema):  # noqa: ANN001
        outer = self

        class _R:
            async def ainvoke(self, messages):  # noqa: ANN001
                outer.calls += 1
                outer.messages = messages
                if outer.error is not None:
                    raise outer.error
                return outer.result

        return _R()


async def _run(model: StubModel, record: CaptureRecord | None = None, **kwargs) -> dict:
    pipeline = build_shopping_pipeline(model, model_name="stub", **kwargs)  # type: ignore[arg-type]
    return await pipeline.ainvoke(ShoppingState(record=record or _record()))


# --- the happy path -------------------------------------------------------- #


async def test_items_come_back_in_the_state():
    model = StubModel(
        ShoppingExtraction(
            items=[ShoppingItem(name="bananas"), ShoppingItem(name="oat milk", quantity="2L")]
        )
    )

    out = await _run(model)

    assert out["error"] is None
    assert [i.name for i in out["extraction"].items] == ["bananas", "oat milk"]
    assert out["extraction"].items[1].quantity == "2L"


async def test_the_thought_text_reaches_the_model():
    model = StubModel(ShoppingExtraction())
    await _run(model, _record("out of coffee again"))

    assert "out of coffee again" in str(model.messages[-1].content)


async def test_an_empty_extraction_is_a_success_not_an_error():
    """'Sony or Bose?' is a Shopping thought with nothing to buy. Treating that as a
    failure would re-queue it on every restart and nag the user about it."""
    out = await _run(StubModel(ShoppingExtraction(items=[])))

    assert out["error"] is None
    assert out["extraction"].items == []


# --- behaviour under imperfection ------------------------------------------ #


async def test_a_provider_failure_is_returned_not_raised():
    out = await _run(StubModel(error=RuntimeError("provider is down")))

    assert out["extraction"] is None
    assert "provider is down" in out["error"]


async def test_a_model_returning_nothing_is_handled():
    out = await _run(StubModel(result=None))

    assert out["extraction"] is None
    assert "no valid ShoppingExtraction" in out["error"]


async def test_a_model_returning_the_wrong_type_is_handled():
    out = await _run(StubModel(result={"items": []}))

    assert out["extraction"] is None
    assert "no valid ShoppingExtraction" in out["error"]


# --- accounting ------------------------------------------------------------ #


async def test_a_successful_extraction_is_recorded(tmp_path: Path):
    cassette = Cassette(tmp_path / "cassettes")
    record = _record()

    await _run(StubModel(ShoppingExtraction(items=[ShoppingItem(name="bananas")])),
               record, cassette=cassette)

    entries = cassette.all_entries()
    assert len(entries) == 1
    assert entries[0].thought_id == record.id
    assert entries[0].model == "stub (shopping)"
    assert entries[0].error is None


async def test_a_failed_extraction_is_recorded_too(tmp_path: Path):
    """Spend and failure both have to be visible; a silent failure looks like a
    thought that simply had nothing to buy."""
    cassette = Cassette(tmp_path / "cassettes")

    await _run(StubModel(error=RuntimeError("boom")), cassette=cassette)

    entries = cassette.all_entries()
    assert len(entries) == 1
    assert "boom" in (entries[0].error or "")
