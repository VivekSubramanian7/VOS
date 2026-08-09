"""The Neo4j projection.

Derived from the journal and therefore disposable: a corrupt volume is an
inconvenience, not an incident (`vos reclassify --rebuild`).

Every write is `MERGE`-based and idempotent, which is what makes both retry after a
crash and full replay safe. Re-projecting the same record twice must produce exactly
the same graph as projecting it once — the tests assert this directly.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from neo4j import AsyncDriver, AsyncGraphDatabase

from vos.contracts import (
    CaptureRecord,
    Classification,
    GraphStats,
    NoteView,
    PostView,
    PulseDigest,
    SourceRef,
    Status,
    ThoughtView,
    VideoMeta,
    VideoNote,
    canonical,
    note_id,
    post_id,
    pulse_id,
)

log = logging.getLogger(__name__)

# Uniqueness constraints are correctness infrastructure, not tuning: they are what
# make MERGE idempotent under retry.
SCHEMA: tuple[str, ...] = (
    "CREATE CONSTRAINT thought_id IF NOT EXISTS "
    "FOR (t:Thought) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT category_name IF NOT EXISTS "
    "FOR (c:Category) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT entity_canonical IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.canonical_name IS UNIQUE",
    "CREATE INDEX thought_created IF NOT EXISTS FOR (t:Thought) ON (t.created_at)",
    "CREATE INDEX thought_status IF NOT EXISTS FOR (t:Thought) ON (t.status)",
    "CREATE FULLTEXT INDEX thought_text IF NOT EXISTS "
    "FOR (t:Thought) ON EACH [t.text, t.title, t.summary]",
    "CREATE CONSTRAINT video_id IF NOT EXISTS FOR (v:Video) REQUIRE v.id IS UNIQUE",
    "CREATE CONSTRAINT note_id IF NOT EXISTS FOR (n:Note) REQUIRE n.id IS UNIQUE",
    "CREATE FULLTEXT INDEX note_text IF NOT EXISTS FOR (n:Note) ON EACH [n.text]",
    "CREATE CONSTRAINT pulse_id IF NOT EXISTS FOR (p:Pulse) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT post_id IF NOT EXISTS FOR (p:Post) REQUIRE p.id IS UNIQUE",
    "CREATE FULLTEXT INDEX post_text IF NOT EXISTS FOR (p:Post) ON EACH [p.text]",
)


def _native(value: Any) -> Any:
    """Neo4j temporal types -> stdlib datetime."""
    return value.to_native() if hasattr(value, "to_native") else value


def _to_view(row: dict[str, Any]) -> ThoughtView:
    return ThoughtView(
        id=UUID(row["id"]),
        text=row["text"],
        title=row.get("title"),
        category=row.get("category"),
        status=row.get("status") or "captured",
        created_at=_native(row["created_at"]),
        entities=[e for e in (row.get("entities") or []) if e],
    )


def _to_note(row: dict[str, Any]) -> NoteView:
    return NoteView(
        id=UUID(row["id"]),
        text=row["text"],
        t_seconds=int(row["t"] or 0),
        video_id=row["video_id"],
        video_title=row.get("title") or row["video_id"],
        url=row.get("url") or "",
        section=row.get("section"),
        # Notes written before scoring existed have no value signal; 0.5 keeps them
        # mid-pack rather than pretending they were judged.
        score=float(row.get("score") if row.get("score") is not None else 0.5),
    )


def _to_post(row: dict[str, Any]) -> PostView:
    return PostView(
        id=UUID(row["id"]),
        text=row["text"],
        author_handle=row["handle"],
        url=row["url"],
        section=row.get("section"),
        score=float(row.get("score") if row.get("score") is not None else 0.5),
        topic=row.get("topic") or "",
        asked_at=_native(row["asked_at"]),
    )


class Neo4jGraph:
    """Implementation of the `GraphStore` protocol."""

    def __init__(self, driver: AsyncDriver, database: str = "neo4j") -> None:
        self._driver = driver
        self._db = database

    @classmethod
    def connect(cls, uri: str, user: str, password: str, database: str = "neo4j") -> Neo4jGraph:
        return cls(
            AsyncGraphDatabase.driver(
                uri,
                auth=(user, password),
                # A young graph legitimately lacks most labels and relationship types:
                # querying for :Source before anything is followed is normal, not a
                # defect. Neo4j reports each as an UNRECOGNIZED-classification warning,
                # which buries real output. Suppress that class only — genuine
                # performance and deprecation warnings still come through.
                notifications_disabled_classifications=["UNRECOGNIZED"],
            ),
            database,
        )

    async def close(self) -> None:
        await self._driver.close()

    async def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = await self._driver.execute_query(query, database_=self._db, **params)
        return [r.data() for r in records]

    # -- schema --------------------------------------------------------- #

    async def ensure_schema(self) -> None:
        """Idempotent; safe to run on every startup."""
        for statement in SCHEMA:
            await self._run(statement)

    # -- writes --------------------------------------------------------- #

    async def upsert_thought(
        self, record: CaptureRecord, classification: Classification | None
    ) -> list[str]:
        """Project a captured thought. Returns the names of any *followed* sources
        the thought turned out to mention.

        Enrichment edges (category, mentions, thread position) are deleted and
        recreated rather than added to, so re-running after a prompt change replaces
        the old classification instead of accumulating alongside it.
        """
        status: Status = "classified" if classification else "captured"

        await self._run(
            """
            MERGE (t:Thought {id: $id})
            SET t.text       = $text,
                t.source     = $source,
                t.transcript = $transcript,
                t.created_at = $created_at,
                t.status     = CASE WHEN t.status = 'deleted' THEN 'deleted' ELSE $status END
            """,
            id=str(record.id),
            text=record.text,
            source=record.source,
            transcript=record.transcript,
            created_at=record.captured_at,
            status=status,
        )

        if classification is None:
            return []

        # Replace, never accumulate.
        await self._run(
            """
            MATCH (t:Thought {id: $id})
            SET t.title = $title, t.summary = $summary, t.confidence = $confidence,
                t.error = NULL
            WITH t
            OPTIONAL MATCH (t)-[r:IN_CATEGORY|MENTIONS]->()
            DELETE r
            WITH DISTINCT t
            OPTIONAL MATCH (t)-[n:NEXT]-()
            DELETE n
            """,
            id=str(record.id),
            title=classification.title,
            summary=classification.summary,
            confidence=classification.confidence,
        )

        await self._run(
            """
            MATCH (t:Thought {id: $id})
            MERGE (c:Category {name: $category})
            MERGE (t)-[:IN_CATEGORY]->(c)
            """,
            id=str(record.id),
            category=classification.category,
        )

        if classification.entities:
            await self._run(
                """
                MATCH (t:Thought {id: $id})
                UNWIND $entities AS e
                MERGE (n:Entity {canonical_name: e.canonical_name})
                  ON CREATE SET n.name = e.name, n.type = e.type
                MERGE (t)-[m:MENTIONS]->(n)
                  SET m.salience = e.salience
                """,
                id=str(record.id),
                entities=[
                    {
                        "canonical_name": canonical(e.name),
                        "name": e.name,
                        "type": e.type,
                        "salience": e.salience,
                    }
                    for e in classification.entities
                ],
            )

        await self._link_thread(record.id, classification.category)
        return await self._linked_sources(record.id)

    async def _link_thread(self, thought_id: UUID, category: str) -> None:
        """Maintain :NEXT within a category.

        Written to be correct under out-of-order insertion, because replay does not
        necessarily follow wall-clock order: it links to both the nearest earlier and
        the nearest later thought.
        """
        await self._run(
            """
            MATCH (t:Thought {id: $id})-[:IN_CATEGORY]->(c:Category {name: $category})
            OPTIONAL MATCH (prev:Thought)-[:IN_CATEGORY]->(c)
              WHERE prev.created_at < t.created_at AND prev.status <> 'deleted'
            WITH t, prev ORDER BY prev.created_at DESC LIMIT 1
            FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END |
                MERGE (prev)-[:NEXT]->(t))
            """,
            id=str(thought_id),
            category=category,
        )
        await self._run(
            """
            MATCH (t:Thought {id: $id})-[:IN_CATEGORY]->(c:Category {name: $category})
            OPTIONAL MATCH (nxt:Thought)-[:IN_CATEGORY]->(c)
              WHERE nxt.created_at > t.created_at AND nxt.status <> 'deleted'
            WITH t, nxt ORDER BY nxt.created_at ASC LIMIT 1
            FOREACH (_ IN CASE WHEN nxt IS NULL THEN [] ELSE [1] END |
                MERGE (t)-[:NEXT]->(nxt))
            """,
            id=str(thought_id),
            category=category,
        )

    async def _linked_sources(self, thought_id: UUID) -> list[str]:
        rows = await self._run(
            """
            MATCH (t:Thought {id: $id})-[:MENTIONS]->(s:Entity:Source)
            RETURN s.name AS name ORDER BY name
            """,
            id=str(thought_id),
        )
        return [r["name"] for r in rows]

    async def mark_unclassified(self, id: UUID, error: str) -> None:
        """Enrichment failed. The thought is already safe in the journal and present
        here; it is flagged for /pending, never dropped."""
        await self._run(
            """
            MATCH (t:Thought {id: $id})
            SET t.status = CASE WHEN t.status = 'deleted' THEN 'deleted'
                                ELSE 'unclassified' END,
                t.error = $error
            """,
            id=str(id),
            error=error[:500],
        )

    async def soft_delete(self, id: UUID) -> None:
        await self._run(
            "MATCH (t:Thought {id: $id}) SET t.status = 'deleted'", id=str(id)
        )

    # -- follows -------------------------------------------------------- #

    async def follow(self, source: SourceRef) -> None:
        """Add the :Source label to an ordinary :Entity (ADR-009).

        If the entity already exists because a thought mentioned it, the follow lands
        on that same node — declaration and mention converge with no extra edge type.
        """
        await self._run(
            """
            MERGE (e:Entity {canonical_name: $canonical})
              ON CREATE SET e.name = $name, e.type = $type
            SET e:Source,
                e.kind = $kind, e.url = $url, e.author = $author, e.note = $note,
                e.followed_at = datetime()
            """,
            canonical=source.canonical_name,
            name=source.name,
            type="person" if source.kind == "person" else "topic",
            kind=source.kind,
            url=source.url,
            author=source.author,
            note=source.note,
        )

    async def unfollow(self, name: str) -> bool:
        """Drop the :Source label but keep the :Entity — thoughts still reference it."""
        rows = await self._run(
            """
            MATCH (e:Entity:Source {canonical_name: $canonical})
            REMOVE e:Source
            REMOVE e.kind, e.followed_at, e.url, e.author, e.note
            RETURN e.name AS name
            """,
            canonical=canonical(name),
        )
        return bool(rows)

    async def following(self) -> list[SourceRef]:
        rows = await self._run(
            """
            MATCH (s:Entity:Source)
            RETURN s.name AS name, s.kind AS kind, s.url AS url,
                   s.author AS author, s.note AS note
            ORDER BY s.kind, s.name
            """
        )
        return [SourceRef(**r) for r in rows]

    # -- reads ---------------------------------------------------------- #

    _VIEW = """
        OPTIONAL MATCH (t)-[:IN_CATEGORY]->(c:Category)
        OPTIONAL MATCH (t)-[:MENTIONS]->(e:Entity)
        RETURN t.id AS id, t.text AS text, t.title AS title,
               c.name AS category, t.status AS status,
               t.created_at AS created_at, collect(DISTINCT e.name) AS entities
    """

    async def recent(self, n: int = 10) -> list[ThoughtView]:
        rows = await self._run(
            f"""
            MATCH (t:Thought) WHERE t.status <> 'deleted'
            WITH t ORDER BY t.created_at DESC LIMIT $n
            {self._VIEW}
            ORDER BY created_at DESC
            """,
            n=n,
        )
        return [_to_view(r) for r in rows]

    async def by_category(self, category: str, n: int = 10) -> list[ThoughtView]:
        rows = await self._run(
            f"""
            MATCH (t:Thought)-[:IN_CATEGORY]->(:Category {{name: $category}})
            WHERE t.status <> 'deleted'
            WITH t ORDER BY t.created_at DESC LIMIT $n
            {self._VIEW}
            ORDER BY created_at DESC
            """,
            category=category,
            n=n,
        )
        return [_to_view(r) for r in rows]

    async def search(self, term: str, n: int = 10) -> list[ThoughtView]:
        rows = await self._run(
            f"""
            CALL db.index.fulltext.queryNodes('thought_text', $term) YIELD node AS t
            WHERE t.status <> 'deleted'
            WITH t LIMIT $n
            {self._VIEW}
            ORDER BY created_at DESC
            """,
            term=term,
            n=n,
        )
        return [_to_view(r) for r in rows]

    async def pending(self) -> list[ThoughtView]:
        """Thoughts that are safe but not yet filed.

        Covers both failure ('unclassified') and never-attempted ('captured') — the
        latter is how a thought recovered by startup re-projection surfaces. Normal
        captures pass through 'captured' for a second or two, which is harmless.
        """
        rows = await self._run(
            f"""
            MATCH (t:Thought) WHERE t.status IN ['unclassified', 'captured']
            WITH t ORDER BY t.created_at ASC
            {self._VIEW}
            ORDER BY created_at ASC
            """
        )
        return [_to_view(r) for r in rows]

    async def last_thought_id(self) -> UUID | None:
        rows = await self._run(
            """
            MATCH (t:Thought) WHERE t.status <> 'deleted'
            RETURN t.id AS id ORDER BY t.created_at DESC LIMIT 1
            """
        )
        return UUID(rows[0]["id"]) if rows else None

    async def categories_by_id(self) -> dict[UUID, str]:
        """Current category assignments — the baseline `reclassify --dry-run` diffs
        against, so a prompt or model change can be inspected before it is applied."""
        rows = await self._run(
            """
            MATCH (t:Thought)-[:IN_CATEGORY]->(c:Category)
            RETURN t.id AS id, c.name AS category
            """
        )
        return {UUID(r["id"]): r["category"] for r in rows}

    async def existing_ids(self) -> set[UUID]:
        """Used on startup to find journal entries the graph is missing — the crash
        window between the journal write and the projection write (§9.2)."""
        rows = await self._run("MATCH (t:Thought) RETURN t.id AS id")
        return {UUID(r["id"]) for r in rows}

    async def stats(self) -> GraphStats:
        totals = await self._run(
            "MATCH (t:Thought) WHERE t.status <> 'deleted' RETURN count(t) AS total"
        )
        by_cat = await self._run(
            """
            MATCH (t:Thought)-[:IN_CATEGORY]->(c:Category)
            WHERE t.status <> 'deleted'
            RETURN c.name AS name, count(t) AS n ORDER BY n DESC
            """
        )
        pending = await self._run(
            "MATCH (t:Thought) WHERE t.status = 'unclassified' RETURN count(t) AS n"
        )
        sources = await self._run(
            """
            MATCH (t:Thought)-[:MENTIONS]->(s:Entity:Source)
            WHERE t.status <> 'deleted'
            RETURN s.name AS name, count(t) AS n ORDER BY n DESC, name LIMIT 5
            """
        )
        return GraphStats(
            total=totals[0]["total"] if totals else 0,
            by_category={r["name"]: r["n"] for r in by_cat},
            pending=pending[0]["n"] if pending else 0,
            top_sources=[(r["name"], r["n"]) for r in sources],
        )

    # -- video → knowledge ---------------------------------------------- #

    async def upsert_video(
        self,
        meta: VideoMeta,
        thought_id: UUID | None = None,
        *,
        is_generated: bool | None = None,
    ) -> None:
        """Create/update the :Video and attach it to its channel and source thought.

        The channel reuses `:Entity` so that a `/follow channel …` before or after
        watching converges on the same node (ADR-009).

        `captions_auto` records transcript provenance on the :Video rather than on each
        :Note. Notes reach it through `:FROM`, so storing it per note would be redundant
        and could drift after a re-distillation.
        """
        await self._run(
            """
            MERGE (v:Video {id: $id})
            SET v.url = $url, v.title = $title, v.channel = $channel,
                v.approx_duration_s = $duration, v.fetched_at = datetime(),
                v.captions_auto = $captions_auto
            MERGE (e:Entity {canonical_name: $channel_canonical})
              ON CREATE SET e.name = $channel, e.type = 'org'
            MERGE (v)-[:PUBLISHED_BY]->(e)
            """,
            id=meta.video_id,
            url=meta.url,
            title=meta.title,
            channel=meta.channel,
            channel_canonical=canonical(meta.channel),
            duration=meta.approx_duration_s,
            captions_auto=is_generated,
        )
        if thought_id is not None:
            # Success clears any earlier failure marker, so a video that was once
            # permanently dead (captions later enabled, say) stops being blocked.
            await self._run(
                """
                MATCH (t:Thought {id: $tid}), (v:Video {id: $vid})
                MERGE (t)-[:ABOUT]->(v)
                REMOVE t.video_error, t.video_error_permanent
                """,
                tid=str(thought_id),
                vid=meta.video_id,
            )

    async def replace_notes(self, video_id: str, notes: list[VideoNote]) -> int:
        """Replace this video's notes wholesale.

        Replace rather than merge, for the same reason reclassification replaces: a
        better prompt should supersede the old output, not silt up alongside it.
        """
        await self._run(
            """
            MATCH (n:Note)-[:FROM]->(:Video {id: $vid})
            DETACH DELETE n
            """,
            vid=video_id,
        )
        if not notes:
            return 0

        payload = [
            {
                "id": str(note_id(video_id, n.t_seconds, n.text)),
                "text": n.text,
                "t_seconds": n.t_seconds,
                "section": n.section,
                "score": n.score,
            }
            for n in notes
        ]
        await self._run(
            """
            MATCH (v:Video {id: $vid})
            UNWIND $notes AS note
            MERGE (n:Note {id: note.id})
              SET n.text = note.text, n.t_seconds = note.t_seconds,
                  n.section = note.section, n.score = note.score
            MERGE (n)-[:FROM]->(v)
            """,
            vid=video_id,
            notes=payload,
        )

        # Separate statement rather than an UNWIND nested under the one above: a note
        # with no entities must still be created, and flattening the pairs here makes
        # that obviously true instead of depending on Cypher row-stream subtleties.
        pairs = [
            {
                "note_id": str(note_id(video_id, n.t_seconds, n.text)),
                "canonical_name": canonical(e.name),
                "name": e.name,
                "type": e.type,
            }
            for n in notes
            for e in n.entities
            if e.name.strip()
        ]
        if pairs:
            await self._run(
                """
                UNWIND $pairs AS p
                MATCH (n:Note {id: p.note_id})
                MERGE (x:Entity {canonical_name: p.canonical_name})
                  ON CREATE SET x.name = p.name, x.type = p.type
                MERGE (n)-[:MENTIONS]->(x)
                """,
                pairs=pairs,
            )
        return len(notes)

    async def notes_for_video(self, video_id: str) -> list[NoteView]:
        rows = await self._run(
            """
            MATCH (n:Note)-[:FROM]->(v:Video {id: $vid})
            RETURN n.id AS id, n.text AS text, n.t_seconds AS t,
                   n.section AS section, n.score AS score,
                   v.id AS video_id, v.title AS title, v.url AS url
            ORDER BY n.t_seconds
            """,
            vid=video_id,
        )
        return [_to_note(r) for r in rows]

    async def search_notes(self, term: str, n: int = 10) -> list[NoteView]:
        rows = await self._run(
            """
            CALL db.index.fulltext.queryNodes('note_text', $term) YIELD node AS note
            MATCH (note)-[:FROM]->(v:Video)
            RETURN note.id AS id, note.text AS text, note.t_seconds AS t,
                   note.section AS section, note.score AS score,
                   v.id AS video_id, v.title AS title, v.url AS url
            LIMIT $n
            """,
            term=term,
            n=n,
        )
        return [_to_note(r) for r in rows]

    # -- pulse ---------------------------------------------------------- #

    async def save_pulse(self, digest: PulseDigest) -> int:
        """Project one digest. Returns how many posts were written.

        MERGE rather than replace, unlike `replace_notes`. Re-distilling a video
        supersedes that video's own earlier output, so replacing is right there. A
        post recurring across digests is the *same post* — it keeps one identity and
        gains a second `HAS_POST` edge.
        """
        pid = str(pulse_id(digest.topic, digest.asked_at))
        await self._run(
            """
            MERGE (p:Pulse {id: $pid})
            SET p.topic = $topic, p.summary = $summary,
                p.asked_at = datetime($asked_at)
            """,
            pid=pid,
            topic=digest.topic,
            summary=digest.summary,
            asked_at=digest.asked_at.isoformat(),
        )
        if not digest.posts:
            return 0

        payload = [
            {
                "id": str(post_id(p.url)),
                "text": p.text,
                "url": p.url,
                "handle": p.author_handle,
                "section": p.section,
                "score": p.score,
            }
            for p in digest.posts
        ]
        await self._run(
            """
            MATCH (p:Pulse {id: $pid})
            UNWIND $posts AS post
            MERGE (n:Post {id: post.id})
              ON CREATE SET n.first_seen = datetime()
            SET n.text = post.text, n.url = post.url,
                n.author_handle = post.handle,
                n.section = post.section, n.score = post.score
            MERGE (p)-[:HAS_POST]->(n)
            WITH n, post
            OPTIONAL MATCH (s:Entity:Source {canonical_name: post.handle})
            FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
                MERGE (n)-[:BY]->(s))
            """,
            pid=pid,
            posts=payload,
        )
        return len(payload)

    _POST_VIEW = """
        RETURN n.id AS id, n.text AS text, n.url AS url,
               n.author_handle AS handle, n.section AS section, n.score AS score,
               p.topic AS topic, p.asked_at AS asked_at
    """

    async def posts_for_pulse(self, pulse: UUID) -> list[PostView]:
        rows = await self._run(
            f"""
            MATCH (p:Pulse {{id: $pid}})-[:HAS_POST]->(n:Post)
            {self._POST_VIEW}
            ORDER BY score DESC
            """,
            pid=str(pulse),
        )
        return [_to_post(r) for r in rows]

    async def search_posts(self, term: str, n: int = 10) -> list[PostView]:
        rows = await self._run(
            f"""
            CALL db.index.fulltext.queryNodes('post_text', $term) YIELD node AS n
            MATCH (p:Pulse)-[:HAS_POST]->(n)
            {self._POST_VIEW}
            LIMIT $n
            """,
            term=term,
            n=n,
        )
        return [_to_post(r) for r in rows]

    async def latest_pulse(self) -> tuple[UUID, datetime] | None:
        rows = await self._run(
            "MATCH (p:Pulse) RETURN p.id AS id, p.asked_at AS at "
            "ORDER BY p.asked_at DESC LIMIT 1"
        )
        return (UUID(rows[0]["id"]), _native(rows[0]["at"])) if rows else None

    async def latest_video(self) -> tuple[str, datetime] | None:
        """The newest video and when it was fetched, so `/more` can decide between a
        recent pulse and a recent video without holding session state."""
        rows = await self._run(
            "MATCH (v:Video) RETURN v.id AS id, v.fetched_at AS at "
            "ORDER BY v.fetched_at DESC LIMIT 1"
        )
        return (rows[0]["id"], _native(rows[0]["at"])) if rows else None

    async def mark_video_failed(self, thought_id: UUID, reason: str, permanent: bool) -> None:
        """Record why a video could not be processed.

        The marker lives on the :Thought rather than on a placeholder :Video, which
        keeps the "no :Video means unprocessed" invariant that `vos.jobs` relies on
        intact. Without it a permanent failure — captions disabled, video deleted —
        is indistinguishable from work that was never attempted, so every restart
        re-queues it forever.
        """
        await self._run(
            """
            MATCH (t:Thought {id: $id})
            SET t.video_error = $reason, t.video_error_permanent = $permanent
            """,
            id=str(thought_id),
            reason=reason[:500],
            permanent=permanent,
        )

    async def unprocessed_video_thoughts(self) -> list[ThoughtView]:
        """VideoKnowledge thoughts with no :Video attached.

        This is how queued-but-lost jobs are recovered after a restart: rather than
        making the queue durable, "needs processing" is derived from the graph.

        A thought whose video failed *permanently* is excluded — retrying it would
        fail identically every time. Transient failures stay in the set, which is the
        whole point of distinguishing the two.
        """
        rows = await self._run(
            f"""
            MATCH (t:Thought)-[:IN_CATEGORY]->(:Category {{name: 'VideoKnowledge'}})
            WHERE t.status <> 'deleted' AND NOT (t)-[:ABOUT]->(:Video)
              AND coalesce(t.video_error_permanent, false) = false
            WITH t ORDER BY t.created_at ASC
            {self._VIEW}
            ORDER BY created_at ASC
            """
        )
        return [_to_view(r) for r in rows]

    async def video_exists(self, video_id: str) -> bool:
        rows = await self._run(
            "MATCH (v:Video {id: $vid}) RETURN count(v) AS n", vid=video_id
        )
        return bool(rows and rows[0]["n"])

    async def all_video_ids(self) -> list[str]:
        rows = await self._run("MATCH (v:Video) RETURN v.id AS id ORDER BY v.fetched_at DESC")
        return [r["id"] for r in rows]

    async def wipe(self) -> None:
        """Destroy the projection. Safe because the journal is the source of truth —
        this is what makes `vos reclassify --rebuild` a normal operation rather than
        a recovery procedure."""
        await self._run("MATCH (n) DETACH DELETE n")


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
