"""Graph projection tests — against a real, ephemeral Neo4j.

The projection is derived and rebuildable, so the tests that matter are about
*idempotency*: re-projecting the same record must produce exactly the same graph as
projecting it once. That property is what makes crash recovery and `--rebuild` safe
rather than hopeful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vos.contracts import (
    CATEGORIES,
    CaptureRecord,
    Classification,
    ExtractedEntity,
    PulseDigest,
    PulsePost,
    SourceRef,
    VideoMeta,
    VideoNote,
    pulse_id,
)
from vos.graph import Neo4jGraph

pytestmark = pytest.mark.integration


def _record(n: int = 1, text: str = "book flights to Tokyo", minutes: int = 0) -> CaptureRecord:
    return CaptureRecord.create(
        chat_id=42,
        message_id=n,
        text=text,
        captured_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes),
    )


def _classification(
    category: str = "TripPlanning", entities: list[str] | None = None
) -> Classification:
    return Classification(
        category=category,  # type: ignore[arg-type]
        title="Tokyo flights",
        summary="Book flights to Tokyo.",
        entities=[
            ExtractedEntity(name=name, type="place", salience=0.8)
            for name in (entities if entities is not None else ["Tokyo"])
        ],
        confidence=0.9,
    )


# --- idempotency (the load-bearing property) ------------------------------- #


async def test_reprojecting_same_record_creates_no_duplicates(graph: Neo4jGraph):
    record, classification = _record(), _classification()
    for _ in range(3):
        await graph.upsert_thought(record, classification)

    rows = await graph._run("MATCH (t:Thought) RETURN count(t) AS n")
    assert rows[0]["n"] == 1
    rows = await graph._run("MATCH (:Thought)-[m:MENTIONS]->() RETURN count(m) AS n")
    assert rows[0]["n"] == 1
    rows = await graph._run("MATCH (:Thought)-[c:IN_CATEGORY]->() RETURN count(c) AS n")
    assert rows[0]["n"] == 1


async def test_redelivered_telegram_update_is_one_thought(graph: Neo4jGraph):
    """Same (chat_id, message_id) delivered twice -> one node, because the ID is
    derived rather than generated."""
    first = CaptureRecord.create(
        chat_id=42, message_id=7, text="same", captured_at=datetime.now(UTC)
    )
    second = CaptureRecord.create(
        chat_id=42, message_id=7, text="same", captured_at=datetime.now(UTC)
    )
    assert first.id == second.id
    await graph.upsert_thought(first, None)
    await graph.upsert_thought(second, None)
    assert (await graph.stats()).total == 1


async def test_kitchen_capture_persists_its_channel(graph: Neo4jGraph):
    """`channel` is what lets 'where did this come from?' be answered later; it must
    survive the projection, and re-projection must not duplicate the node."""
    record = CaptureRecord.create_kitchen(
        client_id="c-9",
        text="buy milk",
        captured_at=datetime.now(UTC),
        source="voice",
        transcript="by milk",
    )
    await graph.upsert_thought(record, None)
    await graph.upsert_thought(record, None)

    rows = await graph._run(
        "MATCH (t:Thought {id: $id}) RETURN t.channel AS channel, count(t) AS n",
        id=str(record.id),
    )
    assert rows[0]["channel"] == "kitchen"
    assert rows[0]["n"] == 1


async def test_telegram_capture_channel_defaults(graph: Neo4jGraph):
    record = _record()
    await graph.upsert_thought(record, None)
    rows = await graph._run(
        "MATCH (t:Thought {id: $id}) RETURN t.channel AS channel", id=str(record.id)
    )
    assert rows[0]["channel"] == "telegram"


async def test_reclassification_replaces_rather_than_accumulates(graph: Neo4jGraph):
    """A prompt change must not leave the old category and entities behind."""
    record = _record()
    await graph.upsert_thought(record, _classification("TripPlanning", ["Tokyo"]))
    await graph.upsert_thought(record, _classification("StudyResearch", ["Kyoto"]))

    (view,) = await graph.recent()
    assert view.category == "StudyResearch"
    assert view.entities == ["Kyoto"]
    rows = await graph._run("MATCH (:Thought)-[c:IN_CATEGORY]->() RETURN count(c) AS n")
    assert rows[0]["n"] == 1


# --- capture without classification ---------------------------------------- #


async def test_capture_without_classification_is_still_stored(graph: Neo4jGraph):
    """The whole point of separating capture from enrichment."""
    await graph.upsert_thought(_record(text="unclassifiable"), None)
    (view,) = await graph.recent()
    assert view.text == "unclassifiable"
    assert view.status == "captured"
    assert view.category is None


async def test_mark_unclassified_surfaces_in_pending(graph: Neo4jGraph):
    record = _record()
    await graph.upsert_thought(record, None)
    await graph.mark_unclassified(record.id, "provider down")

    pending = await graph.pending()
    assert [p.id for p in pending] == [record.id]
    assert (await graph.stats()).pending == 1


async def test_retry_after_failure_clears_pending(graph: Neo4jGraph):
    record = _record()
    await graph.upsert_thought(record, None)
    await graph.mark_unclassified(record.id, "provider down")
    await graph.upsert_thought(record, _classification())
    assert await graph.pending() == []


# --- follows (ADR-009) ------------------------------------------------------ #


async def test_follow_and_mention_converge_on_one_node(graph: Neo4jGraph):
    """The property that justified the dual-label design: a declared source and a
    mention of it are the same node, with no extra relationship type."""
    await graph.follow(SourceRef(name="Naval Ravikant", kind="person"))
    await graph.upsert_thought(
        _record(text="naval on leverage"),
        Classification(
            category="Career",
            title="Leverage",
            summary="On leverage.",
            entities=[ExtractedEntity(name="naval ravikant", type="person", salience=0.9)],
        ),
    )
    rows = await graph._run(
        "MATCH (e:Entity {canonical_name:'naval ravikant'}) RETURN count(e) AS n, labels(e) AS l"
    )
    assert rows[0]["n"] == 1
    assert "Source" in rows[0]["l"]


async def test_upsert_returns_linked_followed_sources(graph: Neo4jGraph):
    await graph.follow(SourceRef(name="Paul Graham", kind="person"))
    linked = await graph.upsert_thought(
        _record(text="pg essay"),
        Classification(
            category="Career",
            title="Essay",
            summary="An essay.",
            entities=[
                ExtractedEntity(name="Paul Graham", type="person", salience=0.9),
                ExtractedEntity(name="Tokyo", type="place", salience=0.2),
            ],
        ),
    )
    assert linked == ["Paul Graham"]  # only the followed one


async def test_follow_before_or_after_mention_is_equivalent(graph: Neo4jGraph):
    """Declaring a follow for an entity that already exists must not duplicate it."""
    await graph.upsert_thought(
        _record(text="thinking about naval"),
        Classification(
            category="Career",
            title="t",
            summary="s",
            entities=[ExtractedEntity(name="Naval", type="person", salience=0.9)],
        ),
    )
    await graph.follow(SourceRef(name="naval", kind="person"))
    rows = await graph._run("MATCH (e:Entity {canonical_name:'naval'}) RETURN count(e) AS n")
    assert rows[0]["n"] == 1


async def test_unfollow_keeps_entity_but_drops_source_label(graph: Neo4jGraph):
    await graph.follow(SourceRef(name="Naval", kind="person"))
    assert await graph.unfollow("naval") is True
    rows = await graph._run(
        "MATCH (e:Entity {canonical_name:'naval'}) RETURN count(e) AS n, labels(e) AS l"
    )
    assert rows[0]["n"] == 1
    assert "Source" not in rows[0]["l"]
    assert await graph.following() == []


async def test_unfollow_unknown_returns_false(graph: Neo4jGraph):
    assert await graph.unfollow("nobody") is False


async def test_following_round_trip(graph: Neo4jGraph):
    await graph.follow(SourceRef(name="Sapiens", kind="book", author="Harari"))
    (s,) = await graph.following()
    assert (s.name, s.kind, s.author) == ("Sapiens", "book", "Harari")


# --- reads ------------------------------------------------------------------ #


async def test_recent_is_newest_first_and_excludes_deleted(graph: Neo4jGraph):
    for i in range(3):
        await graph.upsert_thought(_record(i, text=f"t{i}", minutes=i), _classification())
    deleted = _record(9, text="gone", minutes=9)
    await graph.upsert_thought(deleted, _classification())
    await graph.soft_delete(deleted.id)

    views = await graph.recent(10)
    assert [v.text for v in views] == ["t2", "t1", "t0"]


async def test_by_category_filters(graph: Neo4jGraph):
    trip, stock = _classification("TripPlanning"), _classification("StockResearch")
    await graph.upsert_thought(_record(1, text="trip", minutes=1), trip)
    await graph.upsert_thought(_record(2, text="stock", minutes=2), stock)
    views = await graph.by_category("StockResearch")
    assert [v.text for v in views] == ["stock"]


async def test_search_finds_by_text(graph: Neo4jGraph):
    shopping, stock = _classification("Shopping"), _classification("StockResearch")
    await graph.upsert_thought(_record(1, text="oat milk and bananas"), shopping)
    await graph.upsert_thought(_record(2, text="quarterly earnings"), stock)
    views = await graph.search("bananas")
    assert [v.text for v in views] == ["oat milk and bananas"]


async def test_soft_delete_hides_but_does_not_remove(graph: Neo4jGraph):
    record = _record()
    await graph.upsert_thought(record, _classification())
    await graph.soft_delete(record.id)
    assert await graph.recent() == []
    rows = await graph._run("MATCH (t:Thought) RETURN count(t) AS n")
    assert rows[0]["n"] == 1  # still there, for journal/graph consistency


async def test_deleted_thought_is_not_revived_by_reprojection(graph: Neo4jGraph):
    """Replay must honour a tombstone even though the capture line still exists."""
    record = _record()
    await graph.upsert_thought(record, _classification())
    await graph.soft_delete(record.id)
    await graph.upsert_thought(record, _classification())
    assert await graph.recent() == []


async def test_existing_ids_supports_startup_reprojection(graph: Neo4jGraph):
    a, b = _record(1), _record(2)
    await graph.upsert_thought(a, None)
    ids = await graph.existing_ids()
    assert a.id in ids and b.id not in ids


async def test_stats(graph: Neo4jGraph):
    await graph.follow(SourceRef(name="Naval", kind="person"))
    await graph.upsert_thought(
        _record(1, text="a", minutes=1),
        Classification(
            category="Career",
            title="t",
            summary="s",
            entities=[ExtractedEntity(name="Naval", type="person", salience=0.9)],
        ),
    )
    await graph.upsert_thought(_record(2, text="b", minutes=2), _classification("Shopping"))

    stats = await graph.stats()
    assert stats.total == 2
    assert stats.by_category == {"Career": 1, "Shopping": 1}
    assert stats.top_sources == [("Naval", 1)]


# --- threading -------------------------------------------------------------- #


async def test_next_links_thoughts_within_a_category(graph: Neo4jGraph):
    first = _record(1, text="first", minutes=1)
    second = _record(2, text="second", minutes=2)
    await graph.upsert_thought(first, _classification("TripPlanning"))
    await graph.upsert_thought(second, _classification("TripPlanning"))

    rows = await graph._run(
        "MATCH (a:Thought)-[:NEXT]->(b:Thought) RETURN a.text AS a, b.text AS b"
    )
    assert rows == [{"a": "first", "b": "second"}]


async def test_next_links_correctly_when_replayed_out_of_order(graph: Neo4jGraph):
    """Replay does not follow wall-clock order, so threading must not assume it."""
    later = _record(2, text="second", minutes=2)
    earlier = _record(1, text="first", minutes=1)
    await graph.upsert_thought(later, _classification("TripPlanning"))
    await graph.upsert_thought(earlier, _classification("TripPlanning"))

    rows = await graph._run(
        "MATCH (a:Thought)-[:NEXT]->(b:Thought) RETURN a.text AS a, b.text AS b"
    )
    assert rows == [{"a": "first", "b": "second"}]


async def test_next_does_not_cross_categories(graph: Neo4jGraph):
    await graph.upsert_thought(_record(1, text="trip", minutes=1), _classification("TripPlanning"))
    await graph.upsert_thought(_record(2, text="shop", minutes=2), _classification("Shopping"))
    rows = await graph._run("MATCH (:Thought)-[n:NEXT]->(:Thought) RETURN count(n) AS n")
    assert rows[0]["n"] == 0


# --- rebuild ---------------------------------------------------------------- #


# --- video → knowledge -------------------------------------------------------- #


def _meta(video_id: str = "dQw4w9WgXcQ") -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Speed of Light",
        channel="Veritasium",
        approx_duration_s=900,
    )


def _note(text: str, t: int, entity: str = "Einstein") -> VideoNote:
    return VideoNote(
        text=text,
        t_seconds=t,
        entities=[ExtractedEntity(name=entity, type="person", salience=0.8)],
    )


async def test_video_attaches_to_its_channel_entity(graph: Neo4jGraph):
    await graph.upsert_video(_meta())
    rows = await graph._run(
        "MATCH (:Video)-[:PUBLISHED_BY]->(e:Entity) RETURN e.name AS name"
    )
    assert rows == [{"name": "Veritasium"}]


async def test_following_a_channel_converges_on_the_video_publisher(graph: Neo4jGraph):
    """The payoff of reusing :Entity for sources: watch first, follow later, one node."""
    await graph.upsert_video(_meta())
    await graph.follow(SourceRef(name="veritasium", kind="channel"))
    rows = await graph._run(
        "MATCH (e:Entity {canonical_name:'veritasium'}) RETURN count(e) AS n, labels(e) AS l"
    )
    assert rows[0]["n"] == 1
    assert "Source" in rows[0]["l"]


async def test_video_upsert_is_idempotent(graph: Neo4jGraph):
    for _ in range(3):
        await graph.upsert_video(_meta())
    rows = await graph._run("MATCH (v:Video) RETURN count(v) AS n")
    assert rows[0]["n"] == 1


async def test_notes_are_stored_with_timestamps(graph: Neo4jGraph):
    await graph.upsert_video(_meta())
    await graph.replace_notes("dQw4w9WgXcQ", [_note("cannot measure one-way light", 134)])

    (note,) = await graph.notes_for_video("dQw4w9WgXcQ")
    assert note.t_seconds == 134
    assert note.deep_link == "https://youtu.be/dQw4w9WgXcQ?t=134"


async def test_redistilling_replaces_notes_rather_than_duplicating(graph: Neo4jGraph):
    """The whole point of caching the transcript is to re-run distillation later."""
    await graph.upsert_video(_meta())
    await graph.replace_notes("dQw4w9WgXcQ", [_note("old claim", 10)])
    await graph.replace_notes("dQw4w9WgXcQ", [_note("better claim", 10), _note("and another", 50)])

    notes = await graph.notes_for_video("dQw4w9WgXcQ")
    assert [n.text for n in notes] == ["better claim", "and another"]


async def test_note_with_no_entities_is_still_stored(graph: Neo4jGraph):
    await graph.upsert_video(_meta())
    await graph.replace_notes(
        "dQw4w9WgXcQ", [VideoNote(text="a bare claim", t_seconds=5, entities=[])]
    )
    assert len(await graph.notes_for_video("dQw4w9WgXcQ")) == 1


async def test_note_and_thought_share_an_entity_node(graph: Neo4jGraph):
    """The reason notes go in the graph at all: a video claim and your own thought
    meet on the same entity."""
    await graph.upsert_video(_meta())
    await graph.replace_notes("dQw4w9WgXcQ", [_note("relativity claim", 10, entity="Einstein")])
    await graph.upsert_thought(
        _record(text="thinking about Einstein"),
        Classification(
            category="StudyResearch",
            title="t",
            summary="s",
            entities=[ExtractedEntity(name="einstein", type="person", salience=0.9)],
        ),
    )
    rows = await graph._run(
        """
        MATCH (e:Entity {canonical_name:'einstein'})
        RETURN count{ (:Note)-[:MENTIONS]->(e) } AS notes,
               count{ (:Thought)-[:MENTIONS]->(e) } AS thoughts
        """
    )
    assert rows[0] == {"notes": 1, "thoughts": 1}


async def test_search_notes(graph: Neo4jGraph):
    await graph.upsert_video(_meta())
    await graph.replace_notes(
        "dQw4w9WgXcQ",
        [_note("light cannot be measured one way", 10), _note("clocks drift apart", 20)],
    )
    found = await graph.search_notes("clocks")
    assert [n.text for n in found] == ["clocks drift apart"]


async def test_unprocessed_video_thoughts_finds_work_to_requeue(graph: Neo4jGraph):
    """How a restart recovers queued jobs without a durable queue."""
    unprocessed = _record(1, text="https://youtu.be/dQw4w9WgXcQ")
    processed = _record(2, text="https://youtu.be/aaaaaaaaaaa", minutes=1)
    await graph.upsert_thought(unprocessed, _classification("VideoKnowledge"))
    await graph.upsert_thought(processed, _classification("VideoKnowledge"))
    await graph.upsert_video(_meta("aaaaaaaaaaa"), processed.id)

    pending = await graph.unprocessed_video_thoughts()
    assert [p.id for p in pending] == [unprocessed.id]


async def test_non_video_categories_are_not_queued_for_video_work(graph: Neo4jGraph):
    await graph.upsert_thought(_record(text="oat milk"), _classification("Shopping"))
    assert await graph.unprocessed_video_thoughts() == []


async def test_permanently_failed_video_is_not_requeued(graph: Neo4jGraph):
    """A video with captions disabled fails identically every time.

    Without this the failure is indistinguishable from work never attempted, so the
    thought is re-queued on every single restart for the life of the graph.
    """
    dead = _record(text="https://youtu.be/dQw4w9WgXcQ")
    await graph.upsert_thought(dead, _classification("VideoKnowledge"))
    await graph.mark_video_failed(dead.id, "captions are disabled", True)

    assert await graph.unprocessed_video_thoughts() == []


async def test_transient_failure_stays_in_the_requeue_set(graph: Neo4jGraph):
    """The whole point of distinguishing the two — this one is worth retrying."""
    blocked = _record(text="https://youtu.be/dQw4w9WgXcQ")
    await graph.upsert_thought(blocked, _classification("VideoKnowledge"))
    await graph.mark_video_failed(blocked.id, "YouTube is blocking", False)

    pending = await graph.unprocessed_video_thoughts()
    assert [p.id for p in pending] == [blocked.id]


async def test_a_later_success_clears_the_failure_marker(graph: Neo4jGraph):
    """Captions can be enabled after the fact; the block must not be permanent."""
    thought = _record(text="https://youtu.be/dQw4w9WgXcQ")
    await graph.upsert_thought(thought, _classification("VideoKnowledge"))
    await graph.mark_video_failed(thought.id, "captions are disabled", True)
    await graph.upsert_video(_meta(), thought.id)

    rows = await graph._run(
        "MATCH (t:Thought {id: $id}) RETURN t.video_error_permanent AS p, t.video_error AS e",
        id=str(thought.id),
    )
    assert rows[0] == {"p": None, "e": None}


async def test_note_score_and_section_survive_the_round_trip(graph: Neo4jGraph):
    """`/more` ranks and groups from the graph, so both must be stored, not just shown."""
    await graph.upsert_video(_meta())
    await graph.replace_notes(
        "dQw4w9WgXcQ",
        [
            VideoNote(text="a strong claim", t_seconds=10, section="Setup", score=0.9),
            VideoNote(text="a weaker claim", t_seconds=20, section="Pricing", score=0.2),
        ],
    )
    notes = await graph.notes_for_video("dQw4w9WgXcQ")
    assert [(n.section, n.score) for n in notes] == [("Setup", 0.9), ("Pricing", 0.2)]


async def test_notes_written_before_scoring_read_back_mid_pack(graph: Neo4jGraph):
    """0.5 rather than 0.0 — unscored is unknown, not worthless."""
    await graph.upsert_video(_meta())
    await graph.replace_notes("dQw4w9WgXcQ", [_note("an older claim", 10)])
    await graph._run("MATCH (n:Note) REMOVE n.score, n.section")

    (note,) = await graph.notes_for_video("dQw4w9WgXcQ")
    assert note.score == 0.5
    assert note.section is None


async def test_caption_provenance_is_stored_on_the_video(graph: Neo4jGraph):
    await graph.upsert_video(_meta(), is_generated=True)
    rows = await graph._run("MATCH (v:Video) RETURN v.captions_auto AS auto")
    assert rows[0]["auto"] is True


async def test_unknown_caption_provenance_stays_null_not_false(graph: Neo4jGraph):
    """Artifacts cached before the flag existed must not claim human subtitles."""
    await graph.upsert_video(_meta())
    rows = await graph._run("MATCH (v:Video) RETURN v.captions_auto AS auto")
    assert rows[0]["auto"] is None


async def test_wipe_clears_everything(graph: Neo4jGraph):
    await graph.upsert_thought(_record(), _classification())
    await graph.follow(SourceRef(name="Naval", kind="person"))
    await graph.wipe()
    assert (await graph.stats()).total == 0
    assert await graph.following() == []


# --- x pulse ------------------------------------------------------------------ #


def _digest(*posts: PulsePost, topic: str = "AI", at=None) -> PulseDigest:
    from datetime import UTC, datetime

    return PulseDigest(
        topic=topic,
        summary="A summary.",
        posts=list(posts),
        asked_at=at or datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
    )


def _post(text: str = "a claim", handle: str = "@karpathy", n: int = 1, **kw):
    return PulsePost(
        text=text,
        author_handle=handle,
        url=f"https://x.com/{handle.lstrip('@')}/status/{n}",
        **kw,
    )


@pytest.mark.integration
async def test_pulse_and_posts_round_trip(graph):
    digest = _digest(_post(section="Releases", score=0.9))
    count = await graph.save_pulse(digest)
    assert count == 1

    posts = await graph.posts_for_pulse(pulse_id(digest.topic, digest.asked_at))
    assert [p.text for p in posts] == ["a claim"]
    assert posts[0].section == "Releases"
    assert posts[0].score == 0.9
    assert posts[0].topic == "AI"


@pytest.mark.integration
async def test_the_same_post_in_two_digests_stays_one_node(graph):
    """Otherwise a story running for a week appears seven times in /more."""
    from datetime import UTC, datetime

    shared = _post(n=42)
    await graph.save_pulse(_digest(shared, at=datetime(2026, 8, 9, tzinfo=UTC)))
    await graph.save_pulse(_digest(shared, at=datetime(2026, 8, 10, tzinfo=UTC)))

    rows = await graph._run("MATCH (n:Post) RETURN count(n) AS c")
    assert rows[0]["c"] == 1


@pytest.mark.integration
async def test_posts_link_to_a_followed_source(graph):
    await graph.follow(SourceRef(name="@karpathy", kind="x"))
    await graph.save_pulse(_digest(_post(handle="@karpathy")))

    rows = await graph._run(
        "MATCH (:Post)-[:BY]->(s:Entity:Source) RETURN s.name AS name"
    )
    assert [r["name"] for r in rows] == ["@karpathy"]


@pytest.mark.integration
async def test_posts_from_unfollowed_accounts_have_no_source_edge(graph):
    await graph.save_pulse(_digest(_post(handle="@stranger")))
    rows = await graph._run("MATCH (:Post)-[:BY]->() RETURN count(*) AS c")
    assert rows[0]["c"] == 0


@pytest.mark.integration
async def test_search_posts_finds_a_post(graph):
    await graph.save_pulse(_digest(_post(text="transformers got faster")))
    found = await graph.search_posts("transformers")
    assert [p.text for p in found] == ["transformers got faster"]


@pytest.mark.integration
async def test_search_notes_does_not_return_posts(graph):
    """The whole reason posts got their own label — see the spec's correction 2."""
    await graph.save_pulse(_digest(_post(text="transformers got faster")))
    assert await graph.search_notes("transformers") == []


