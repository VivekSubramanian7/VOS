"""Shopping store tests.

No Docker here: the list is SQLite, so the rules that actually decide what you see at
the shop — dedup, the pending/bought gate, and convergence under out-of-order replay —
are all testable in milliseconds against a temp file.

The property under test throughout is commutativity. Startup recovery, async
re-extraction, and `--rebuild` all deliver events in whatever order they happen to
arrive, so a list that depended on that order would be quietly wrong rather than
loudly broken.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from vos.contracts import ShoppingItem, thought_id
from vos.shopping import SqliteShoppingStore

T0 = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _item(name: str, quantity: str | None = None, note: str | None = None) -> ShoppingItem:
    return ShoppingItem(name=name, quantity=quantity, note=note)


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = SqliteShoppingStore(tmp_path / "shopping.db")
    await s.ensure_schema()
    try:
        yield s
    finally:
        await s.close()


TH1 = thought_id(42, 1)
TH2 = thought_id(42, 2)
TH3 = thought_id(42, 3)


# --- schema and basics ---------------------------------------------------- #


async def test_ensure_schema_is_idempotent(store: SqliteShoppingStore):
    await store.ensure_schema()
    await store.ensure_schema()
    assert await store.pending_items() == []


async def test_add_then_read_back(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas"), _item("oat milk", "2L")], _at(0))

    items = await store.pending_items()
    assert [i.name for i in items] == ["bananas", "oat milk"]
    assert items[1].quantity == "2L"
    assert all(i.status == "pending" for i in items)


async def test_items_dedupe_on_canonical_name(store: SqliteShoppingStore):
    """Two spellings across two thoughts are one thing to buy, with two requests
    behind it."""
    await store.add(TH1, [_item("Oat  Milk")], _at(0))
    await store.add(TH2, [_item("oat milk")], _at(5))

    items = await store.pending_items()
    assert len(items) == 1
    assert items[0].name == "Oat  Milk", "first spelling wins the display name"
    assert items[0].sources == 2


async def test_latest_qualifier_wins(store: SqliteShoppingStore):
    """Asking plainly after asking for 2L should stop showing 2L."""
    await store.add(TH1, [_item("oat milk", "2L")], _at(0))
    await store.add(TH2, [_item("oat milk")], _at(5))

    assert (await store.pending_items())[0].quantity is None


async def test_pending_is_ordered_oldest_first(store: SqliteShoppingStore):
    """Stable numbering is what makes `/bought 2` mean the row the user just read."""
    await store.add(TH1, [_item("bananas")], _at(10))
    await store.add(TH2, [_item("bread")], _at(20))

    assert [i.name for i in await store.pending_items()] == ["bananas", "bread"]


async def test_reextraction_replaces_rather_than_accumulates(store: SqliteShoppingStore):
    """Re-running after a prompt change must correct the list, not double it."""
    await store.add(TH1, [_item("bananas"), _item("oranges")], _at(0))
    await store.add(TH1, [_item("bananas")], _at(0))

    assert [i.name for i in await store.pending_items()] == ["bananas"]


async def test_an_item_wanted_by_another_thought_survives_reextraction(
    store: SqliteShoppingStore,
):
    await store.add(TH1, [_item("bananas")], _at(0))
    await store.add(TH2, [_item("bananas")], _at(1))
    await store.add(TH1, [], _at(0))

    assert [i.name for i in await store.pending_items()] == ["bananas"]


async def test_projecting_twice_equals_projecting_once(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas", "6")], _at(0))
    first = await store.pending_items()
    await store.add(TH1, [_item("bananas", "6")], _at(0))

    assert [i.model_dump() for i in await store.pending_items()] == [
        i.model_dump() for i in first
    ]


# --- marks ---------------------------------------------------------------- #


async def test_mark_bought_moves_it_off_the_list(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas")], _at(0))

    assert await store.mark("bananas", "bought", _at(30)) == "bananas"
    assert await store.pending_items() == []
    assert await store.bought_count() == 1


async def test_mark_matches_on_canonical_name(store: SqliteShoppingStore):
    await store.add(TH1, [_item("Oat Milk")], _at(0))
    assert await store.mark("  oat   milk ", "bought", _at(30)) == "Oat Milk"


async def test_unbought_puts_it_back(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas")], _at(0))
    await store.mark("bananas", "bought", _at(30))
    await store.mark("bananas", "unbought", _at(31))

    items = await store.pending_items()
    assert [i.name for i in items] == ["bananas"]
    assert items[0].bought_at is None


async def test_a_stale_mark_is_ignored(store: SqliteShoppingStore):
    """Replay delivers old events after new ones; the later decision must stand."""
    await store.add(TH1, [_item("bananas")], _at(0))
    await store.mark("bananas", "bought", _at(30))

    assert await store.mark("bananas", "unbought", _at(10)) is None
    assert await store.pending_items() == []


async def test_a_mark_for_an_unknown_item_creates_it(store: SqliteShoppingStore):
    """Rebuild replays ticks before re-extraction has run. Dropping them here would
    silently reset the list — the exact failure journalling the mark exists to prevent.
    """
    assert await store.mark("bananas", "bought", _at(30)) == "bananas"
    assert await store.bought_count() == 1


async def test_last_bought_is_what_undo_reverses(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas"), _item("bread")], _at(0))
    await store.mark("bananas", "bought", _at(30))
    await store.mark("bread", "bought", _at(31))

    last = await store.last_bought()
    assert last is not None and last.name == "bread"


async def test_last_bought_on_an_untouched_list(store: SqliteShoppingStore):
    assert await store.last_bought() is None


# --- the time rule -------------------------------------------------------- #


async def test_asking_again_after_buying_reopens_the_item(store: SqliteShoppingStore):
    """You bought milk on Monday and want more on Friday. That is a new request."""
    await store.add(TH1, [_item("oat milk")], _at(0))
    await store.mark("oat milk", "bought", _at(30))
    await store.add(TH2, [_item("oat milk")], _at(60))

    items = await store.pending_items()
    assert [i.name for i in items] == ["oat milk"]
    assert items[0].bought_at is None


async def test_replaying_an_older_thought_does_not_resurrect_a_bought_item(
    store: SqliteShoppingStore,
):
    """The failure this guards against is a rebuild un-buying everything you already
    picked up, which is the whole reason marks are journalled."""
    await store.add(TH1, [_item("oat milk")], _at(0))
    await store.mark("oat milk", "bought", _at(30))
    await store.add(TH1, [_item("oat milk")], _at(0))

    assert await store.pending_items() == []
    assert await store.bought_count() == 1


async def test_a_reopened_item_goes_to_the_bottom_of_the_list(store: SqliteShoppingStore):
    await store.add(TH1, [_item("oat milk")], _at(0))
    await store.mark("oat milk", "bought", _at(10))
    await store.add(TH2, [_item("bread")], _at(20))
    await store.add(TH3, [_item("oat milk")], _at(30))

    assert [i.name for i in await store.pending_items()] == ["bread", "oat milk"]


async def test_an_older_add_backdates_the_item(store: SqliteShoppingStore):
    await store.add(TH2, [_item("bananas")], _at(50))
    await store.add(TH1, [_item("bananas")], _at(5))

    assert (await store.pending_items())[0].added_at == _at(5)


@pytest.mark.parametrize(
    "order",
    [(0, 1, 2), (2, 1, 0), (1, 0, 2), (2, 0, 1), (1, 2, 0), (0, 2, 1)],
)
async def test_replay_converges_regardless_of_order(tmp_path: Path, order: tuple[int, ...]):
    """The load-bearing property: rebuild applies captures and marks in whatever order
    it reaches them, so the final list must not depend on that order."""
    events = [
        ("add", TH1, [_item("oat milk")], _at(0)),
        ("mark", "oat milk", "bought", _at(30)),
        ("add", TH2, [_item("bananas")], _at(60)),
    ]

    store = SqliteShoppingStore(tmp_path / f"shopping-{''.join(map(str, order))}.db")
    await store.ensure_schema()
    try:
        for index in order:
            event = events[index]
            if event[0] == "add":
                await store.add(event[1], event[2], event[3])  # type: ignore[arg-type]
            else:
                await store.mark(event[1], event[2], event[3])  # type: ignore[arg-type]

        assert [i.name for i in await store.pending_items()] == ["bananas"]
        assert await store.bought_count() == 1
    finally:
        await store.close()


# --- extraction markers --------------------------------------------------- #


async def test_a_successful_extraction_with_no_items_still_counts_as_done(
    store: SqliteShoppingStore,
):
    """'Sony or Bose?' is a Shopping thought with nothing to buy. Without the marker
    it would be re-extracted on every restart, forever."""
    await store.add(TH1, [], _at(0))
    await store.record_extraction(TH1)

    assert await store.pending_items() == []
    assert await store.extracted_ok_ids() == {TH1}


async def test_a_failed_extraction_is_not_done(store: SqliteShoppingStore):
    await store.record_extraction(TH1, error="provider timed out")
    assert await store.extracted_ok_ids() == set()


async def test_a_retry_replaces_the_failure(store: SqliteShoppingStore):
    await store.record_extraction(TH1, error="provider timed out")
    await store.record_extraction(TH1)
    assert await store.extracted_ok_ids() == {TH1}


async def test_item_by_id_round_trips(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas")], _at(0))
    item = (await store.pending_items())[0]

    fetched = await store.item_by_id(item.id)
    assert fetched is not None and fetched.canonical_name == "bananas"
    assert await store.item_by_id(item.id + 999) is None


async def test_wipe_empties_everything(store: SqliteShoppingStore):
    await store.add(TH1, [_item("bananas")], _at(0))
    await store.mark("bananas", "bought", _at(30))
    await store.record_extraction(TH1)

    await store.wipe()

    assert await store.pending_items() == []
    assert await store.bought_count() == 0
    assert await store.extracted_ok_ids() == set()


async def test_state_survives_reopening_the_file(tmp_path: Path):
    """It is a projection, not a cache: a restart must not lose the list."""
    path = tmp_path / "shopping.db"
    first = SqliteShoppingStore(path)
    await first.ensure_schema()
    await first.add(TH1, [_item("bananas")], _at(0))
    await first.close()

    second = SqliteShoppingStore(path)
    await second.ensure_schema()
    try:
        assert [i.name for i in await second.pending_items()] == ["bananas"]
    finally:
        await second.close()


async def test_uuid_round_trips_through_the_marker_table(store: SqliteShoppingStore):
    await store.record_extraction(TH1)
    ids = await store.extracted_ok_ids()
    assert isinstance(next(iter(ids)), UUID)
