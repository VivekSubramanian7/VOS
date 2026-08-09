"""The kiosk web app — FastAPI factory plus the uvicorn co-hosting helper.

The app never constructs its dependencies; `KioskDeps` mirrors how `VosBot` receives
everything injected, so the whole surface can be driven in tests with fakes and no
model, graph or microphone anywhere near it.

Exposure model: this binds to 127.0.0.1 and is published to the family through
`tailscale serve` only. There is no TLS, CORS or rate limiting here on purpose —
the tailnet is the perimeter, and adding half-hearted versions of those would
suggest this is safe to bind to a real interface. It is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vos.contracts import CaptureRecord, InputSource
from vos.projection import classify_one

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# A tap-to-talk clip is a few hundred KB of opus; ten megabytes is minutes of audio
# and means a stuck button or a hostile client, not a family member.
_MAX_AUDIO_BYTES = 10 * 1024 * 1024


@dataclass
class KioskDeps:
    """Everything the kiosk endpoints touch. Fields arrive ready-built from
    `shell.run()`; None is a legitimate value for a capability that is off."""

    journal: Any
    graph: Any
    pipeline: Any
    jobs: Any
    transcriber: Any
    budget: Any = None
    cassette: Any = None
    chat_agent: Any = None
    pin: str | None = None
    classify_timeout_s: float = 12.0
    """How long the capture endpoint waits for enrichment before answering
    "pending". The thought is durable either way; this only bounds the tablet's
    spinner."""


class CaptureRequest(BaseModel):
    text: str
    client_id: str
    source: InputSource = "voice"
    transcript: str | None = None


def build_web_app(deps: KioskDeps) -> FastAPI:
    # No docs endpoints: the kiosk has exactly one client, and an API explorer on a
    # family device is surface area with no user.
    app = FastAPI(title="VOS kitchen kiosk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.deps = deps

    @app.middleware("http")
    async def pin_gate(request: Request, call_next) -> Response:
        """Gate every /api/ route except health.

        Health stays open so a fresh page can ask "do I need to prompt for a PIN?"
        before it has one. The comparison is constant-time; a PIN is four digits
        typed by a family member, but timing-safe costs nothing.
        """
        path = request.url.path
        if deps.pin is not None and path.startswith("/api/") and path != "/api/health":
            supplied = request.headers.get("X-VOS-PIN", "")
            if not secrets.compare_digest(supplied.encode(), deps.pin.encode()):
                return JSONResponse({"detail": "PIN required"}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "pin_required": deps.pin is not None}

    @app.get("/api/ping")
    async def ping() -> dict:
        """Behind the gate — the frontend verifies an entered PIN by pinging."""
        return {"ok": True}

    # One clip at a time. Whisper saturates the CPU; two concurrent transcriptions
    # would make both slower than running them in sequence, and there is one tablet.
    transcribe_gate = asyncio.Semaphore(1)

    @app.post("/api/transcribe")
    async def transcribe(audio: UploadFile) -> dict:
        """Audio in, text out. The bytes live in this request's memory and nowhere
        else — nothing here writes them to disk, and nothing downstream sees them."""
        data = await audio.read()
        if len(data) > _MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio too large")
        if not data:
            raise HTTPException(status_code=400, detail="empty audio")
        async with transcribe_gate:
            text = await deps.transcriber.transcribe(
                data, audio.content_type or "audio/webm"
            )
        return {"transcript": text}

    @app.post("/api/capture")
    async def capture(req: CaptureRequest) -> Any:
        """The §8.1 contract over HTTP: journal (fsync) → ack → enrich.

        The handler itself touches only the journal. Every graph write happens
        inside the job below, on the single JobQueue worker — this handler runs
        concurrently with aiogram handlers, and ADR-008 stays true only if the
        queue is the sole path to the graph from here.
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")

        record = CaptureRecord.create_kitchen(
            client_id=req.client_id,
            text=text,
            captured_at=datetime.now(UTC),
            source=req.source,
            transcript=req.transcript,
        )

        # --- durability boundary ---------------------------------------- #
        try:
            await deps.journal.append(record)
        except OSError as exc:
            log.exception("Journal write failed for kiosk capture %s", record.id)
            return JSONResponse(
                {"saved": False, "detail": type(exc).__name__}, status_code=503
            )

        # From here the thought is safe; enrichment may fail without losing it.
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

        async def enrich() -> None:
            try:
                await deps.graph.upsert_thought(record, None)
                if deps.budget is not None and deps.budget.exceeded():
                    with contextlib.suppress(Exception):
                        await deps.graph.mark_unclassified(
                            record.id, "daily budget reached"
                        )
                    outcome = {"status": "unclassified", "error": "daily budget reached"}
                else:
                    classification, error, _linked = await classify_one(
                        deps.pipeline, deps.graph, record
                    )
                    if classification is not None:
                        outcome = {
                            "status": "classified",
                            "category": classification.category,
                            "title": classification.title,
                        }
                    else:
                        outcome = {"status": "unclassified", "error": error}
            except Exception as exc:  # noqa: BLE001 — e.g. the graph is down
                log.exception("Kiosk enrichment failed for %s", record.id)
                outcome = {"status": "unclassified", "error": type(exc).__name__}
            if not fut.done():  # done = the request already timed out and moved on
                fut.set_result(outcome)

        await deps.jobs.submit(f"kitchen:{record.id}", enrich)

        try:
            outcome = await asyncio.wait_for(fut, timeout=deps.classify_timeout_s)
        except TimeoutError:
            # Same shape as Telegram's budget/failure path: the thought is saved
            # and sits in /pending; the tablet is told exactly that.
            return {"saved": True, "id": str(record.id), "status": "pending"}
        return {"saved": True, "id": str(record.id), **outcome}

    # Mounted last: anything that is not /api/* is the SPA.
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app


def start_server(app: FastAPI, host: str, port: int) -> tuple[asyncio.Task, Any]:
    """Run uvicorn inside the already-running event loop, beside aiogram.

    Returns the serving task and the server; shutdown is `server.should_exit = True`
    then awaiting the task — the shell gives it five seconds in its `finally`.
    """
    import contextlib

    import uvicorn

    class _QuietServer(uvicorn.Server):
        # The load-bearing no-ops. aiogram's polling loop owns Ctrl+C; left alone,
        # uvicorn replaces the SIGINT handler with one that sets its own
        # `should_exit` and never raises KeyboardInterrupt — the bot would simply
        # stop being stoppable. `capture_signals` is the uvicorn >= 0.29 mechanism;
        # `install_signal_handlers` covers older versions if the pin ever moves.

        @contextlib.contextmanager
        def capture_signals(self):
            yield

        def install_signal_handlers(self) -> None:
            pass

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,  # the shell already configured logging
        access_log=False,  # per-request lines for one tablet are noise
        lifespan="on",
    )
    server = _QuietServer(config)
    task = asyncio.create_task(server.serve(), name="vos-web")
    return task, server
