"""Web skeleton tests.

Two properties carry this story: the PIN gate actually gates (constant-time, header
only, but `/api/health` stays open so the frontend can discover whether a PIN is
needed at all), and a co-hosted uvicorn drains promptly when told to exit — the
shell's `finally` gives it five seconds before moving on to stop the job queue.
"""

from __future__ import annotations

import asyncio

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from vos.web.app import KioskDeps, build_web_app, start_server  # noqa: E402


def _deps(pin: str | None = None) -> KioskDeps:
    return KioskDeps(
        journal=None,
        graph=None,
        pipeline=None,
        jobs=None,
        transcriber=None,
        pin=pin,
    )


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
