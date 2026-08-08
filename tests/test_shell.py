"""Shell tests — no Telegram, no Neo4j, no network.

The handlers are exercised directly against fakes. The assertions that matter are
about the *capture contract* in §8.1:

  - the journal write happens before anything is acknowledged;
  - if the journal write fails, the user is told the thought was NOT saved;
  - if anything after the journal write fails, the thought is still safe and the
    user is told where to find it.

A false "captured" is the one outcome the design refuses, so it is tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from vos.cassette import BudgetGuard, Cassette, CassetteEntry
from vos.contracts import (
    CaptureRecord,
    Classification,
    ExtractedEntity,
    GraphStats,
    SourceRef,
    ThoughtView,
    Tombstone,
    canonical,
)
from vos.jobs import JobQueue
from vos.journal import JsonlJournal
from vos.shell import VosBot, _match_category, _parse_follow

# --- fakes ----------------------------------------------------------------- #


class FakeReply:
    def __init__(self) -> None:
        self.text: str | None = None
        self.edits: list[str] = []

    async def edit_text(self, text: str, **_: Any) -> None:
        self.edits.append(text)
        self.text = text

    @property
    def final(self) -> str:
        return self.edits[-1] if self.edits else (self.text or "")


class FakeChat:
    def __init__(self, chat_id: int = 42) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str | None = None, message_id: int = 1) -> None:
        self.text = text
        self.message_id = message_id
        self.chat = FakeChat()
        self.replies: list[FakeReply] = []

    async def answer(self, text: str, **_: Any) -> FakeReply:
        reply = FakeReply()
        reply.text = text
        self.replies.append(reply)
        return reply

    @property
    def last(self) -> str:
        return self.replies[-1].final if self.replies else ""


class FakeCommand:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


class FakeGraph:
    """In-memory stand-in for the Neo4j projection."""

    def __init__(self, *, fail: bool = False) -> None:
        self.thoughts: dict[UUID, dict] = {}
        self.sources: dict[str, SourceRef] = {}
        self.videos: dict[str, Any] = {}
        self.notes: dict[str, list] = {}
        self.captions_auto: dict[str, bool | None] = {}
        self.video_failures: dict[UUID, tuple[str, bool]] = {}
        self.fail = fail

    async def upsert_thought(self, record, classification) -> list[str]:
        if self.fail:
            raise RuntimeError("neo4j down")
        entry = self.thoughts.setdefault(record.id, {"record": record, "status": "captured"})
        if classification is not None:
            entry["classification"] = classification
            entry["status"] = "classified"
            return [
                s.name
                for e in classification.entities
                if (s := self.sources.get(canonical(e.name)))
            ]
        return []

    async def mark_unclassified(self, id: UUID, error: str) -> None:
        if id in self.thoughts:
            self.thoughts[id]["status"] = "unclassified"
            self.thoughts[id]["error"] = error

    async def soft_delete(self, id: UUID) -> None:
        if id in self.thoughts:
            self.thoughts[id]["status"] = "deleted"

    async def following(self) -> list[SourceRef]:
        return list(self.sources.values())

    async def follow(self, source: SourceRef) -> None:
        self.sources[source.canonical_name] = source

    async def unfollow(self, name: str) -> bool:
        return self.sources.pop(canonical(name), None) is not None

    async def recent(self, n: int = 10) -> list[ThoughtView]:
        return [self._view(e) for e in self.thoughts.values() if e["status"] != "deleted"][:n]

    async def by_category(self, category: str, n: int = 10) -> list[ThoughtView]:
        return [
            self._view(e)
            for e in self.thoughts.values()
            if e.get("classification") and e["classification"].category == category
        ][:n]

    async def search(self, term: str, n: int = 10) -> list[ThoughtView]:
        return [
            self._view(e) for e in self.thoughts.values() if term in e["record"].text
        ][:n]

    async def pending(self) -> list[ThoughtView]:
        return [
            self._view(e)
            for e in self.thoughts.values()
            if e["status"] in ("unclassified", "captured")
        ]

    async def stats(self) -> GraphStats:
        live = [e for e in self.thoughts.values() if e["status"] != "deleted"]
        return GraphStats(
            total=len(live),
            pending=len([e for e in live if e["status"] != "classified"]),
        )

    async def existing_ids(self) -> set[UUID]:
        return set(self.thoughts)

    # -- video --

    async def upsert_video(self, meta, thought_id=None, *, is_generated=None) -> None:
        self.videos[meta.video_id] = meta
        self.captions_auto[meta.video_id] = is_generated
        self.video_failures.pop(thought_id, None)

    async def mark_video_failed(self, thought_id, reason: str, permanent: bool) -> None:
        self.video_failures[thought_id] = (reason, permanent)

    async def replace_notes(self, video_id: str, notes) -> int:
        self.notes[video_id] = list(notes)
        return len(notes)

    async def search_notes(self, term: str, n: int = 10):
        return []

    async def notes_for_video(self, video_id: str):
        from uuid import uuid4

        from vos.contracts import NoteView

        return [
            NoteView(
                id=uuid4(),
                text=n.text,
                t_seconds=n.t_seconds,
                section=n.section,
                score=n.score,
                video_id=video_id,
                video_title="A Talk",
                url=f"https://youtu.be/{video_id}",
            )
            for n in self.notes.get(video_id, [])
        ]

    async def all_video_ids(self):
        return list(self.videos)

    async def unprocessed_video_thoughts(self):
        return []

    @staticmethod
    def _view(entry: dict) -> ThoughtView:
        record = entry["record"]
        c = entry.get("classification")
        return ThoughtView(
            id=record.id,
            text=record.text,
            title=c.title if c else None,
            category=c.category if c else None,
            status=entry["status"],
            created_at=record.captured_at,
        )


class FakePipeline:
    def __init__(self, classification: Classification | None = None, error: str | None = None):
        self.classification = classification
        self.error = error

    async def ainvoke(self, state) -> dict:
        return {"classification": self.classification, "error": self.error}


def _classification() -> Classification:
    return Classification(
        category="TripPlanning",
        title="Tokyo flights",
        summary="Book flights to Tokyo.",
        entities=[ExtractedEntity(name="Tokyo", type="place", salience=0.9)],
        confidence=0.9,
    )


@pytest.fixture
def bot(tmp_path: Path) -> VosBot:
    return VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=FakePipeline(_classification()),
    )


# --- the capture contract --------------------------------------------------- #


async def test_capture_writes_to_journal_before_replying(bot: VosBot):
    message = FakeMessage("book flights to Tokyo")
    await bot.capture(message)  # type: ignore[arg-type]

    records = bot.journal.records()
    assert [r.text for r in records] == ["book flights to Tokyo"]
    assert "Trip planning" in message.last


async def test_capture_is_idempotent_for_a_redelivered_message(bot: VosBot):
    for _ in range(2):
        await bot.capture(FakeMessage("same thought", message_id=7))  # type: ignore[arg-type]
    assert len(bot.journal.records()) == 1  # same derived ID collapses


async def test_journal_failure_tells_the_user_it_was_not_saved(bot: VosBot, monkeypatch):
    """The one outcome the design refuses is a false 'captured'."""

    async def boom(_entry):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bot.journal, "append", boom)
    message = FakeMessage("this must not be acknowledged")
    await bot.capture(message)  # type: ignore[arg-type]

    assert "Not saved" in message.last
    assert "Captured" not in message.last


async def test_graph_failure_does_not_lose_the_thought(tmp_path: Path):
    """Neo4j down: the journal still has it, and the user is told it's pending."""
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(fail=True),  # type: ignore[arg-type]
        pipeline=FakePipeline(_classification()),
    )
    message = FakeMessage("survives a database outage")
    await bot.capture(message)  # type: ignore[arg-type]

    assert [r.text for r in bot.journal.records()] == ["survives a database outage"]
    assert "Captured" in message.last
    assert "/pending" in message.last


