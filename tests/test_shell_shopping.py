"""Shell tests for the shopping flow — dispatch, the inline keyboard, and the taps.

The fakes are shared with `test_shell.py` rather than duplicated, so the Telegram
surface behaves identically in both.

Two properties carry most of these tests. First, the journal leads: a tick that was not
durably recorded must not appear to have happened. Second, a stale keyboard refuses
rather than guessing — buttons outlive the list they were drawn from, and buying the
wrong thing is discovered at the till.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from test_shell import (
    FakeCommand,
    FakeGraph,
    FakeMessage,
    FakePipeline,
    _classification,
)

from vos.contracts import Classification, ItemView, canonical
from vos.jobs import JobQueue
from vos.journal import JsonlJournal
from vos.shell import VosBot, _item_ref, resolve_item


class StubShoppingPipeline:
    def __init__(self, result: dict | None = None):
        self.result = result or {"extraction": None, "error": "nothing stubbed"}
        self.calls: list[str] = []

    async def ainvoke(self, state) -> dict:
        self.calls.append(state.record.text)
        return self.result


class FakeCallback:
    """A tap. Carries the message it came from, so edit-in-place is observable."""

    def __init__(self, data: str, message: Any = None) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **_: Any) -> None:
        self.answers.append((text, show_alert))

    @property
    def toast(self) -> str:
        return self.answers[-1][0] if self.answers else ""

    @property
    def alerted(self) -> bool:
        return bool(self.answers and self.answers[-1][1])


def _shopping_classification() -> Classification:
    return Classification(
        category="Shopping", title="Groceries", summary="Things to buy.", confidence=0.9
    )


async def _shopping_bot(
    tmp_path: Path, items: list[str] | None = None, error: str | None = None
):
    from vos.contracts import ShoppingExtraction, ShoppingItem
    from vos.shopping import SqliteShoppingStore

    store = SqliteShoppingStore(tmp_path / "shopping.db")
    await store.ensure_schema()

    extraction = (
        None
        if error
        else ShoppingExtraction(
            items=[ShoppingItem(name=n) for n in (items if items is not None else
                                                 ["bananas", "oat milk"])]
        )
    )
    jobs = JobQueue()
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=FakePipeline(_shopping_classification()),
        shopping=store,
        shopping_pipeline=StubShoppingPipeline({"extraction": extraction, "error": error}),
        jobs=jobs,
    )
    return bot, jobs, store


async def _capture(bot: VosBot, jobs: JobQueue, text: str = "need bananas", n: int = 1):
    await jobs.start()
    message = FakeMessage(text, message_id=n)
    await bot.capture(message)  # type: ignore[arg-type]
    await jobs.drain()
    await jobs.stop()
    return message


# --- dispatch ---------------------------------------------------------------- #


async def test_a_shopping_thought_is_queued_not_extracted_inline(tmp_path: Path):
    """Capture must return immediately; extraction is a second model call."""
    bot, jobs, store = await _shopping_bot(tmp_path)
    await jobs.start()

    message = FakeMessage("need bananas and oat milk")
    await bot.capture(message)  # type: ignore[arg-type]

    assert "Shopping" in message.replies[0].final
    assert await store.pending_items() == [], "the worker has not run yet"

    await jobs.drain()
    await jobs.stop()

    assert [i.name for i in await store.pending_items()] == ["bananas", "oat milk"]
    assert "Added to your list" in message.replies[-1].final
    await store.close()


async def test_a_non_shopping_thought_extracts_nothing(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    bot.pipeline = FakePipeline(_classification())  # TripPlanning

    await _capture(bot, jobs, "book flights to Tokyo")

    assert bot.shopping_pipeline.calls == []
    await store.close()


async def test_a_video_thought_does_not_reach_the_shopping_pipeline(tmp_path: Path):
    """The two dispatches are exclusive; a link must not be shopped for."""
    bot, jobs, store = await _shopping_bot(tmp_path)
    bot.pipeline = FakePipeline(
        Classification(category="VideoKnowledge", title="v", summary="s", confidence=0.9)
    )

    await _capture(bot, jobs, "watch https://youtu.be/dQw4w9WgXcQ")

    assert bot.shopping_pipeline.calls == []
    await store.close()


async def test_an_extraction_failure_leaves_the_thought_intact(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path, error="provider is down")

    message = await _capture(bot, jobs)

    assert [r.text for r in bot.journal.records()] == ["need bananas"]
    assert "Couldn't turn that into a list" in message.replies[-1].final
    assert await store.extracted_ok_ids() == set()
    await store.close()


async def test_an_empty_extraction_says_so(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path, items=[])

    message = await _capture(bot, jobs, "should I get the Sony or the Bose?")

    assert "Nothing to add" in message.replies[-1].final
    await store.close()


# --- /shopping and the keyboard ---------------------------------------------- #


async def test_shopping_lists_items_with_a_button_each(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    message = FakeMessage()
    await bot.cmd_shopping(message)  # type: ignore[arg-type]

    assert "1. bananas" in message.last
    buttons = message.replies[-1].markup.inline_keyboard
    assert [row[0].text for row in buttons] == ["✓ bananas", "✓ oat milk"]
    await store.close()


async def test_an_empty_list_says_so_and_offers_no_buttons(tmp_path: Path):
    bot, _, store = await _shopping_bot(tmp_path)

    message = FakeMessage()
    await bot.cmd_shopping(message)  # type: ignore[arg-type]

    assert "Nothing on it" in message.last
    assert message.replies[-1].markup is None
    await store.close()


async def test_undo_appears_only_once_something_is_bought(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    first = FakeMessage()
    await bot.cmd_shopping(first)  # type: ignore[arg-type]
    assert all(
        "Undo" not in row[0].text for row in first.replies[-1].markup.inline_keyboard
    )

    item = (await store.pending_items())[0]
    await bot.on_buy_callback(FakeCallback(_item_ref(item)))  # type: ignore[arg-type]

    second = FakeMessage()
    await bot.cmd_shopping(second)  # type: ignore[arg-type]
    assert any(
        "Undo" in row[0].text for row in second.replies[-1].markup.inline_keyboard
    )
    await store.close()


async def test_callback_data_stays_within_telegrams_budget(tmp_path: Path):
    """64 bytes is a hard transport limit and item names are arbitrary user text."""
    bot, jobs, store = await _shopping_bot(
        tmp_path, items=["a" * 79, "ünïcödé wíth émöjí 🍌🍌🍌"]
    )
    await _capture(bot, jobs)

    for item in await store.pending_items():
        assert len(_item_ref(item).encode("utf-8")) <= 64
    await store.close()


# --- tapping ------------------------------------------------------------------ #


async def test_tapping_buys_the_item_and_redraws_in_place(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    listing = FakeMessage()
    await bot.cmd_shopping(listing)  # type: ignore[arg-type]
    rendered = listing.replies[-1]

    item = (await store.pending_items())[0]
    callback = FakeCallback(_item_ref(item), message=rendered)
    await bot.on_buy_callback(callback)  # type: ignore[arg-type]

    assert item.name in callback.toast
    assert [i.name for i in await store.pending_items()] == ["oat milk"]
    assert "bananas" not in rendered.final, "the list was not redrawn in place"
    await store.close()


async def test_a_tap_is_journalled_before_it_is_applied(tmp_path: Path):
    """The journal leads and the projection follows — the contract /undo also keeps."""
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)
    item = (await store.pending_items())[0]

    seen: list[int] = []
    original = store.mark

    async def spy(name: str, action: str, at):
        seen.append(len(bot.journal.item_marks()))
        return await original(name, action, at)

    store.mark = spy  # type: ignore[method-assign]
    await bot.on_buy_callback(FakeCallback(_item_ref(item)))  # type: ignore[arg-type]

    assert seen == [1], "the store was written before the journal had the mark"
    await store.close()


async def test_a_journal_failure_leaves_the_list_untouched(tmp_path: Path, monkeypatch):
    """A tick that was not durably recorded must not appear to have happened."""
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)
    item = (await store.pending_items())[0]

    async def boom(_entry):
        raise OSError("disk full")

    monkeypatch.setattr(bot.journal, "append", boom)
    callback = FakeCallback(_item_ref(item))
    await bot.on_buy_callback(callback)  # type: ignore[arg-type]

    assert [i.name for i in await store.pending_items()] == ["bananas", "oat milk"]
    assert callback.alerted, "a silent failure reads as success"
    await store.close()


async def test_a_stale_button_refuses_rather_than_buying_the_wrong_thing(tmp_path: Path):
    """Row ids are reused after a rebuild. The digest is what turns that into a
    refusal instead of ticking off whatever now occupies the row."""
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)
    item = (await store.pending_items())[0]

    callback = FakeCallback(f"buy:{item.id}:deadbeef")
    await bot.on_buy_callback(callback)  # type: ignore[arg-type]

    assert "out of date" in callback.toast
    assert bot.journal.item_marks() == []
    assert len(await store.pending_items()) == 2
    await store.close()


async def test_an_unparseable_callback_is_refused(tmp_path: Path):
    bot, _, store = await _shopping_bot(tmp_path)

    callback = FakeCallback("buy:not-a-number:abc")
    await bot.on_buy_callback(callback)  # type: ignore[arg-type]

    assert "recognise" in callback.toast
    assert bot.journal.item_marks() == []
    await store.close()


async def test_undo_puts_the_last_item_back(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)
    item = (await store.pending_items())[0]
    await bot.on_buy_callback(FakeCallback(_item_ref(item)))  # type: ignore[arg-type]

    await bot.on_unbuy_callback(FakeCallback("unbuy:last"))  # type: ignore[arg-type]

    assert [i.name for i in await store.pending_items()] == ["bananas", "oat milk"]
    assert [m.action for m in bot.journal.item_marks()] == ["bought", "unbought"]
    await store.close()


async def test_undo_with_nothing_bought(tmp_path: Path):
    bot, _, store = await _shopping_bot(tmp_path)

    callback = FakeCallback("unbuy:last")
    await bot.on_unbuy_callback(callback)  # type: ignore[arg-type]

    assert "Nothing to undo" in callback.toast
    await store.close()


# --- /bought ------------------------------------------------------------------ #


async def test_bought_by_number_matches_the_rendered_list(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    message = FakeMessage()
    await bot.cmd_bought(message, FakeCommand("2"))  # type: ignore[arg-type]

    assert "oat milk" in message.last
    assert [i.name for i in await store.pending_items()] == ["bananas"]
    await store.close()


async def test_bought_by_name_is_case_and_space_insensitive(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    await bot.cmd_bought(FakeMessage(), FakeCommand("  OAT   MILK "))  # type: ignore[arg-type]

    assert [i.name for i in await store.pending_items()] == ["bananas"]
    await store.close()


async def test_bought_is_journalled(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    await bot.cmd_bought(FakeMessage(), FakeCommand("bananas"))  # type: ignore[arg-type]

    assert [(m.name, m.action) for m in bot.journal.item_marks()] == [("bananas", "bought")]
    await store.close()


async def test_an_ambiguous_term_asks_rather_than_guessing(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path, items=["oat milk", "almond milk"])
    await _capture(bot, jobs)

    message = FakeMessage()
    await bot.cmd_bought(message, FakeCommand("milk"))  # type: ignore[arg-type]

    assert "Which one?" in message.last
    assert len(await store.pending_items()) == 2
    assert bot.journal.item_marks() == []
    await store.close()


async def test_an_unmatched_term_says_so(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs)

    message = FakeMessage()
    await bot.cmd_bought(message, FakeCommand("caviar"))  # type: ignore[arg-type]

    assert "Nothing on the list matches" in message.last
    await store.close()


async def test_bought_without_an_argument_shows_usage(tmp_path: Path):
    bot, _, store = await _shopping_bot(tmp_path)

    message = FakeMessage()
    await bot.cmd_bought(message, FakeCommand(None))  # type: ignore[arg-type]

    assert "Usage" in message.last
    await store.close()


# --- resolve_item (pure) ------------------------------------------------------ #


def _view(n: int, name: str) -> ItemView:
    return ItemView(
        id=n,
        name=name,
        canonical_name=canonical(name),
        added_at=datetime(2026, 8, 9, tzinfo=UTC),
    )


def test_resolve_item_prefers_an_exact_name_over_a_substring():
    """'milk' must tick off milk, not oat milk, when both are on the list."""
    match, candidates = resolve_item("milk", [_view(1, "milk"), _view(2, "oat milk")])

    assert match is not None and match.name == "milk"
    assert candidates == []


def test_resolve_item_accepts_an_unambiguous_substring():
    match, _ = resolve_item("oat", [_view(1, "oat milk"), _view(2, "bananas")])
    assert match is not None and match.name == "oat milk"


def test_resolve_item_returns_candidates_when_ambiguous():
    match, candidates = resolve_item(
        "milk", [_view(1, "oat milk"), _view(2, "almond milk")]
    )

    assert match is None
    assert [c.name for c in candidates] == ["oat milk", "almond milk"]


def test_resolve_item_rejects_an_out_of_range_number():
    assert resolve_item("9", [_view(1, "bananas")]) == (None, [])
    assert resolve_item("0", [_view(1, "bananas")]) == (None, [])


def test_resolve_item_on_an_empty_list():
    assert resolve_item("bananas", []) == (None, [])


# --- startup recovery --------------------------------------------------------- #


async def test_recovery_requeues_nothing_when_everything_is_extracted(tmp_path: Path):
    bot, jobs, store = await _shopping_bot(tmp_path)
    await _capture(bot, jobs, "need bananas")

    bot.shopping_pipeline.calls.clear()
    queued = await bot.requeue_unextracted_shopping(FakeMessage().answer)

    assert queued == 0
    assert bot.shopping_pipeline.calls == []
    await store.close()


async def test_recovery_picks_up_a_thought_the_store_never_finished(tmp_path: Path):
    """A failed extraction leaves no success marker, so the work is still owed."""
    bot, jobs, store = await _shopping_bot(tmp_path, error="provider is down")
    await _capture(bot, jobs, "need bananas")

    bot.shopping_pipeline.calls.clear()
    await jobs.start()
    queued = await bot.requeue_unextracted_shopping(FakeMessage().answer)
    await jobs.drain()
    await jobs.stop()

    assert queued == 1
    assert bot.shopping_pipeline.calls == ["need bananas"]
    await store.close()


async def test_recovery_does_nothing_without_a_store(tmp_path: Path):
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=FakePipeline(_shopping_classification()),
    )
    assert await bot.requeue_unextracted_shopping(FakeMessage().answer) == 0
