"""The xAI pulse client.

Two things here earn their tests. Handles arrive in every shape a person might type
them, and they have to reach xAI as bare names while matching a stored `@handle`
source. And the model can return a plausible-looking post URL that points nowhere —
those are dropped and counted rather than rendered as if they were checkable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vos.contracts import PulseError
from vos.pulse import (
    SOURCE_COST_USD,
    PulseFetcher,
    build_search_parameters,
    canonical_post_url,
    normalise_handle,
    parse_digest,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


# -- handles ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "@karpathy",
        "karpathy",
        "  @Karpathy ",
        "https://x.com/karpathy",
        "https://twitter.com/karpathy",
        "https://x.com/karpathy?s=20",
    ],
)
def test_every_handle_shape_normalises_the_same(raw: str):
    """Whatever the user types, it must match the stored source and reach xAI bare."""
    assert normalise_handle(raw) == "@karpathy"


@pytest.mark.parametrize("raw", ["", "@", "not a handle", "@waytoolongforahandle1"])
def test_invalid_handles_are_rejected(raw: str):
    assert normalise_handle(raw) is None


# -- post urls -------------------------------------------------------------- #


def test_twitter_and_x_urls_canonicalise_together():
    """Otherwise the same post gets two ids and appears twice in /more."""
    a = canonical_post_url("https://twitter.com/karpathy/status/123")
    b = canonical_post_url("https://x.com/karpathy/status/123")
    assert a == b == "https://x.com/karpathy/status/123"


def test_tracking_parameters_are_stripped():
    assert (
        canonical_post_url("https://x.com/karpathy/status/123?s=46&t=abc")
        == "https://x.com/karpathy/status/123"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/karpathy",
        "https://example.com/karpathy/status/123",
        "not a url",
        "https://x.com/karpathy/status/abc",
    ],
)
def test_non_post_urls_are_rejected(url: str):
    assert canonical_post_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/karpathy/status/123abc456",
        "https://x.com/karpathy/status/123abc",
    ],
)
def test_trailing_garbage_after_the_status_id_is_rejected(url: str):
    """A truncated match would rewrite a malformed id into a well-formed link that
    points at a post which does not exist — exactly the failure this function
    exists to prevent."""
    assert canonical_post_url(url) is None


def test_status_id_followed_by_a_trailing_slash_still_canonicalises():
    assert (
        canonical_post_url("https://x.com/karpathy/status/123/")
        == "https://x.com/karpathy/status/123"
    )


def test_status_id_followed_by_a_query_string_still_canonicalises():
    assert (
        canonical_post_url("https://x.com/karpathy/status/123?s=20")
        == "https://x.com/karpathy/status/123"
    )


# -- request building ------------------------------------------------------- #


def test_followed_handles_reach_xai_without_the_at_sign():
    params = build_search_parameters(["@karpathy", "@sama"], now=NOW, max_sources=25)
    assert params["sources"] == [{"type": "x", "x_handles": ["karpathy", "sama"]}]


def test_no_handles_means_an_unfiltered_trending_search():
    """Following nobody is a normal state, not an error — the digest still works."""
    params = build_search_parameters([], now=NOW, max_sources=25)
    assert params["sources"] == [{"type": "x"}]
    assert "x_handles" not in params["sources"][0]


def test_search_window_is_the_last_day():
    params = build_search_parameters([], now=NOW, max_sources=25)
    assert params["from_date"] == "2026-08-08"


def test_source_cap_is_passed_through():
    """This is the cost lever — $0.025 a source — so it must not be silently dropped."""
    assert build_search_parameters([], now=NOW, max_sources=8)["max_search_results"] == 8


# -- parsing ---------------------------------------------------------------- #


def _payload(**overrides) -> str:
    body = {
        "summary": "A quiet day.",
        "posts": [
            {
                "text": "Something concrete happened.",
                "author_handle": "@karpathy",
                "url": "https://x.com/karpathy/status/123",
                "section": "Releases",
                "score": 0.9,
            }
        ],
    }
    body.update(overrides)
    return json.dumps(body)


def test_a_valid_payload_parses():
    digest, dropped = parse_digest(_payload(), topic="AI", asked_at=NOW, handles=[])
    assert dropped == 0
    assert digest.topic == "AI"
    assert digest.posts[0].author_handle == "@karpathy"
    assert digest.posts[0].score == 0.9


def test_items_without_a_usable_link_are_dropped_and_counted():
    """Silent truncation is the defect this codebase already fixed once for notes."""
    payload = _payload(
        posts=[
            {"text": "real", "author_handle": "@a", "url": "https://x.com/a/status/1"},
            {"text": "fake", "author_handle": "@b", "url": "https://x.com/b"},
        ]
    )
    digest, dropped = parse_digest(payload, topic="AI", asked_at=NOW, handles=[])
    assert [p.text for p in digest.posts] == ["real"]
    assert dropped == 1


def test_urls_are_stored_canonicalised():
    payload = _payload(
        posts=[
            {
                "text": "x",
                "author_handle": "@a",
                "url": "https://twitter.com/a/status/9?s=20",
            }
        ]
    )
    digest, _ = parse_digest(payload, topic="AI", asked_at=NOW, handles=[])
    assert digest.posts[0].url == "https://x.com/a/status/9"


def test_handles_in_items_are_normalised():
    payload = _payload(
        posts=[
            {"text": "x", "author_handle": "Karpathy", "url": "https://x.com/a/status/1"}
        ]
    )
    digest, _ = parse_digest(payload, topic="AI", asked_at=NOW, handles=[])
    assert digest.posts[0].author_handle == "@karpathy"


def test_malformed_json_raises_valueerror():
    with pytest.raises(ValueError):
        parse_digest("not json at all", topic="AI", asked_at=NOW, handles=[])


def test_json_fenced_in_markdown_still_parses():
    """Models wrap JSON in code fences even when told not to."""
    fenced = "```json\n" + _payload() + "\n```"
    digest, _ = parse_digest(fenced, topic="AI", asked_at=NOW, handles=[])
    assert digest.summary == "A quiet day."


def test_a_payload_with_no_posts_is_valid_not_an_error():
    """A genuinely quiet day must not look like a failure."""
    digest, dropped = parse_digest(
        _payload(posts=[]), topic="AI", asked_at=NOW, handles=[]
    )
    assert digest.posts == []
    assert dropped == 0


# -- PulseFetcher ------------------------------------------------------------ #


def _response(content: str, *, sources: int = 3) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "num_sources_used": sources,
        },
    }


class StubTransport:
    """Records request bodies and replays canned responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fetcher(tmp_path: Path, transport) -> PulseFetcher:
    return PulseFetcher(
        api_key="test-key",
        artifact_dir=tmp_path,
        model="grok-4.1-fast",
        max_sources=25,
        transport=transport,
        now=lambda: NOW,
    )