async def test_classification_failure_reports_pending(tmp_path: Path):
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=FakePipeline(None, error="provider down"),
    )
    message = FakeMessage("still safe")
    await bot.capture(message)  # type: ignore[arg-type]

    assert len(bot.journal.records()) == 1
    assert "couldn't classify" in message.last
    assert "provider down" in message.last


async def test_budget_exceeded_defers_but_still_captures(tmp_path: Path):
    cassette = Cassette(tmp_path / "c")
    cassette.record(
        CassetteEntry(
            thought_id=CaptureRecord.create(
                chat_id=1, message_id=1, text="x", captured_at=datetime.now(UTC)
            ).id,
            model="m",
            prompt="p",
            cost_usd=99.0,
        )
    )
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=FakePipeline(_classification()),
        budget=BudgetGuard(cassette, 1.00),
    )
    message = FakeMessage("captured despite budget")
    await bot.capture(message)  # type: ignore[arg-type]

    assert len(bot.journal.records()) == 1  # never lost
    assert "budget" in message.last.lower()


async def test_empty_message_is_ignored(bot: VosBot):
    await bot.capture(FakeMessage("   "))  # type: ignore[arg-type]
    assert bot.journal.records() == []


async def test_voice_is_refused_explicitly_not_silently(bot: VosBot):
    message = FakeMessage()
    await bot.on_voice(message)  # type: ignore[arg-type]
    assert "not</b> saved" in message.last or "not saved" in message.last.lower()
    assert bot.journal.records() == []