@pytest.mark.integration
async def test_latest_pulse_is_the_most_recent(graph):
    from datetime import UTC, datetime

    old = datetime(2026, 8, 1, tzinfo=UTC)
    new = datetime(2026, 8, 9, tzinfo=UTC)
    await graph.save_pulse(_digest(_post(n=1), at=old))
    await graph.save_pulse(_digest(_post(n=2), at=new))

    latest = await graph.latest_pulse()
    assert latest is not None
    assert latest[0] == pulse_id("AI", new)


@pytest.mark.integration
async def test_latest_pulse_is_none_on_an_empty_graph(graph):
    assert await graph.latest_pulse() is None


@pytest.mark.integration
async def test_saving_a_digest_with_no_posts_is_fine(graph):
    """A quiet day still records that we asked."""
    assert await graph.save_pulse(_digest()) == 0


# --- category seeding and shopping recovery -------------------------------- #


async def test_every_category_exists_after_ensure_schema(graph: Neo4jGraph):
    """The vocabulary is fixed and known up front, so an empty category is a real
    thing containing nothing — not a category that does not exist. Before seeding,
    a browser looking at the graph saw only whatever the last few weeks contained.
    """
    rows = await graph._run("MATCH (c:Category) RETURN c.name AS name")
    assert {r["name"] for r in rows} == set(CATEGORIES)


