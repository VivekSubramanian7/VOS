"""Contract tests.

The one that matters is determinism of `thought_id` — it is the mechanism that turns
Telegram's at-least-once delivery into exactly-once capture.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vos.contracts import (
    CATEGORIES,
    CaptureRecord,
    Classification,
    ExtractedEntity,
    PostView,
    PulseDigest,
    PulsePost,
    PulseResult,
    SourceRef,
    canonical,
    post_id,
    pulse_id,
    thought_id,
)


def test_thought_id_is_deterministic():
    assert thought_id(42, 7) == thought_id(42, 7)


def test_thought_id_distinguishes_chat_and_message():
    assert thought_id(42, 7) != thought_id(42, 8)
    assert thought_id(42, 7) != thought_id(43, 7)
    # Guards against a naive f"{chat}{message}" concatenation, where (1, 23) and
    # (12, 3) would collide.
    assert thought_id(1, 23) != thought_id(12, 3)


def test_capture_record_create_derives_id():
    r = CaptureRecord.create(
        chat_id=42, message_id=7, text="hello", captured_at=datetime.now(UTC)
    )
    assert r.id == thought_id(42, 7)
    assert r.kind == "capture"
    assert r.source == "text"


def test_capture_record_is_frozen():
    r = CaptureRecord.create(
        chat_id=1, message_id=1, text="x", captured_at=datetime.now(UTC)
    )
    with pytest.raises(ValidationError):
        r.text = "mutated"  # type: ignore[misc]


def test_classification_rejects_unknown_category():
    with pytest.raises(ValidationError):
        Classification(category="Nonsense", title="t", summary="s")  # type: ignore[arg-type]


def test_classification_bounds_confidence():
    with pytest.raises(ValidationError):
        Classification(category="Other", title="t", summary="s", confidence=1.5)


def test_all_categories_are_valid_classifications():
    for c in CATEGORIES:
        Classification(category=c, title="t", summary="s")  # type: ignore[arg-type]


def test_entity_salience_bounds():
    ExtractedEntity(name="Japan", type="place", salience=0.0)
    ExtractedEntity(name="Japan", type="place", salience=1.0)
    with pytest.raises(ValidationError):
        ExtractedEntity(name="Japan", type="place", salience=-0.1)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Naval Ravikant", "naval ravikant"),
        ("  Naval   Ravikant ", "Naval Ravikant"),
        ("NVDA", "nvda"),
    ],
)
def test_canonical_collapses_case_and_whitespace(a: str, b: str):
    assert canonical(a) == canonical(b)


def test_canonical_keeps_distinct_names_distinct():
    # Normalisation must not over-collapse: these are different people.
    assert canonical("Naval") != canonical("Navalny")
    assert canonical("Paul Graham") != canonical("Paul Grahams")


def test_source_ref_canonical_name():
    assert SourceRef(name="  Paul  Graham ", kind="person").canonical_name == "paul graham"


def test_post_id_is_stable_for_the_same_url():
    """The same post surfacing in two digests must stay one node in the graph."""
    url = "https://x.com/karpathy/status/1234567890"
    assert post_id(url) == post_id(url)


def test_post_id_differs_between_posts():
    assert post_id("https://x.com/a/status/1") != post_id("https://x.com/a/status/2")


def test_pulse_id_is_stable_for_the_same_run():
    at = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    assert pulse_id("AI", at) == pulse_id("AI", at)


def test_pulse_id_differs_between_runs():
    """Each run is its own digest even on the same topic."""
    a = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    b = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)
    assert pulse_id("AI", a) != pulse_id("AI", b)


def test_post_score_defaults_to_mid():
    post = PulsePost(
        text="a claim", author_handle="@karpathy", url="https://x.com/karpathy/status/1"
    )
    assert post.score == 0.5


def test_post_view_deep_link_is_the_post_url():
    view = PostView(
        id=post_id("https://x.com/karpathy/status/1"),
        text="a claim",
        author_handle="@karpathy",
        url="https://x.com/karpathy/status/1",
        topic="AI",
        asked_at=datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert view.deep_link == "https://x.com/karpathy/status/1"


def test_pulse_result_is_not_ok_without_a_digest():
    assert not PulseResult(topic="AI", error="boom").ok


def test_pulse_result_is_ok_with_a_digest():
    digest = PulseDigest(
        topic="AI", summary="s", posts=[], asked_at=datetime(2026, 8, 9, tzinfo=UTC)
    )
    assert PulseResult(topic="AI", digest=digest).ok