# --- linked follows --------------------------------------------------------- #


async def test_capture_reports_linked_followed_sources(bot: VosBot):
    await bot.graph.follow(SourceRef(name="Tokyo", kind="channel"))  # type: ignore[attr-defined]
    message = FakeMessage("trip to Tokyo")
    await bot.capture(message)  # type: ignore[arg-type]
    assert "Follows" in message.last and "Tokyo" in message.last


# --- commands ---------------------------------------------------------------- #


async def test_undo_appends_tombstone_and_keeps_original_line(bot: VosBot, tmp_path: Path):
    await bot.capture(FakeMessage("regrettable thought"))  # type: ignore[arg-type]
    message = FakeMessage()
    await bot.cmd_undo(message)  # type: ignore[arg-type]

    assert bot.journal.records() == []  # gone from the replay view
    raw = next((tmp_path / "journal").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "regrettable thought" in raw  # but never erased
    assert any(isinstance(e, Tombstone) for e in bot.journal.read_all())


async def test_undo_with_nothing_captured(bot: VosBot):
    message = FakeMessage()
    await bot.cmd_undo(message)  # type: ignore[arg-type]
    assert "Nothing to undo" in message.last


async def test_recent_and_search(bot: VosBot):
    await bot.capture(FakeMessage("oat milk", message_id=1))  # type: ignore[arg-type]
    await bot.capture(FakeMessage("quarterly earnings", message_id=2))  # type: ignore[arg-type]

    m = FakeMessage()
    await bot.cmd_recent(m, FakeCommand())  # type: ignore[arg-type]
    assert "oat milk" in m.last

    m = FakeMessage()
    await bot.cmd_search(m, FakeCommand("earnings"))  # type: ignore[arg-type]
    assert "quarterly earnings" in m.last


async def test_search_without_argument_shows_usage(bot: VosBot):
    m = FakeMessage()
    await bot.cmd_search(m, FakeCommand(None))  # type: ignore[arg-type]
    assert "Usage" in m.last


async def test_category_rejects_unknown_name(bot: VosBot):
    m = FakeMessage()
    await bot.cmd_category(m, FakeCommand("Nonsense"))  # type: ignore[arg-type]
    assert "Unknown category" in m.last


async def test_follow_and_following_round_trip(bot: VosBot):
    m = FakeMessage()
    await bot.cmd_follow(m, FakeCommand("person Naval Ravikant"))  # type: ignore[arg-type]
    assert "Naval Ravikant" in m.last

    m = FakeMessage()
    await bot.cmd_following(m)  # type: ignore[arg-type]
    assert "Naval Ravikant" in m.last

    m = FakeMessage()
    await bot.cmd_unfollow(m, FakeCommand("naval ravikant"))  # type: ignore[arg-type]
    assert "Unfollowed" in m.last


async def test_follow_bad_syntax_shows_usage(bot: VosBot):
    m = FakeMessage()
    await bot.cmd_follow(m, FakeCommand("Naval"))  # type: ignore[arg-type]
    assert "Usage" in m.last


async def test_pending_retries_and_files(tmp_path: Path):
    failing = FakePipeline(None, error="down")
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=failing,
    )
    await bot.capture(FakeMessage("retry me"))  # type: ignore[arg-type]

    bot.pipeline = FakePipeline(_classification())  # provider recovers
    m = FakeMessage()
    await bot.cmd_pending(m)  # type: ignore[arg-type]
    assert "Filed 1" in m.last


