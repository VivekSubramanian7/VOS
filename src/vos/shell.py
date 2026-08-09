"""Telegram gateway.

aiogram 3, long-polling. No webhook, no public URL, no tunnel — which is why the dev
machine and a VPS behave identically and the move between them needs no code change.

The capture handler implements §8.1, and the ordering there is the contract:

    journal.append()  ->  fsync  ->  ack  ->  classify  ->  update

Nothing before the fsync may be skipped, and nothing after it may be allowed to fail
the capture. If the journal write raises, the user is told the thought was NOT saved —
a false "captured" is the one outcome the design refuses.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import escape
from typing import Any
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from vos.cassette import BudgetGuard, Cassette
from vos.contracts import (
    CaptureRecord,
    CaptureResult,
    ItemMark,
    ItemView,
    SourceKind,
    SourceRef,
    canonical,
)
from vos.graph import Neo4jGraph
from vos.jobs import JobQueue
from vos.journal import JsonlJournal
from vos.projection import (
    classify_one,
    process_shopping,
    process_video,
    reproject_missing,
    run_pulse,
)
from vos.pulse import normalise_handle
from vos.render import (
    HELP,
    render_all_notes,
    render_all_posts,
    render_capture,
    render_following,
    render_pulse,
    render_search,
    render_shopping_list,
    render_shopping_update,
    render_stats,
    render_thoughts,
    render_video,
    split_message,
)
from vos.video import VideoFetcher, extract_video_id, find_video_id

log = logging.getLogger(__name__)


async def _send(answer: Callable[[str], Awaitable[Any]], text: str) -> None:
    """Deliver rendered output, split if Telegram would reject it as too long.

    Every list-shaped reply needs this, not just the video one: `/recent 50` alone
    renders well past the 4096-character ceiling with no model involved.
    """
    for chunk in split_message(text):
        await answer(chunk)


async def _replace(
    note: Any, answer: Callable[[str], Awaitable[Any]], text: str
) -> None:
    """Overwrite a placeholder message, continuing into follow-ups if it won't fit.

    `edit_text` carries exactly one message, so the remainder has to be sent beside it.
    """
    first, *rest = split_message(text)
    await note.edit_text(first)
    for chunk in rest:
        await answer(chunk)


class VosBot:
    """Wires the components together and exposes an aiogram Router.

    Dependencies are injected rather than constructed here, so the whole shell can be
    driven in tests with a fake journal/graph/pipeline and no Telegram at all.
    """

    def __init__(
        self,
        *,
        journal: JsonlJournal,
        graph: Neo4jGraph,
        pipeline: Any,
        cassette: Cassette | None = None,
        budget: BudgetGuard | None = None,
        video_pipeline: Any = None,
        jobs: JobQueue | None = None,
        pulse_fetcher: Any = None,
        pulse_topic: str = "AI",
        shopping: Any = None,
        shopping_pipeline: Any = None,
    ) -> None:
        self.journal = journal
        self.graph = graph
        self.pipeline = pipeline
        self.cassette = cassette
        self.budget = budget
        self.video_pipeline = video_pipeline
        self.jobs = jobs
        self.pulse_fetcher = pulse_fetcher
        self.pulse_topic = pulse_topic
        self.shopping = shopping
        self.shopping_pipeline = shopping_pipeline

    # -- capture -------------------------------------------------------- #

    async def capture(self, message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        record = CaptureRecord.create(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=text,
            captured_at=datetime.now(UTC),
        )

        # --- durability boundary --------------------------------------- #
        try:
            await self.journal.append(record)
        except OSError as exc:
            # Refusing to ack is correct. Pretending to have saved it is not.
            log.exception("Journal write failed for %s", record.id)
            await message.answer(
                "❌ <b>Not saved.</b> I couldn't write to the journal "
                f"(<i>{exc.__class__.__name__}</i>). Please send it again."
            )
            return

        # From here on the thought is safe. Everything below is enrichment and is
        # allowed to fail without losing anything.
        with contextlib.suppress(Exception):
            await self.graph.upsert_thought(record, None)

        ack = await message.answer("📥 Captured…")
        result = await self._enrich(record)
        await _replace(ack, message.answer, render_capture(result))

        # Follow-on work gets queued rather than processed here: both are further model
        # calls, and the polling loop must not stall for them.
        if result.classification is None:
            return
        if (
            result.classification.category == "VideoKnowledge"
            and (video_id := find_video_id(text)) is not None
        ):
            await self._queue_video(message.answer, video_id, record.id)
        elif result.classification.category == "Shopping":
            await self._queue_shopping(message.answer, record)

    async def _enrich(self, record: CaptureRecord) -> CaptureResult:
        if self.budget and self.budget.exceeded():
            with contextlib.suppress(Exception):
                await self.graph.mark_unclassified(record.id, "daily budget reached")
            return CaptureResult(
                record=record, status="unclassified", error="daily budget reached"
            )

        try:
            classification, error, linked = await classify_one(
                self.pipeline, self.graph, record
            )
        except Exception as exc:  # noqa: BLE001 - e.g. the graph is down
            log.exception("Enrichment failed for %s", record.id)
            return CaptureResult(
                record=record, status="unclassified", error=f"{type(exc).__name__}: {exc}"
            )

        if classification is None:
            return CaptureResult(record=record, status="unclassified", error=error)

        return CaptureResult(
            record=record,
            classification=classification,
            linked_sources=linked,
            status="classified",
        )

    # -- video ---------------------------------------------------------- #

    async def _queue_video(
        self, answer: Callable[[str], Awaitable[Any]], video_id: str, thought_id: UUID | None
    ) -> None:
        """Enqueue distillation and post the result when it lands.

        Takes an `answer` callable rather than a Message so startup recovery — which
        has no incoming message to reply to — can pass `bot.send_message` instead.
        """
        if self.video_pipeline is None or self.jobs is None:
            return

        async def job() -> None:
            note = await answer("📺 Reading the transcript…")
            try:
                result = await process_video(
                    self.video_pipeline, self.graph, video_id, thought_id
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Video job failed for %s", video_id)
                await note.edit_text(
                    f"📺 Video processing failed: <i>{type(exc).__name__}</i>. "
                    "The thought itself is saved."
                )
                return
            await _replace(note, answer, render_video(result))

        await self.jobs.submit(f"video:{video_id}", job)

    async def cmd_video(self, message: Message, command: CommandObject) -> None:
        video_id = extract_video_id(command.args or "") or find_video_id(command.args or "")
        if video_id is None:
            await message.answer(
                "Give me a YouTube link: <code>/video https://youtu.be/…</code>\n"
                "<i>Only YouTube is supported for now.</i>"
            )
            return
        if self.jobs is None:
            await message.answer("Video processing isn't enabled.")
            return
        await self._queue_video(message.answer, video_id, None)

    async def cmd_redistil(self, message: Message, command: CommandObject) -> None:
        """Re-run distillation from the cached transcript — the point of caching it."""
        video_id = extract_video_id(command.args or "") or find_video_id(command.args or "")
        if video_id is None:
            await message.answer("Usage: <code>/redistil https://youtu.be/…</code>")
            return
        await self._queue_video(message.answer, video_id, None)

    # -- pulse ---------------------------------------------------------- #

    async def cmd_pulse(self, message: Message, command: CommandObject) -> None:
        """Best of the last 24 hours on X.

        Queued rather than inline: the search takes 10-30s and the polling loop must
        not stall. The budget is checked *before* queueing — a digest costs real money
        per source, so refusing early is the whole point of the guard.
        """
        if self.pulse_fetcher is None:
            await message.answer(
                "🐦 X pulse isn't enabled.\n"
                "Add <code>XAI_API_KEY=…</code> to your .env and restart.\n"
                "Keys come from <a href=\"https://console.x.ai\">console.x.ai</a>."
            )
            return
        if self.jobs is None:
            await message.answer("Background jobs aren't enabled.")
            return
        if self.budget and self.budget.exceeded():
            await message.answer(
                f"💸 Daily budget reached (${self.budget.spent_today():.2f}). "
                "No pulse — it would cost around $0.63."
            )
            return

        topic = (command.args or "").strip() or self.pulse_topic

        async def job() -> None:
            note = await message.answer(f"🐦 Reading X for “{escape(topic)}”…")
            try:
                result = await run_pulse(
                    self.pulse_fetcher, self.graph, topic, cassette=self.cassette
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Pulse job failed for %s", topic)
                await note.edit_text(
                    f"🐦 Pulse failed: <i>{type(exc).__name__}</i>."
                )
                return
            await _replace(note, message.answer, render_pulse(result))

        await self.jobs.submit(f"pulse:{topic}", job)

    # -- shopping ------------------------------------------------------- #

    async def _queue_shopping(
        self, answer: Callable[..., Awaitable[Any]], record: CaptureRecord
    ) -> None:
        """Enqueue item extraction and post the result when it lands.

        Takes an `answer` callable rather than a Message for the same reason
        `_queue_video` does: startup recovery has no incoming message to reply to.
        """
        if self.shopping_pipeline is None or self.shopping is None or self.jobs is None:
            return
        if self.budget and self.budget.exceeded():
            # Silent rather than a message: on the capture path the user has just been
            # told the budget is gone, and on the recovery path there is nobody to tell.
            # The thought stays unextracted, so the next restart picks it up.
            return

        async def job() -> None:
            note = await answer("🛒 Making a list…")
            try:
                result = await process_shopping(
                    self.shopping_pipeline, self.shopping, record
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Shopping job failed for %s", record.id)
                await note.edit_text(
                    f"🛒 Couldn't build the list: <i>{type(exc).__name__}</i>. "
                    "The thought itself is saved."
                )
                return
            await _replace(note, answer, render_shopping_update(result))

        await self.jobs.submit(f"shopping:{record.id}", job)

    async def _list_state(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """Render the list and the keyboard together.

        One place, because the two have to agree: a button pointing at a row the text
        does not show is precisely how a tap buys the wrong thing.
        """
        items = await self.shopping.pending_items()
        bought = await self.shopping.bought_count()
        undo = await self.shopping.last_bought()
        return (
            render_shopping_list(items, bought),
            _shopping_keyboard(items, undo=undo is not None),
        )

    async def cmd_shopping(self, message: Message) -> None:
        if self.shopping is None:
            await message.answer("🛒 Shopping isn't enabled.")
            return
        text, markup = await self._list_state()
        await _send_list(message.answer, text, markup)

    async def _tick(
        self, callback: CallbackQuery, name: str, action: str, toast: str
    ) -> None:
        """Journal the decision, apply it, then redraw.

        Same contract as `/undo`: the journal leads and the projection follows, so a
        crash in between costs a redraw rather than the decision — startup replay puts
        it back.
        """
        now = datetime.now(UTC)
        try:
            await self.journal.append(ItemMark(name=name, action=action, at=now))
        except OSError as exc:
            log.exception("Journal write failed for item mark %r", name)
            await callback.answer(
                f"Couldn't save that ({exc.__class__.__name__}). Try again.",
                show_alert=True,
            )
            return

        with contextlib.suppress(Exception):
            await self.shopping.mark(name, action, now)

        await callback.answer(toast)
        await self._redraw(callback.message)

    async def _redraw(self, message: Any) -> None:
        """Update the list in place.

        Editing rather than sending: in a shop the list should stay one message you
        keep glancing at, not a thread that grows by one every time you pick something
        up.
        """
        if message is None:
            return
        text, markup = await self._list_state()
        # Telegram raises rather than no-ops when the text is unchanged, and again when
        # a message is too old to edit. Neither is worth surfacing — the tick is saved
        # either way, and the next /shopping renders fresh.
        with contextlib.suppress(Exception):
            await message.edit_text(text, reply_markup=markup)

    async def on_buy_callback(self, callback: CallbackQuery) -> None:
        if self.shopping is None:
            await callback.answer("Shopping isn't enabled.")
            return

        item_id, digest = _parse_ref(callback.data or "")
        if item_id is None:
            await callback.answer("I don't recognise that button.")
            return

        item = await self.shopping.item_by_id(item_id)
        if item is None or _digest(item.canonical_name) != digest:
            # The row was reused by a rebuild, or the item is gone. Refusing is the
            # entire reason the digest is carried: the alternative is silently buying
            # whatever now occupies that row.
            await callback.answer(
                "That list is out of date — send /shopping again.", show_alert=True
            )
            return

        await self._tick(callback, item.name, "bought", f"✓ {item.name}")

    async def on_unbuy_callback(self, callback: CallbackQuery) -> None:
        """Undo the last tick — the correction for a mis-tap, which is the failure a
        keyboard of adjacent buttons actually has."""
        if self.shopping is None:
            await callback.answer("Shopping isn't enabled.")
            return
        last = await self.shopping.last_bought()
        if last is None:
            await callback.answer("Nothing to undo.")
            return
        await self._tick(callback, last.name, "unbought", f"↩ {last.name}")

    async def cmd_bought(self, message: Message, command: CommandObject) -> None:
        """Text fallback for the buttons.

        Kept because the `/shopping` message scrolls away mid-shop, and voice-typing
        "bought milk" beats scrolling back to find it.
        """
        if self.shopping is None:
            await message.answer("🛒 Shopping isn't enabled.")
            return

        term = (command.args or "").strip()
        if not term:
            await message.answer(
                "Usage: <code>/bought oat milk</code> or <code>/bought 2</code>"
            )
            return

        items = await self.shopping.pending_items()
        match, candidates = resolve_item(term, items)
        if match is None:
            if candidates:
                names = "\n".join(f"• {escape(i.name)}" for i in candidates)
                await message.answer(f"Which one?\n{names}")
            else:
                await message.answer(f"Nothing on the list matches “{escape(term)}”.")
            return

        now = datetime.now(UTC)
        try:
            await self.journal.append(ItemMark(name=match.name, action="bought", at=now))
        except OSError as exc:
            log.exception("Journal write failed for item mark %r", match.name)
            await message.answer(
                f"❌ Couldn't save that (<i>{exc.__class__.__name__}</i>). Try again."
            )
            return

        with contextlib.suppress(Exception):
            await self.shopping.mark(match.name, "bought", now)

        await message.answer(f"✓ <b>{escape(match.name)}</b>")

    async def requeue_unextracted_shopping(
        self, answer: Callable[..., Awaitable[Any]]
    ) -> int:
        """Startup recovery for the non-durable queue.

        Outstanding work is what the graph knows is filed under Shopping, minus what
        the store has finished with. Two stores, one question — the standing cost of
        keeping list state out of the graph (ADR-010), and it is paid only here.
        """
        if self.shopping_pipeline is None or self.shopping is None or self.jobs is None:
            return 0

        done = await self.shopping.extracted_ok_ids()
        by_id = {r.id: r for r in self.journal.records()}
        queued = 0
        for thought in await self.graph.category_thought_ids("Shopping"):
            record = by_id.get(thought)
            if thought in done or record is None:
                continue
            await self._queue_shopping(answer, record)
            queued += 1
        return queued

    async def cmd_more(self, message: Message, command: CommandObject) -> None:
        """Every stored claim for a video, not just the ones the reply had room for.

        Bare `/more` means the most recently distilled video, mirroring how `/undo`
        assumes the last thought — the overwhelmingly common case is "the one I just
        sent". Costs nothing: the notes are already in the graph, so this is a read,
        not another distillation.
        """
        raw = (command.args or "").strip()
        if raw:
            video_id = extract_video_id(raw) or find_video_id(raw)
            if video_id is None:
                await message.answer("Usage: <code>/more</code> or <code>/more &lt;url&gt;</code>")
                return
        else:
            # Whichever the user was most recently looking at, derived from the graph
            # rather than held as session state — the same trick /undo uses.
            pulse = await self.graph.latest_pulse()
            video = await self.graph.latest_video()
            if pulse and (video is None or pulse[1] >= video[1]):
                posts = await self.graph.posts_for_pulse(pulse[0])
                if posts:
                    await _send(message.answer, render_all_posts(posts))
                    return
                # A quiet day still MERGEs a :Pulse node with zero posts. Without this
                # fallback, that empty pulse would permanently block bare /more from
                # ever reaching the video notes again.
            if video is None:
                await message.answer("Nothing processed yet.")
                return
            video_id = video[0]

        notes = await self.graph.notes_for_video(video_id)
        await _send(message.answer, render_all_notes(notes))

    async def cmd_notes(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Usage: <code>/notes leverage</code>")
            return
        term = command.args.strip()
        notes = await self.graph.search_notes(term, 10)
        posts = await self.graph.search_posts(term, 10)
        await _send(message.answer, render_search(notes, posts, term))

    async def requeue_unprocessed_videos(
        self, answer: Callable[[str], Awaitable[Any]]
    ) -> int:
        """Startup recovery for the non-durable queue.

        Outstanding work is derived rather than stored: a VideoKnowledge thought with
        no :Video attached hasn't been processed.
        """
        if self.video_pipeline is None or self.jobs is None:
            return 0
        by_id = {r.id: r for r in self.journal.records()}
        queued = 0
        for view in await self.graph.unprocessed_video_thoughts():
            record = by_id.get(view.id)
            if record is None or (video_id := find_video_id(record.text)) is None:
                continue
            await self._queue_video(answer, video_id, record.id)
            queued += 1
        return queued

    # -- commands ------------------------------------------------------- #

    async def cmd_start(self, message: Message) -> None:
        await message.answer(HELP)

    async def cmd_recent(self, message: Message, command: CommandObject) -> None:
        n = _int_arg(command.args, default=10, maximum=50)
        views = await self.graph.recent(n)
        await _send(
            message.answer,
            render_thoughts(views, f"Last {len(views)}", "Nothing captured yet."),
        )

    async def cmd_category(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Usage: <code>/category TripPlanning</code>")
            return
        name = _match_category(command.args.strip())
        if name is None:
            await message.answer(f"Unknown category: <code>{command.args.strip()}</code>")
            return
        views = await self.graph.by_category(name, 20)
        await _send(message.answer, render_thoughts(views, name, f"Nothing in {name} yet."))

    async def cmd_search(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Usage: <code>/search oat milk</code>")
            return
        views = await self.graph.search(command.args.strip(), 20)
        await _send(
            message.answer,
            render_thoughts(views, f"Matches for “{command.args.strip()}”", "No matches."),
        )

    async def cmd_undo(self, message: Message) -> None:
        last = self.journal.last_capture()
        if last is None:
            await message.answer("Nothing to undo.")
            return
        from vos.contracts import Tombstone

        # Tombstone first: the journal leads, the graph follows.
        await self.journal.append(Tombstone(id=last.id, deleted_at=datetime.now(UTC)))
        with contextlib.suppress(Exception):
            await self.graph.soft_delete(last.id)
        await message.answer(f"🗑 Removed: <i>{last.text[:80]}</i>")

    async def cmd_stats(self, message: Message) -> None:
        stats = await self.graph.stats()
        spent = self.budget.spent_today() if self.budget else None
        await _send(message.answer, render_stats(stats, spent))

    async def cmd_pending(self, message: Message) -> None:
        views = await self.graph.pending()
        if not views:
            await message.answer("✅ Nothing pending — everything is filed.")
            return

        note = await message.answer(f"Retrying {len(views)} thought(s)…")
        by_id = {r.id: r for r in self.journal.records()}
        ok = failed = 0
        for view in views:
            record = by_id.get(view.id)
            if record is None:
                continue
            classification, _, _ = await classify_one(self.pipeline, self.graph, record)
            ok += classification is not None
            failed += classification is None
        await note.edit_text(
            f"✅ Filed {ok}." + (f" ⚠️ {failed} still failing." if failed else "")
        )

    async def cmd_follow(self, message: Message, command: CommandObject) -> None:
        source = _parse_follow(command.args or "")
        if source is None:
            await message.answer(
                "Usage:\n"
                "<code>/follow person Naval Ravikant</code>\n"
                "<code>/follow book Sapiens by Harari</code>\n"
                "<code>/follow channel https://youtube.com/@veritasium</code>\n"
                "<code>/follow x @karpathy</code>"
            )
            return
        await self.graph.follow(source)
        await message.answer(f"⭐ Following <b>{source.name}</b> ({source.kind})")

    async def cmd_unfollow(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Usage: <code>/unfollow Naval Ravikant</code>")
            return
        removed = await self.graph.unfollow(command.args.strip())
        await message.answer(
            f"Unfollowed <b>{command.args.strip()}</b>."
            if removed
            else f"Not following <b>{command.args.strip()}</b>."
        )

    async def cmd_following(self, message: Message) -> None:
        await _send(message.answer, render_following(await self.graph.following()))

    async def on_voice(self, message: Message) -> None:
        """Explicit, never a silent drop — a thought the user believes was captured
        and wasn't is worse than a clear refusal."""
        await message.answer(
            "🎤 Voice isn't wired up yet — this note was <b>not</b> saved.\n"
            "Send it as text for now."
        )

    # -- wiring --------------------------------------------------------- #

    def router(self) -> Router:
        r = Router()
        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_start, Command("help"))
        r.message.register(self.cmd_recent, Command("recent"))
        r.message.register(self.cmd_category, Command("category"))
        r.message.register(self.cmd_search, Command("search"))
        r.message.register(self.cmd_undo, Command("undo"))
        r.message.register(self.cmd_stats, Command("stats"))
        r.message.register(self.cmd_pending, Command("pending"))
        r.message.register(self.cmd_follow, Command("follow"))
        r.message.register(self.cmd_unfollow, Command("unfollow"))
        r.message.register(self.cmd_following, Command("following"))
        r.message.register(self.cmd_video, Command("video"))
        r.message.register(self.cmd_redistil, Command("redistil"))
        r.message.register(self.cmd_pulse, Command("pulse"))
        r.message.register(self.cmd_notes, Command("notes"))
        r.message.register(self.cmd_more, Command("more"))
        r.message.register(self.cmd_shopping, Command("shopping"))
        r.message.register(self.cmd_bought, Command("bought"))
        r.message.register(self.on_voice, F.voice | F.audio | F.video_note)
        # Registered last: anything not matched above is a thought.
        r.message.register(self.capture, F.text)
        # Taps, not messages. The owner middleware sits on `dp.update`, so these are
        # already gated by it — a callback carries `event_from_user` like any update.
        r.callback_query.register(self.on_buy_callback, F.data.startswith("buy:"))
        r.callback_query.register(self.on_unbuy_callback, F.data == "unbuy:last")
        return r


