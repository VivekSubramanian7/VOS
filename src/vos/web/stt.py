"""Local speech-to-text — the kiosk's only audio processing.

faster-whisper runs on this machine's CPU. Audio bytes arrive from the browser, are
decoded in memory by the bundled PyAV (no host ffmpeg), and are never written to
disk: the privacy posture is that family audio exists only in RAM, only for the
duration of one call.
"""

from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Callable
from typing import Any

ModelFactory = Callable[[str], Any]
"""Builds the underlying model given its name. Injectable so tests never touch the
real thing — the default downloads hundreds of MB on first use."""


def _default_factory(model_name: str) -> Any:
    # Imported here, not at module top: the adapter must be importable (for tests
    # and type checks) on installs without the kiosk extra.
    from faster_whisper import WhisperModel

    # int8 halves memory and is the documented fast path on CPU; accuracy loss is
    # negligible at this clip length. GPU would be `device="cuda"`, deliberately
    # not configurable until someone actually has one.
    return WhisperModel(model_name, device="cpu", compute_type="int8")


class FasterWhisperTranscriber:
    """Implements the `Transcriber` protocol declared in contracts.py.

    Loading is lazy: the daemon must start instantly with the kiosk enabled but not
    yet used, and the first mic tap pays the model load. The load sits behind a
    `threading.Lock` because two racing first-calls would otherwise each load a copy.
    """

    def __init__(self, model_name: str = "small", *, model_factory: ModelFactory | None = None):
        self._model_name = model_name
        self._factory = model_factory or _default_factory
        self._model: Any = None
        self._lock = threading.Lock()

    async def transcribe(self, audio: bytes, mime: str) -> str:
        # The mime is advisory; PyAV sniffs the container from the bytes themselves.
        return await asyncio.to_thread(self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: bytes) -> str:
        model = self._model_get_or_load()
        segments, _info = model.transcribe(io.BytesIO(audio), vad_filter=True)
        # `transcribe` returns a *lazy* generator — decoding happens as it is
        # consumed. Joining here, inside the worker thread, is what keeps that work
        # off the event loop; returning the generator would smuggle it back on.
        return " ".join(s.text.strip() for s in segments).strip()

    def _model_get_or_load(self) -> Any:
        with self._lock:
            if self._model is None:
                self._model = self._factory(self._model_name)
            return self._model
