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
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static"


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