# --- helpers ------------------------------------------------------------ #

# Telegram accepts 100 buttons, but a keyboard longer than a screen stops being
# faster than typing. Past this the list still renders in full and `/bought` still
# works — only the taps stop, which is why the cap is safe to be blunt about.
MAX_ITEM_BUTTONS = 40


def _digest(canonical_name: str) -> str:
    return hashlib.sha1(
        canonical_name.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:8]


def _item_ref(item: ItemView) -> str:
    """`buy:<row id>:<name digest>`.

    The row id alone would be unsafe. `--rebuild` reassigns ids, so a button tapped
    from a message sent before one could tick off whatever now occupies that row.
    Pairing the id with a digest of the name turns that into a refusal instead of a
    wrong purchase, and both together sit far inside Telegram's 64-byte budget.
    """
    return f"buy:{item.id}:{_digest(item.canonical_name)}"


def _parse_ref(data: str) -> tuple[int | None, str]:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "buy" or not parts[1].isdigit():
        return None, ""
    return int(parts[1]), parts[2]


def _shopping_keyboard(
    items: list[ItemView], *, undo: bool = False
) -> InlineKeyboardMarkup | None:
    """One tap-to-buy button per item, plus Undo once there is something to undo."""
    rows = [
        [InlineKeyboardButton(text=f"✓ {item.name}"[:60], callback_data=_item_ref(item))]
        for item in items[:MAX_ITEM_BUTTONS]
    ]
    if undo:
        rows.append(
            [InlineKeyboardButton(text="↩ Undo last", callback_data="unbuy:last")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_list(
    answer: Callable[..., Awaitable[Any]], text: str, markup: InlineKeyboardMarkup | None
) -> Any:
    """Send a rendered list, keeping the keyboard on the final chunk.

    A keyboard belongs to exactly one message, and the buttons are the actionable
    part, so they go on the message the user is left looking at.
    """
    chunks = split_message(text)
    for chunk in chunks[:-1]:
        await answer(chunk)
    return await answer(chunks[-1], reply_markup=markup)


def resolve_item(
    term: str, items: list[ItemView]
) -> tuple[ItemView | None, list[ItemView]]:
    """Work out which item a `/bought` argument means.

    Returns (match, candidates): a match, or the candidates to disambiguate between.
    Pure, so the matching rules are testable without a Telegram transport.

    A number indexes the list exactly as rendered. Otherwise an exact name wins
    outright, and a substring is an answer only when it is unambiguous — asking is
    cheap, and ticking off the wrong thing is discovered at the till.
    """
    term = term.strip()
    if term.isdigit():
        index = int(term)
        return (items[index - 1], []) if 1 <= index <= len(items) else (None, [])

    key = canonical(term)
    for item in items:
        if item.canonical_name == key:
            return item, []

    matches = [i for i in items if key in i.canonical_name]
    return (matches[0], []) if len(matches) == 1 else (None, matches)


def _int_arg(raw: str | None, *, default: int, maximum: int) -> int:
    try:
        return max(1, min(int((raw or "").strip()), maximum))
    except (TypeError, ValueError):
        return default


def _match_category(raw: str) -> str | None:
    from vos.contracts import CATEGORIES

    squashed = raw.replace(" ", "").replace("-", "").casefold()
    for name in CATEGORIES:
        if name.casefold() == squashed:
            return name
    return None


def _parse_follow(args: str) -> SourceRef | None:
    """`/follow book Sapiens by Harari` -> SourceRef(kind='book', author='Harari')."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    kind_raw, rest = parts[0].casefold(), parts[1].strip()
    if kind_raw not in ("person", "book", "channel", "x"):
        return None
    kind: SourceKind = kind_raw  # type: ignore[assignment]

    if kind == "x":
        # Stored as @handle so it matches the author on a post and reaches xAI bare.
        handle = normalise_handle(rest)
        if handle is None:
            return None
        return SourceRef(
            name=handle, kind="x", url=f"https://x.com/{handle.lstrip('@')}"
        )

    author = None
    if kind == "book" and " by " in rest:
        rest, author = (p.strip() for p in rest.rsplit(" by ", 1))

    url = rest if rest.startswith(("http://", "https://")) else None
    return SourceRef(name=rest, kind=kind, url=url, author=author)


class AllowOnlyOwner:
    """Outer middleware dropping every update from anyone but the owner.

    A bot token is effectively public — anyone who learns the username can message the
    bot. Without this, a stranger could write into the graph and spend the model
    budget. Dropped silently: a reply would confirm the bot exists.
    """

    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id != self.allowed_user_id:
            log.warning("Dropped update from unauthorised user %s", user.id)
            return None
        return await handler(event, data)


# --- entrypoint ---------------------------------------------------------- #


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    from vos.pipeline import (
        build_pipeline,
        build_shopping_pipeline,
        build_video_pipeline,
        load_model,
    )
    from vos.projection import replay_item_marks
    from vos.settings import get_settings
    from vos.shopping import SqliteShoppingStore

    settings = get_settings()

    journal = JsonlJournal(settings.vos_journal_dir)
    cassette = Cassette(settings.vos_cassette_dir)
    budget = BudgetGuard(cassette, settings.vos_daily_budget_usd)
    fetcher = VideoFetcher(settings.vos_artifact_dir)
    graph = Neo4jGraph.connect(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password.get_secret_value()
    )
    shopping = SqliteShoppingStore(settings.vos_shopping_db)

    model = load_model(settings.vos_model)
    pipeline = build_pipeline(model, model_name=settings.vos_model, cassette=cassette)
    video_pipeline = build_video_pipeline(
        model, fetcher, model_name=settings.vos_model, cassette=cassette
    )
    shopping_pipeline = build_shopping_pipeline(
        model, model_name=settings.vos_model, cassette=cassette
    )

    await graph.ensure_schema()
    await shopping.ensure_schema()
    recovered = await reproject_missing(journal, graph)
    if recovered:
        log.info("Recovered %d thought(s) from the journal into the graph.", len(recovered))

    # Closes the crash window between a tick reaching the journal and reaching the
    # list, and restores the whole list after a `--rebuild`. Cheap and idempotent, so
    # it runs unconditionally rather than only when something looks wrong.
    if marks := await replay_item_marks(journal, shopping):
        log.info("Replayed %d shopping mark(s) from the journal.", marks)

    jobs = JobQueue(concurrency=1)  # single writer — see ADR-008
    await jobs.start()

    pulse_fetcher = None
    if settings.xai_api_key is not None:
        from vos.pulse import PulseFetcher

        pulse_fetcher = PulseFetcher(
            api_key=settings.xai_api_key.get_secret_value(),
            artifact_dir=settings.vos_artifact_dir,
            model=settings.vos_pulse_model,
            max_sources=settings.vos_pulse_max_sources,
            base_url=settings.vos_xai_base_url,
        )

    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    vos = VosBot(
        journal=journal,
        graph=graph,
        pipeline=pipeline,
        cassette=cassette,
        budget=budget,
        video_pipeline=video_pipeline,
        jobs=jobs,
        pulse_fetcher=pulse_fetcher,
        pulse_topic=settings.vos_pulse_topic,
        shopping=shopping,
        shopping_pipeline=shopping_pipeline,
    )

    async def announce(text: str, **kwargs: Any):
        return await bot.send_message(settings.vos_allowed_user_id, text, **kwargs)

    # The job queue is in-memory, so a restart loses whatever was waiting. Outstanding
    # work is recovered from the graph rather than persisted (see vos.jobs).
    if requeued := await vos.requeue_unprocessed_videos(announce):
        log.info("Re-queued %d unprocessed video(s).", requeued)
    if requeued := await vos.requeue_unextracted_shopping(announce):
        log.info("Re-queued %d unextracted shopping thought(s).", requeued)

    # The kitchen kiosk shares this process — one event loop, one JobQueue, one
    # writer (ADR-008). Lazy imports: the kiosk extra may not be installed, and an
    # unset flag must change nothing.
    web_task = web_server = None
    if settings.vos_kiosk_enabled:
        from vos.web.app import KioskDeps, build_web_app, start_server
        from vos.web.chat_agent import KitchenChat
        from vos.web.stt import FasterWhisperTranscriber

        kiosk_app = build_web_app(
            KioskDeps(
                journal=journal,
                graph=graph,
                pipeline=pipeline,
                jobs=jobs,
                transcriber=FasterWhisperTranscriber(settings.vos_whisper_model),
                budget=budget,
                cassette=cassette,
                shopping=shopping,
                shopping_pipeline=shopping_pipeline,
                chat_agent=KitchenChat(
                    model,
                    graph,
                    model_name=settings.vos_model,
                    cassette=cassette,
                    budget=budget,
                    session_ttl_s=settings.vos_kiosk_session_ttl_s,
                ),
                pin=(
                    settings.vos_kiosk_pin.get_secret_value()
                    if settings.vos_kiosk_pin
                    else None
                ),
            )
        )
        web_task, web_server = start_server(
            kiosk_app, settings.vos_kiosk_host, settings.vos_kiosk_port
        )
        log.info(
            "Kiosk serving on %s:%d (reach it via `tailscale serve`).",
            settings.vos_kiosk_host,
            settings.vos_kiosk_port,
        )

    dp = Dispatcher()
    dp.update.outer_middleware(AllowOnlyOwner(settings.vos_allowed_user_id))
    dp.include_router(vos.router())

    log.info("VOS listening. Model=%s", settings.vos_model)
    try:
        await dp.start_polling(bot)
    finally:
        # Web first: it feeds the job queue, so it must stop accepting before the
        # queue drains. Five seconds, then the rest of shutdown proceeds regardless.
        if web_server is not None and web_task is not None:
            web_server.should_exit = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(web_task, timeout=5)
        await jobs.stop()
        await graph.close()
        await shopping.close()
        await bot.session.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
