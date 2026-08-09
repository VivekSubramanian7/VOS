"""`run_pulse` — fetch a digest, record what it cost, project it.

The spend tests are the ones that matter. `BudgetGuard` sums cassette entries, and a
pulse is not a thought, so nothing records it unless this module does. An unrecorded
digest is free money as far as the guard is concerned.
"""

from __future__ import annotations

import contextlib

import pytest
import pytest_asyncio

from vos.contracts import PulseArtifact, PulseDigest, PulseError, PulsePost, SourceRef
from vos.projection import run_pulse


class StubFetcher:
    def __init__(self, artifact=None, dropped: int = 0, error: str | None = None):
        self.artifact = artifact
        self.dropped = dropped
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    async def fetch(self, topic: str, handles: list[str]):
        self.calls.append((topic, handles))
        if self.error:
            raise PulseError(self.error)
        return self.artifact, self.dropped


def _artifact(*posts: PulsePost, cost: float = 0.63) -> PulseArtifact:
    from datetime import UTC, datetime

    return PulseArtifact(
        digest=PulseDigest(
            topic="AI",
            summary="s",
            posts=list(posts),
            asked_at=datetime(2026, 8, 9, tzinfo=UTC),
        ),
        raw_response={},
        fetched_at=datetime(2026, 8, 9, tzinfo=UTC),
        model="grok-4.1-fast",
        sources_used=25,
        cost_usd=cost,
    )


def _post(n: int = 1) -> PulsePost:
    return PulsePost(
        text="a claim",
        author_handle="@karpathy",
        url=f"https://x.com/karpathy/status/{n}",
    )


class FakePulseGraph:
    """In-memory stand-in — the pulse path needs only these three methods."""

    def __init__(self, *, fail: bool = False) -> None:
        self.pulses: dict[str, object] = {}
        self.sources: list[SourceRef] = []
        self.fail = fail

    async def following(self) -> list[SourceRef]:
        return list(self.sources)

    async def follow(self, source: SourceRef) -> None:
        self.sources.append(source)

    async def save_pulse(self, digest) -> int:
        if self.fail:
            raise RuntimeError("neo4j down")
        self.pulses[digest.topic] = digest
        return len(digest.posts)


@pytest.fixture
def pulse_graph() -> FakePulseGraph:
    return FakePulseGraph()


async def test_run_pulse_projects_and_reports(pulse_graph):
    result = await run_pulse(StubFetcher(_artifact(_post())), pulse_graph, "AI")
    assert result.ok
    assert result.post_count == 1
    assert result.cost_usd == 0.63


async def test_only_x_handles_are_passed_to_the_fetcher(pulse_graph):
    """A followed book must not become a search filter."""
    await pulse_graph.follow(SourceRef(name="@karpathy", kind="x"))
    await pulse_graph.follow(SourceRef(name="Sapiens", kind="book"))
    fetcher = StubFetcher(_artifact(_post()))

    await run_pulse(fetcher, pulse_graph, "AI")
    assert fetcher.calls == [("AI", ["@karpathy"])]


async def test_a_fetch_failure_is_reported_not_raised(pulse_graph):
    result = await run_pulse(StubFetcher(error="xAI request failed"), pulse_graph, "AI")
    assert not result.ok
    assert "xAI request failed" in (result.error or "")


async def test_nothing_is_written_when_the_fetch_fails(pulse_graph):
    await run_pulse(StubFetcher(error="boom"), pulse_graph, "AI")
    assert pulse_graph.pulses == {}


async def test_the_spend_is_recorded_even_though_it_is_not_a_thought(tmp_path):
    """BudgetGuard sums cassette entries, so an unrecorded pulse is free money."""
    from vos.cassette import BudgetGuard, Cassette

    cassette = Cassette(tmp_path)
    graph = FakePulseGraph()
    await run_pulse(StubFetcher(_artifact(_post())), graph, "AI", cassette=cassette)

    assert BudgetGuard(cassette, 2.0).spent_today() == pytest.approx(0.63)


async def test_the_spend_is_recorded_even_if_the_graph_write_fails(tmp_path):
    """The money left the account whether or not the projection succeeded."""
    from vos.cassette import BudgetGuard, Cassette

    cassette = Cassette(tmp_path)
    graph = FakePulseGraph(fail=True)
    with contextlib.suppress(Exception):
        await run_pulse(StubFetcher(_artifact(_post())), graph, "AI", cassette=cassette)

    assert BudgetGuard(cassette, 2.0).spent_today() == pytest.approx(0.63)