async def test_pending_when_empty(bot: VosBot):
    m = FakeMessage()
    await bot.cmd_pending(m)  # type: ignore[arg-type]
    assert "Nothing pending" in m.last


# --- video ------------------------------------------------------------------- #


class StubVideoPipeline:
    def __init__(self, result: dict | None = None):
        self.result = result or {}
        self.calls: list[str] = []

    async def ainvoke(self, state) -> dict:
        self.calls.append(state.video_id)
        return self.result


def _video_bot(tmp_path: Path, pipeline_result: dict | None = None) -> tuple[VosBot, JobQueue]:
    from vos.contracts import VideoDistillation, VideoMeta, VideoNote

    result = pipeline_result or {
        "artifact": type(
            "A",
            (),
            {
                "meta": VideoMeta(
                    video_id="dQw4w9WgXcQ",
                    url="https://youtu.be/dQw4w9WgXcQ",
                    title="Speed of Light",
                    channel="Veritasium",
                ),
                "is_generated": False,
            },
        )(),
        "distillation": VideoDistillation(
            summary="About light.",
            notes=[VideoNote(text="cannot measure one way", t_seconds=134)],
        ),
        "error": None,
        "truncated": False,
    }
    jobs = JobQueue()
    bot = VosBot(
        journal=JsonlJournal(tmp_path / "journal"),
        graph=FakeGraph(),  # type: ignore[arg-type]
        pipeline=FakePipeline(
            Classification(
                category="VideoKnowledge", title="A video", summary="s", confidence=0.9
            )
        ),
        video_pipeline=StubVideoPipeline(result),
        jobs=jobs,
    )
    return bot, jobs


async def test_youtube_link_is_queued_not_processed_inline(tmp_path: Path):
    """Capture must return immediately; distillation runs on the worker."""
    bot, jobs = _video_bot(tmp_path)
    await jobs.start()

    message = FakeMessage("worth watching https://youtu.be/dQw4w9WgXcQ")
    await bot.capture(message)  # type: ignore[arg-type]

    # The capture reply is already delivered before the job has run.
    assert "Video knowledge" in message.replies[0].final
    await jobs.drain()
    await jobs.stop()

    assert "Speed of Light" in message.replies[-1].final
    assert "134" in message.replies[-1].final  # deep link timestamp


async def test_non_video_thought_queues_nothing(tmp_path: Path):
    bot, jobs = _video_bot(tmp_path)
    bot.pipeline = FakePipeline(_classification())  # TripPlanning
    await jobs.start()
    await bot.capture(FakeMessage("book flights to Tokyo"))  # type: ignore[arg-type]
    await jobs.drain()
    await jobs.stop()
    assert bot.video_pipeline.calls == []  # type: ignore[union-attr]


async def test_videoknowledge_without_a_link_queues_nothing(tmp_path: Path):
    """Classified as video but no URL — nothing to fetch."""
    bot, jobs = _video_bot(tmp_path)
    await jobs.start()
    await bot.capture(FakeMessage("that documentary I watched was great"))  # type: ignore[arg-type]
    await jobs.drain()
    await jobs.stop()
    assert bot.video_pipeline.calls == []  # type: ignore[union-attr]


async def test_video_failure_says_the_thought_is_still_saved(tmp_path: Path):
    bot, jobs = _video_bot(
        tmp_path,
        {"artifact": None, "distillation": None, "error": "captions are disabled"},
    )
    await jobs.start()
    message = FakeMessage("https://youtu.be/dQw4w9WgXcQ")
    await bot.capture(message)  # type: ignore[arg-type]
    await jobs.drain()
    await jobs.stop()

    assert len(bot.journal.records()) == 1  # the thought survived
    assert "captions are disabled" in message.replies[-1].final
    assert "saved" in message.replies[-1].final


