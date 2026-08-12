"""Web app tests — no Telegram, no Neo4j, no microphone, no model.

The capture endpoint re-states the §8.1 contract for a second transport, so the
assertions mirror test_shell.py: journal write before anything else, a failed journal
write reported as NOT saved, and everything after the fsync allowed to fail without
losing the thought. On top of that, kiosk-specific properties: all graph writes go
through the JobQueue (the FastAPI handler is genuinely concurrent with aiogram, so
writing inline would break ADR-008), and a slow classification degrades to "pending"
rather than holding the tablet's request open.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_shell import FakeGraph, FakePipeline, _classification

from vos.contracts import kitchen_thought_id
from vos.jobs import JobQueue
from vos.journal import JsonlJournal

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from vos.web.app import KioskDeps, build_web_app, start_server  # noqa: E402


class FakeTranscriber:
    def __init__(self, text: str = "buy milk") -> None:
        self.text = text
        self.calls: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, mime: str) -> str:
        self.calls.append((audio, mime))
        return self.text


class FakeBudget:
    def __init__(self, exceeded: bool = False) -> None:
        self._exceeded = exceeded

    def exceeded(self) -> bool:
        return self._exceeded

    def spent_today(self) -> float:
        return 2.0 if self._exceeded else 0.0


def _deps(pin: str | None = None, **overrides) -> KioskDeps:
    defaults = dict(
        journal=None,
        graph=None,
        pipeline=None,
        jobs=None,
        transcriber=None,
        pin=pin,
    )
    defaults.update(overrides)
    return KioskDeps(**defaults)


async def _started_jobs() -> JobQueue:
    jobs = JobQueue(concurrency=1)
    await jobs.start()
    return jobs


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kiosk"
    )


# --- health and PIN discovery -------------------------------------------- #


async def test_health_is_open_and_reports_no_pin():
    async with _client(build_web_app(_deps())) as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "pin_required": False}


async def test_health_is_open_even_with_a_pin_set():
    """The frontend has to be able to ask 'do I need to prompt?' before it has a PIN."""
    async with _client(build_web_app(_deps(pin="4321"))) as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["pin_required"] is True


# --- the PIN gate --------------------------------------------------------- #


async def test_api_is_open_when_no_pin_configured():
    async with _client(build_web_app(_deps())) as client:
        resp = await client.get("/api/ping")
    assert resp.status_code == 200


async def test_api_rejects_missing_pin():
    async with _client(build_web_app(_deps(pin="4321"))) as client:
        resp = await client.get("/api/ping")
    assert resp.status_code == 401


async def test_api_rejects_wrong_pin():
    async with _client(build_web_app(_deps(pin="4321"))) as client:
        resp = await client.get("/api/ping", headers={"X-VOS-PIN": "1111"})
    assert resp.status_code == 401


async def test_api_accepts_correct_pin():
    async with _client(build_web_app(_deps(pin="4321"))) as client:
        resp = await client.get("/api/ping", headers={"X-VOS-PIN": "4321"})
    assert resp.status_code == 200


async def test_static_index_is_served_at_root():
    async with _client(build_web_app(_deps())) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# --- /api/transcribe ------------------------------------------------------ #


async def test_transcribe_returns_the_transcript():
    stt = FakeTranscriber("add oat milk to the list")
    app = build_web_app(_deps(transcriber=stt))
    async with _client(app) as client:
        resp = await client.post(
            "/api/transcribe",
            files={"audio": ("clip.webm", b"opus-bytes", "audio/webm")},
        )
    assert resp.status_code == 200
    assert resp.json() == {"transcript": "add oat milk to the list"}
    assert stt.calls == [(b"opus-bytes", "audio/webm")]


async def test_transcribe_rejects_empty_audio():
    app = build_web_app(_deps(transcriber=FakeTranscriber()))
    async with _client(app) as client:
        resp = await client.post(
            "/api/transcribe", files={"audio": ("clip.webm", b"", "audio/webm")}
        )
    assert resp.status_code == 400


async def test_transcribe_rejects_oversized_audio():
    """A tap-to-talk clip is a few hundred KB; anything huge is a bug or abuse."""
    app = build_web_app(_deps(transcriber=FakeTranscriber()))
    async with _client(app) as client:
        resp = await client.post(
            "/api/transcribe",
            files={"audio": ("clip.webm", b"x" * (11 * 1024 * 1024), "audio/webm")},
        )
    assert resp.status_code == 413


# --- /api/capture: the §8.1 contract over HTTP ----------------------------- #


def _capture_body(text: str = "buy milk", client_id: str = "c-1", **extra) -> dict:
    return {"text": text, "client_id": client_id, **extra}


async def test_capture_classifies_and_reports(tmp_path: Path):
    journal = JsonlJournal(tmp_path / "journal")
    graph = FakeGraph()
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=journal,
                graph=graph,
                pipeline=FakePipeline(_classification()),
                jobs=jobs,
            )
        )
        async with _client(app) as client:
            resp = await client.post(
                "/api/capture",
                json=_capture_body(source="voice", transcript="by milk"),
            )
    finally:
        await jobs.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert body["status"] == "classified"
    assert body["category"] == "TripPlanning"
    assert body["title"] == "Tokyo flights"

    (record,) = journal.records()
    assert record.id == kitchen_thought_id("c-1")
    assert record.channel == "kitchen"
    assert record.source == "voice"
    assert record.transcript == "by milk"
    assert graph.thoughts[record.id]["status"] == "classified"


async def test_capture_journal_failure_reports_not_saved(tmp_path: Path):
    """A false 'captured' is the one outcome the design refuses — over any transport."""

    class BrokenJournal:
        async def append(self, entry) -> None:
            raise OSError("disk full")

    graph = FakeGraph()
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(journal=BrokenJournal(), graph=graph, pipeline=FakePipeline(), jobs=jobs)
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body())
        assert resp.status_code == 503
        assert resp.json()["saved"] is False
        # Nothing may reach the graph for a thought that was never durable.
        await jobs.drain()
        assert graph.thoughts == {}
    finally:
        await jobs.stop()


async def test_capture_graph_writes_go_through_the_job_queue(tmp_path: Path):
    """ADR-008: the handler runs concurrently with aiogram handlers, so it must not
    touch the graph itself. With no worker running, the graph must stay empty; the
    moment the worker starts, the queued job projects the thought."""
    journal = JsonlJournal(tmp_path / "journal")
    graph = FakeGraph()
    jobs = JobQueue(concurrency=1)  # deliberately NOT started yet
    app = build_web_app(
        _deps(
            journal=journal,
            graph=graph,
            pipeline=FakePipeline(_classification()),
            jobs=jobs,
            classify_timeout_s=0.05,
        )
    )
    async with _client(app) as client:
        resp = await client.post("/api/capture", json=_capture_body())

    assert resp.json()["status"] == "pending"  # timed out waiting, but saved
    assert graph.thoughts == {}, "handler wrote the graph directly"
    assert list(journal.records()), "journal write must not depend on the queue"

    await jobs.start()
    try:
        await jobs.drain()
    finally:
        await jobs.stop()
    assert graph.thoughts, "queued job never projected the thought"


async def test_capture_slow_classification_degrades_to_pending(tmp_path: Path):
    class SlowPipeline:
        async def ainvoke(self, state) -> dict:
            await asyncio.sleep(0.5)
            return {"classification": _classification(), "error": None}

    journal = JsonlJournal(tmp_path / "journal")
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=journal,
                graph=FakeGraph(),
                pipeline=SlowPipeline(),
                jobs=jobs,
                classify_timeout_s=0.05,
            )
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body())
        body = resp.json()
        assert body["saved"] is True
        assert body["status"] == "pending"
        await jobs.drain()  # the job still completes after the response went out
    finally:
        await jobs.stop()


async def test_capture_budget_exceeded_defers_classification(tmp_path: Path):
    """Capture always works; only enrichment costs money and only it is deferred."""
    journal = JsonlJournal(tmp_path / "journal")
    graph = FakeGraph()
    pipeline = FakePipeline(_classification())
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=journal,
                graph=graph,
                pipeline=pipeline,
                jobs=jobs,
                budget=FakeBudget(exceeded=True),
            )
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body())
    finally:
        await jobs.stop()

    body = resp.json()
    assert body["saved"] is True
    assert body["status"] == "unclassified"
    assert "budget" in body["error"]
    record_id = kitchen_thought_id("c-1")
    assert graph.thoughts[record_id]["status"] == "unclassified"


async def test_capture_retry_with_same_client_id_dedupes(tmp_path: Path):
    journal = JsonlJournal(tmp_path / "journal")
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=journal,
                graph=FakeGraph(),
                pipeline=FakePipeline(_classification()),
                jobs=jobs,
            )
        )
        async with _client(app) as client:
            await client.post("/api/capture", json=_capture_body(client_id="c-9"))
            await client.post("/api/capture", json=_capture_body(client_id="c-9"))
    finally:
        await jobs.stop()

    assert len(journal.records()) == 1


async def test_capture_rejects_blank_text(tmp_path: Path):
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(),
                jobs=jobs,
            )
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body(text="   "))
    finally:
        await jobs.stop()
    assert resp.status_code == 400


# --- /api/capture: shopping extraction -------------------------------------- #


class FakeShoppingStore:
    def __init__(self) -> None:
        self.added: list[tuple] = []
        self.extractions: list[tuple] = []

    async def add(self, thought_id, items, captured_at) -> None:
        self.added.append((thought_id, items))

    async def record_extraction(self, thought_id, error=None) -> None:
        self.extractions.append((thought_id, error))

    async def pending_items(self):
        from datetime import UTC, datetime

        from vos.contracts import ItemView, canonical

        return [
            ItemView(
                id=i,
                name=item.name,
                canonical_name=canonical(item.name),
                added_at=datetime.now(UTC),
            )
            for i, (_tid, items) in enumerate(self.added, start=1)
            for item in items
        ]


class FakeShoppingPipeline:
    def __init__(self, names: list[str] | None = None, error: str | None = None) -> None:
        self.names = names or []
        self.error = error
        self.calls = 0

    async def ainvoke(self, state) -> dict:
        from vos.contracts import ShoppingExtraction, ShoppingItem

        self.calls += 1
        if self.error:
            return {"extraction": None, "error": self.error}
        return {
            "extraction": ShoppingExtraction(
                items=[ShoppingItem(name=n) for n in self.names]
            ),
            "error": None,
        }


def _shopping_classification():
    from vos.contracts import Classification

    return Classification(
        category="Shopping", title="Groceries", summary="Buy oat milk and eggs."
    )


async def test_kitchen_shopping_capture_lands_on_the_list(tmp_path: Path):
    """The kiosk is an add-only door to the shopping list: a Shopping-classified
    capture extracts items exactly as the Telegram path does."""
    store = FakeShoppingStore()
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(_shopping_classification()),
                jobs=jobs,
                shopping=store,
                shopping_pipeline=FakeShoppingPipeline(["oat milk", "eggs"]),
            )
        )
        async with _client(app) as client:
            resp = await client.post(
                "/api/capture", json=_capture_body(text="oat milk and eggs")
            )
    finally:
        await jobs.stop()

    body = resp.json()
    assert body["status"] == "classified"
    assert body["items"] == ["oat milk", "eggs"]
    (added,) = store.added
    assert added[0] == kitchen_thought_id("c-1")
    assert [i.name for i in added[1]] == ["oat milk", "eggs"]


async def test_non_shopping_capture_never_touches_the_list(tmp_path: Path):
    store = FakeShoppingStore()
    pipeline = FakeShoppingPipeline(["should not appear"])
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(_classification()),  # TripPlanning
                jobs=jobs,
                shopping=store,
                shopping_pipeline=pipeline,
            )
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body())
    finally:
        await jobs.stop()

    assert resp.json()["status"] == "classified"
    assert "items" not in resp.json()
    assert store.added == []
    assert pipeline.calls == 0


async def test_failed_extraction_does_not_fail_the_capture(tmp_path: Path):
    store = FakeShoppingStore()
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(_shopping_classification()),
                jobs=jobs,
                shopping=store,
                shopping_pipeline=FakeShoppingPipeline(error="model exploded"),
            )
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body())
    finally:
        await jobs.stop()

    body = resp.json()
    assert body["status"] == "classified"  # the thought is filed regardless
    assert "items" not in body
    assert store.added == []
    # The failure is recorded so the startup recovery loop retries it.
    assert store.extractions[-1][1] == "model exploded"


async def test_shopping_extraction_skipped_when_kiosk_lacks_the_store(tmp_path: Path):
    """Kiosk without shopping wired (extra not configured) must still capture."""
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(_shopping_classification()),
                jobs=jobs,
            )
        )
        async with _client(app) as client:
            resp = await client.post("/api/capture", json=_capture_body())
    finally:
        await jobs.stop()
    assert resp.json()["status"] == "classified"


# --- /api/shopping: the card tab -------------------------------------------- #


async def test_card_tap_adds_without_any_model_call(tmp_path: Path):
    """A tapped card is a known item: journal capture + preset classification +
    direct store add. Neither the classifier nor the extractor may be invoked."""
    store = FakeShoppingStore()
    classifier = FakePipeline(_classification())
    extractor = FakeShoppingPipeline(["should not run"])
    journal = JsonlJournal(tmp_path / "journal")
    graph = FakeGraph()
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=journal,
                graph=graph,
                pipeline=classifier,
                jobs=jobs,
                shopping=store,
                shopping_pipeline=extractor,
            )
        )
        async with _client(app) as client:
            resp = await client.post(
                "/api/shopping/add", json={"name": "oat milk", "client_id": "card-1"}
            )
    finally:
        await jobs.stop()

    body = resp.json()
    assert body["saved"] is True
    assert "oat milk" in body["pending"]
    assert extractor.calls == 0

    (record,) = journal.records()
    assert record.text == "oat milk"
    assert record.channel == "kitchen"
    entry = graph.thoughts[record.id]
    assert entry["classification"].category == "Shopping"
    (added,) = store.added
    assert [i.name for i in added[1]] == ["oat milk"]
    assert store.extractions[-1] == (record.id, None)


async def test_card_tap_retry_dedupes_in_the_journal(tmp_path: Path):
    journal = JsonlJournal(tmp_path / "journal")
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=journal,
                graph=FakeGraph(),
                pipeline=FakePipeline(),
                jobs=jobs,
                shopping=FakeShoppingStore(),
            )
        )
        async with _client(app) as client:
            await client.post(
                "/api/shopping/add", json={"name": "rice", "client_id": "card-2"}
            )
            await client.post(
                "/api/shopping/add", json={"name": "rice", "client_id": "card-2"}
            )
    finally:
        await jobs.stop()
    assert len(journal.records()) == 1


async def test_shopping_pending_lists_canonical_names(tmp_path: Path):
    store = FakeShoppingStore()
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(),
                jobs=jobs,
                shopping=store,
            )
        )
        async with _client(app) as client:
            await client.post(
                "/api/shopping/add", json={"name": "  Kitchen  Tissue ", "client_id": "c1"}
            )
            resp = await client.get("/api/shopping")
    finally:
        await jobs.stop()
    assert resp.json() == {"pending": ["kitchen tissue"]}


async def test_shopping_endpoints_503_without_the_store(tmp_path: Path):
    jobs = await _started_jobs()
    try:
        app = build_web_app(
            _deps(
                journal=JsonlJournal(tmp_path / "journal"),
                graph=FakeGraph(),
                pipeline=FakePipeline(),
                jobs=jobs,
            )
        )
        async with _client(app) as client:
            get_resp = await client.get("/api/shopping")
            add_resp = await client.post(
                "/api/shopping/add", json={"name": "rice", "client_id": "c1"}
            )
    finally:
        await jobs.stop()
    assert get_resp.status_code == 503
    assert add_resp.status_code == 503


# --- /api/chat -------------------------------------------------------------- #


class FakeChatAgent:
    def __init__(self, reply: str = "hello from VOS") -> None:
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    async def reply(self, session_id: str, text: str) -> str:
        self.calls.append((session_id, text))
        return self._reply


async def test_chat_round_trip():
    agent = FakeChatAgent("You noted oat milk on Tuesday.")
    app = build_web_app(_deps(chat_agent=agent))
    async with _client(app) as client:
        resp = await client.post(
            "/api/chat", json={"session_id": "s1", "message": "any milk notes?"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"reply": "You noted oat milk on Tuesday."}
    assert agent.calls == [("s1", "any milk notes?")]


async def test_chat_without_an_agent_is_a_clear_503():
    app = build_web_app(_deps(chat_agent=None))
    async with _client(app) as client:
        resp = await client.post(
            "/api/chat", json={"session_id": "s1", "message": "hi"}
        )
    assert resp.status_code == 503


async def test_chat_rejects_blank_message():
    app = build_web_app(_deps(chat_agent=FakeChatAgent()))
    async with _client(app) as client:
        resp = await client.post("/api/chat", json={"session_id": "s1", "message": "  "})
    assert resp.status_code == 400


# --- co-hosted server lifecycle ------------------------------------------- #


async def test_server_starts_and_drains_within_five_seconds():
    """The shell's shutdown budget. If this exceeds 5s, Ctrl+C on the bot would
    abandon the web task mid-flight instead of draining it."""
    app = build_web_app(_deps())
    task, server = start_server(app, "127.0.0.1", 0)  # port 0: kernel picks a free one

    for _ in range(200):  # wait for startup, max ~2s
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn never reported startup"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


async def test_server_leaves_the_sigint_handler_alone():
    """The load-bearing property. Un-overridden, uvicorn swaps in a SIGINT handler
    that sets its own `should_exit` and never raises KeyboardInterrupt — Ctrl+C
    would stop reaching aiogram and the bot would stop being stoppable."""
    import signal

    before = signal.getsignal(signal.SIGINT)
    app = build_web_app(_deps())
    task, server = start_server(app, "127.0.0.1", 0)
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


# --- doctor -------------------------------------------------------------- #


class FakeSlotFetcher:
    """Stands in for SlotFetcher: the endpoint only needs `.fetch()`."""

    def __init__(self, snapshot=None, error: Exception | None = None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


async def test_doctor_is_503_when_doctolib_is_off():
    """An unset VOS_DOCTOLIB_URL must leave the rest of the kiosk untouched."""
    app = build_web_app(_deps())
    async with _client(app) as c:
        resp = await c.get("/api/doctor")
    assert resp.status_code == 503


async def test_doctor_returns_slots_as_iso_strings():
    from vos.contracts import AppointmentSlot, DoctolibSnapshot

    snapshot = DoctolibSnapshot(
        source_url="https://www.doctolib.de/availabilities.json?x=1",
        fetched_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
        slots=[AppointmentSlot(starts_at=datetime(2026, 8, 19, 10, 20, tzinfo=UTC))],
        total=1,
    )
    app = build_web_app(_deps(slot_fetcher=FakeSlotFetcher(snapshot)))
    async with _client(app) as c:
        resp = await c.get("/api/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["slots"] == ["2026-08-19T10:20:00+00:00"]


async def test_a_doctolib_failure_is_a_200_the_tab_can_render():
    """Rate limits and bot checks are expected answers, not server errors. A 5xx
    here would make the tab show a network failure instead of the reason."""
    from vos.contracts import DoctolibError

    app = build_web_app(
        _deps(slot_fetcher=FakeSlotFetcher(error=DoctolibError("Try again later.")))
    )
    async with _client(app) as c:
        resp = await c.get("/api/doctor")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "detail": "Try again later.", "slots": []}


async def test_doctor_writes_nothing():
    """Looking at a calendar is not a decision anybody authored, so unlike the
    shopping tab there must be no journal entry and no queued job."""
    from vos.contracts import DoctolibSnapshot

    snapshot = DoctolibSnapshot(
        source_url="https://www.doctolib.de/availabilities.json?x=1",
        fetched_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )
    fetcher = FakeSlotFetcher(snapshot)
    app = build_web_app(_deps(slot_fetcher=fetcher, journal=None, jobs=None))
    async with _client(app) as c:
        resp = await c.get("/api/doctor")
    assert resp.status_code == 200
    assert fetcher.calls == 1
