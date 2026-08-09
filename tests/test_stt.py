"""STT adapter tests.

The adapter is the kiosk's only audio processing, so the properties that matter are:
it satisfies the `Transcriber` protocol (the seam declared in Phase 1), the model
loads exactly once no matter how many calls race, and the lazy segment generator is
fully consumed before `transcribe` returns — a half-consumed generator would leak
decoding work onto whatever awaits next.

The real model is env-gated: it downloads ~250 MB and takes seconds, which is a
manual verification step, not CI material.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest

from vos.contracts import Transcriber
from vos.web.stt import FasterWhisperTranscriber


@dataclass
class _Segment:
    text: str


class _FakeModel:
    """Mimics faster_whisper.WhisperModel.transcribe: returns a *lazy* generator."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        info = object()
        return (_Segment(t) for t in self.texts), info


def _factory_returning(model: _FakeModel):
    calls = {"n": 0}

    def factory(model_name: str):
        calls["n"] += 1
        return model

    return factory, calls


def test_satisfies_the_transcriber_protocol():
    stt = FasterWhisperTranscriber("small", model_factory=lambda name: _FakeModel([]))
    assert isinstance(stt, Transcriber)


async def test_joins_segments_into_one_transcript():
    model = _FakeModel([" Buy milk,", " and eggs. "])
    factory, _ = _factory_returning(model)
    stt = FasterWhisperTranscriber("small", model_factory=factory)

    text = await stt.transcribe(b"opus-bytes", "audio/webm")

    assert text == "Buy milk, and eggs."


async def test_empty_audio_yields_empty_string():
    factory, _ = _factory_returning(_FakeModel([]))
    stt = FasterWhisperTranscriber("small", model_factory=factory)
    assert await stt.transcribe(b"", "audio/webm") == ""


async def test_model_loads_once_across_concurrent_calls():
    """Two first-calls racing must not load two copies of a multi-hundred-MB model."""
    model = _FakeModel(["hi"])
    factory, calls = _factory_returning(model)
    stt = FasterWhisperTranscriber("small", model_factory=factory)

    await asyncio.gather(*(stt.transcribe(b"x", "audio/webm") for _ in range(8)))

    assert calls["n"] == 1
    assert model.calls == 8


async def test_generator_is_consumed_before_transcribe_returns():
    """`WhisperModel.transcribe` is lazy; if the adapter returned the generator's work
    to the event loop, the result would not be a plain string already joined."""
    consumed: list[str] = []

    class _TrackingModel:
        def transcribe(self, audio, **kwargs):
            def gen():
                for t in ("a", "b"):
                    consumed.append(t)
                    yield _Segment(t)

            return gen(), object()

    stt = FasterWhisperTranscriber("small", model_factory=lambda name: _TrackingModel())
    text = await stt.transcribe(b"x", "audio/webm")

    assert consumed == ["a", "b"]
    assert text == "a b"


@pytest.mark.skipif(
    not os.environ.get("VOS_TEST_REAL_WHISPER"),
    reason="downloads the real model; set VOS_TEST_REAL_WHISPER=1 to run",
)
async def test_real_model_transcribes_generated_tone():
    """Manual gate: proves the bundled PyAV decodes webm/opus without host ffmpeg."""
    import io as _io

    import av
    import numpy as np

    buf = _io.BytesIO()
    container = av.open(buf, mode="w", format="webm")
    stream = container.add_stream("libopus", rate=48000)
    silence = np.zeros((1, 960), dtype=np.int16)
    for _ in range(50):  # ~1s
        frame = av.AudioFrame.from_ndarray(silence, format="s16", layout="mono")
        frame.sample_rate = 48000
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()

    stt = FasterWhisperTranscriber(os.environ.get("VOS_WHISPER_MODEL", "base"))
    text = await stt.transcribe(buf.getvalue(), "audio/webm")
    assert isinstance(text, str)  # silence may legitimately transcribe to ""