async def test_fetch_returns_a_digest(tmp_path: Path):
    fetcher = _fetcher(tmp_path, StubTransport(_response(_payload())))
    artifact, dropped = await fetcher.fetch("AI", [])
    assert dropped == 0
    assert artifact.digest.posts[0].author_handle == "@karpathy"
    assert artifact.model == "grok-4.1-fast"


async def test_the_digest_is_cached_to_disk(tmp_path: Path):
    """A digest cannot be re-derived tomorrow and re-asking costs money."""
    fetcher = _fetcher(tmp_path, StubTransport(_response(_payload())))
    await fetcher.fetch("AI", [])
    assert list((tmp_path / "pulses").glob("*.json"))


async def test_an_invalid_response_is_retried_once(tmp_path: Path):
    transport = StubTransport(_response("not json"), _response(_payload()))
    fetcher = _fetcher(tmp_path, transport)
    artifact, _ = await fetcher.fetch("AI", [])
    assert artifact.digest.posts
    assert len(transport.bodies) == 2


async def test_the_retry_tells_the_model_what_was_wrong(tmp_path: Path):
    """A bare retry would most likely fail the same way."""
    transport = StubTransport(_response("not json"), _response(_payload()))
    fetcher = _fetcher(tmp_path, transport)

    await fetcher.fetch("AI", [])
    retry_messages = transport.bodies[1]["messages"]
    assert "not JSON" in retry_messages[-1]["content"]


async def test_two_invalid_responses_raise(tmp_path: Path):
    transport = StubTransport(_response("nope"), _response("still nope"))
    fetcher = _fetcher(tmp_path, transport)
    with pytest.raises(PulseError, match="unusable"):
        await fetcher.fetch("AI", [])


async def test_a_transport_failure_raises_pulse_error(tmp_path: Path):
    """The user sees "couldn't fetch", not an httpx traceback."""
    fetcher = _fetcher(tmp_path, StubTransport(RuntimeError("connection reset")))
    with pytest.raises(PulseError):
        await fetcher.fetch("AI", [])


async def test_cost_counts_sources_and_tokens(tmp_path: Path):
    fetcher = _fetcher(tmp_path, StubTransport(_response(_payload(), sources=10)))
    artifact, _ = await fetcher.fetch("AI", [])
    assert artifact.sources_used == 10
    assert artifact.cost_usd is not None
    assert artifact.cost_usd >= 10 * SOURCE_COST_USD


async def test_cost_assumes_the_full_cap_when_usage_is_missing(tmp_path: Path):
    """Under-counting spend would let the budget guard be walked straight through."""
    response = _response(_payload())
    del response["usage"]
    fetcher = _fetcher(tmp_path, StubTransport(response))
    artifact, _ = await fetcher.fetch("AI", [])
    assert artifact.cost_usd is not None
    assert artifact.cost_usd >= 25 * SOURCE_COST_USD


async def test_followed_handles_reach_the_request(tmp_path: Path):
    transport = StubTransport(_response(_payload()))
    fetcher = _fetcher(tmp_path, transport)
    await fetcher.fetch("AI", ["@karpathy"])
    params = transport.bodies[0]["search_parameters"]
    assert params["sources"][0]["x_handles"] == ["karpathy"]