async def test_cmd_video_rejects_non_youtube(tmp_path: Path):
    bot, _ = _video_bot(tmp_path)
    m = FakeMessage()
    await bot.cmd_video(m, FakeCommand("https://vimeo.com/12345"))  # type: ignore[arg-type]
    assert "Only YouTube" in m.last


async def test_cmd_notes_requires_a_term(tmp_path: Path):
    bot, _ = _video_bot(tmp_path)
    m = FakeMessage()
    await bot.cmd_notes(m, FakeCommand(None))  # type: ignore[arg-type]
    assert "Usage" in m.last


# --- parsing ----------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "kind", "name", "author"),
    [
        ("person Naval Ravikant", "person", "Naval Ravikant", None),
        ("book Sapiens by Harari", "book", "Sapiens", "Harari"),
        (
            "book Thinking, Fast and Slow by Kahneman",
            "book",
            "Thinking, Fast and Slow",
            "Kahneman",
        ),
        (
            "channel https://youtube.com/@veritasium",
            "channel",
            "https://youtube.com/@veritasium",
            None,
        ),
    ],
)
def test_parse_follow(raw, kind, name, author):
    source = _parse_follow(raw)
    assert source is not None
    assert (source.kind, source.name, source.author) == (kind, name, author)


# --- /more ------------------------------------------------------------------ #


async def test_more_shows_every_stored_claim(tmp_path: Path):
    """The reply shows the best few; nothing is lost, so /more is a read not a re-run."""
    bot, jobs = _video_bot(tmp_path)
    await jobs.start()
    await bot.capture(FakeMessage("https://youtu.be/dQw4w9WgXcQ"))  # type: ignore[arg-type]
    await jobs.drain()
    await jobs.stop()

    message = FakeMessage()
    await bot.cmd_more(message, FakeCommand())  # type: ignore[arg-type]
    assert "cannot measure one way" in message.last


async def test_more_defaults_to_the_most_recent_video(tmp_path: Path):
    bot, jobs = _video_bot(tmp_path)
    await jobs.start()
    await bot.capture(FakeMessage("https://youtu.be/dQw4w9WgXcQ"))  # type: ignore[arg-type]
    await jobs.drain()
    await jobs.stop()

    bare, explicit = FakeMessage(), FakeMessage()
    await bot.cmd_more(bare, FakeCommand())  # type: ignore[arg-type]
    await bot.cmd_more(explicit, FakeCommand("https://youtu.be/dQw4w9WgXcQ"))  # type: ignore[arg-type]
    assert bare.last == explicit.last


async def test_more_with_nothing_processed_says_so(tmp_path: Path):
    bot, jobs = _video_bot(tmp_path)
    message = FakeMessage()
    await bot.cmd_more(message, FakeCommand())  # type: ignore[arg-type]
    assert "No videos processed yet" in message.last


async def test_more_rejects_something_that_is_not_a_video(tmp_path: Path):
    bot, jobs = _video_bot(tmp_path)
    message = FakeMessage()
    await bot.cmd_more(message, FakeCommand("what did it say about pricing"))  # type: ignore[arg-type]
    assert "Usage" in message.last


@pytest.mark.parametrize("raw", ["", "person", "wizard Gandalf", "   "])
def test_parse_follow_rejects_bad_input(raw):
    assert _parse_follow(raw) is None


def test_parse_follow_sets_url_for_channels():
    assert _parse_follow("channel https://x.com/y").url == "https://x.com/y"  # type: ignore[union-attr]
    assert _parse_follow("channel Veritasium").url is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TripPlanning", "TripPlanning"),
        ("trip planning", "TripPlanning"),
        ("trip-planning", "TripPlanning"),
        ("SHOPPING", "Shopping"),
        ("nonsense", None),
    ],
)
def test_match_category(raw, expected):
    assert _match_category(raw) == expected