async def test_seeding_categories_is_idempotent(graph: Neo4jGraph):
    await graph.ensure_schema()
    await graph.ensure_schema()

    rows = await graph._run("MATCH (c:Category) RETURN count(c) AS n")
    assert rows[0]["n"] == len(CATEGORIES)


async def test_a_rebuild_reseeds_the_categories(graph: Neo4jGraph):
    """`--rebuild` wipes then ensures, so the vocabulary must come back with it."""
    await graph.wipe()
    await graph.ensure_schema()

    rows = await graph._run("MATCH (c:Category) RETURN count(c) AS n")
    assert rows[0]["n"] == len(CATEGORIES)


async def test_seeding_does_not_disturb_a_category_in_use(graph: Neo4jGraph):
    record = _record(text="buy oat milk")
    await graph.upsert_thought(record, _classification("Shopping"))
    await graph.ensure_schema()

    views = await graph.by_category("Shopping")
    assert [v.id for v in views] == [record.id]


async def test_category_thought_ids_returns_live_thoughts_oldest_first(graph: Neo4jGraph):
    first = _record(1, text="buy bananas", minutes=0)
    second = _record(2, text="buy bread", minutes=5)
    for record in (first, second):
        await graph.upsert_thought(record, _classification("Shopping"))

    assert await graph.category_thought_ids("Shopping") == [first.id, second.id]


async def test_category_thought_ids_excludes_deleted(graph: Neo4jGraph):
    record = _record(text="buy bananas")
    await graph.upsert_thought(record, _classification("Shopping"))
    await graph.soft_delete(record.id)

    assert await graph.category_thought_ids("Shopping") == []


async def test_category_thought_ids_is_empty_for_an_unused_category(graph: Neo4jGraph):
    """Seeded but empty. The query must return nothing rather than fail to match."""
    assert await graph.category_thought_ids("Shopping") == []