async def test_dropped_items_are_carried_through(pulse_graph):
    result = await run_pulse(StubFetcher(_artifact(_post()), dropped=3), pulse_graph, "AI")
    assert result.dropped == 3


# =========================================================================== #
# Shopping
# =========================================================================== #


class StubShoppingPipeline:
    def __init__(self, result: dict | None = None):
        self.result = result or {}
        self.calls = 0

    async def ainvoke(self, state):
        self.calls += 1
        return self.result


def _capture(text: str = "buy bananas", n: int = 1):
    from datetime import UTC, datetime

    from vos.contracts import CaptureRecord

    return CaptureRecord.create(
        chat_id=42, message_id=n, text=text,
        captured_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
    )


@pytest_asyncio.fixture
async def shopping(tmp_path):
    from vos.shopping import SqliteShoppingStore

    store = SqliteShoppingStore(tmp_path / "shopping.db")
    await store.ensure_schema()
    try:
        yield store
    finally:
        await store.close()


async def test_process_shopping_projects_and_reports(shopping):
    from vos.contracts import ShoppingExtraction, ShoppingItem
    from vos.projection import process_shopping

    record = _capture()
    pipeline = StubShoppingPipeline(
        {"extraction": ShoppingExtraction(items=[ShoppingItem(name="bananas")]), "error": None}
    )

    result = await process_shopping(pipeline, shopping, record)

    assert result.ok
    assert [i.name for i in result.items] == ["bananas"]
    assert [i.name for i in await shopping.pending_items()] == ["bananas"]


async def test_a_failed_extraction_is_reported_not_raised(shopping):
    from vos.projection import process_shopping

    record = _capture()
    pipeline = StubShoppingPipeline({"extraction": None, "error": "provider is down"})

    result = await process_shopping(pipeline, shopping, record)

    assert not result.ok
    assert result.error == "provider is down"
    assert await shopping.pending_items() == []


async def test_a_failure_does_not_count_as_extracted(shopping):
    """Otherwise a transient provider outage would drop the thought off the list
    permanently, with nothing to say it had ever been meant to be there."""
    from vos.projection import process_shopping

    record = _capture()
    await process_shopping(
        StubShoppingPipeline({"extraction": None, "error": "boom"}), shopping, record
    )

    assert await shopping.extracted_ok_ids() == set()


async def test_an_empty_extraction_still_counts_as_extracted(shopping):
    from vos.contracts import ShoppingExtraction
    from vos.projection import process_shopping

    record = _capture("should I get the Sony or the Bose?")
    result = await process_shopping(
        StubShoppingPipeline({"extraction": ShoppingExtraction(items=[]), "error": None}),
        shopping,
        record,
    )

    assert result.ok and result.items == []
    assert await shopping.extracted_ok_ids() == {record.id}


async def test_replay_item_marks_reapplies_ticks(shopping, tmp_path):
    from datetime import UTC, datetime

    from vos.contracts import ItemMark, ShoppingExtraction, ShoppingItem
    from vos.journal import JsonlJournal
    from vos.projection import process_shopping, replay_item_marks

    journal = JsonlJournal(tmp_path / "journal")
    record = _capture()
    await journal.append(record)
    await journal.append(
        ItemMark(name="bananas", action="bought", at=datetime(2026, 8, 9, 12, tzinfo=UTC))
    )

    await process_shopping(
        StubShoppingPipeline(
            {"extraction": ShoppingExtraction(items=[ShoppingItem(name="bananas")]),
             "error": None}
        ),
        shopping,
        record,
    )
    assert len(await shopping.pending_items()) == 1

    assert await replay_item_marks(journal, shopping) == 1
    assert await shopping.pending_items() == []


async def test_replaying_marks_twice_changes_nothing(shopping, tmp_path):
    """Startup replays on every boot, so this runs far more often than it matters."""
    from datetime import UTC, datetime

    from vos.contracts import ItemMark
    from vos.journal import JsonlJournal
    from vos.projection import replay_item_marks

    journal = JsonlJournal(tmp_path / "journal")
    await journal.append(
        ItemMark(name="bananas", action="bought", at=datetime(2026, 8, 9, 12, tzinfo=UTC))
    )

    await replay_item_marks(journal, shopping)
    await replay_item_marks(journal, shopping)

    assert await shopping.bought_count() == 1
