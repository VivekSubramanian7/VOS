"""Contracts — the spine.

Every other module talks through these types and nothing else. This module imports
nothing from the rest of the package, which is what keeps the dependency graph acyclic
and makes each component independently testable.

Seam protocols (Transcriber, Journal, GraphStore) are declared here even where Phase 1
has no implementation, so that adding one later is an adapter rather than a refactor.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid5

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

Category = Literal[
    "Shopping",
    "TripPlanning",
    "Family",
    "Career",
    "StudyResearch",
    "StockResearch",
    "VideoKnowledge",
    "Other",
]
"""Fixed set. `Other` is an honest fallback: a thought the classifier is not confident
about is filed there and flagged, never forced into a wrong bucket. Volume in `Other`
is the signal for which category to add next."""

CATEGORIES: tuple[str, ...] = (
    "Shopping",
    "TripPlanning",
    "Family",
    "Career",
    "StudyResearch",
    "StockResearch",
    "VideoKnowledge",
    "Other",
)

EntityType = Literal["person", "place", "product", "org", "ticker", "topic", "url"]
SourceKind = Literal["person", "book", "channel", "x"]
InputSource = Literal["text", "voice"]

Status = Literal["captured", "classified", "unclassified", "deleted"]
"""captured    — in the journal and the graph, not yet classified
classified  — enrichment succeeded
unclassified— enrichment failed; retryable via /pending. Never dropped.
deleted     — soft-deleted by /undo; the journal line is never edited"""


# Fixed namespace for deterministic thought IDs. Changing this value would orphan
# every existing thought, so it is a constant, not configuration.
NAMESPACE_VOS = UUID("1e5b3a7c-0d4f-5c8e-9a2b-7f6d3c1e8a04")


def thought_id(chat_id: int, message_id: int) -> UUID:
    """Deterministic ID for a captured message.

    Telegram guarantees at-least-once delivery: the same update can arrive twice, and
    the process can crash between the journal write and the graph write. Deriving the
    ID from (chat_id, message_id) rather than generating a random one means a retry
    produces the *same* ID, so `MERGE` under a uniqueness constraint collapses it.

    This function is the whole reason at-least-once delivery yields exactly-once capture.
    """
    return uuid5(NAMESPACE_VOS, f"{chat_id}:{message_id}")


# --------------------------------------------------------------------------- #
# Journal record — the source of truth
# --------------------------------------------------------------------------- #


class CaptureRecord(BaseModel):
    """One captured thought, exactly as it arrived.

    Immutable and append-only. Everything else in the system is derived from a stream
    of these, which is what makes the graph rebuildable and reclassification possible.
    """

    model_config = {"frozen": True}

    # Discriminator: the journal is a tagged union of capture, tombstone, and
    # item-mark lines.
    kind: Literal["capture"] = "capture"
    id: UUID
    chat_id: int
    message_id: int
    text: str
    source: InputSource = "text"
    transcript: str | None = None
    captured_at: datetime

    @classmethod
    def create(
        cls,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        captured_at: datetime,
        source: InputSource = "text",
        transcript: str | None = None,
    ) -> CaptureRecord:
        return cls(
            id=thought_id(chat_id, message_id),
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            source=source,
            transcript=transcript,
            captured_at=captured_at,
        )


class Tombstone(BaseModel):
    """Appended by /undo. The original capture line is never edited — that is what
    keeps the journal append-only and replay deterministic."""

    model_config = {"frozen": True}

    kind: Literal["tombstone"] = "tombstone"
    id: UUID
    deleted_at: datetime


class ItemMark(BaseModel):
    """Appended when a shopping item is ticked off, or un-ticked.

    Bought-ness is a decision the person made, not something a model derived, so it
    belongs in the journal rather than only in a projection. Without it `vos reclassify
    --rebuild` — a normal operation here, not a recovery procedure — would silently
    reset the whole list to pending. Same shape as `Tombstone`: the action is appended,
    never applied by editing an earlier line.
    """

    model_config = {"frozen": True}

    kind: Literal["item_mark"] = "item_mark"
    name: str
    action: Literal["bought", "unbought"] = "bought"
    at: datetime


JournalEntry = Annotated[
    CaptureRecord | Tombstone | ItemMark, Field(discriminator="kind")
]


# --------------------------------------------------------------------------- #
# Classification — the model's structured output
# --------------------------------------------------------------------------- #


class ExtractedEntity(BaseModel):
    name: str = Field(description="Entity as written, e.g. 'Japan' or 'NVDA'")
    type: EntityType = Field(description="What kind of thing this is")
    salience: float = Field(
        default=0.5, ge=0.0, le=1.0, description="How central to the thought, 0..1"
    )


class Classification(BaseModel):
    """The classifier's contract. Passed to `with_structured_output()`, so these field
    descriptions are part of the prompt — they are load-bearing, not documentation."""

    category: Category = Field(description="Best-fit category; use Other if unsure")
    title: str = Field(max_length=120, description="Short human-readable title")
    summary: str = Field(max_length=500, description="One or two sentences")
    entities: list[ExtractedEntity] = Field(
        default_factory=list, description="People, places, products, orgs, tickers, topics, URLs"
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Confidence in the category, 0..1"
    )


# --------------------------------------------------------------------------- #
# Followed sources
# --------------------------------------------------------------------------- #


class SourceRef(BaseModel):
    """A declared follow: a person, book, or channel that shapes your thinking.

    Stored as an ordinary :Entity carrying an extra :Source label, so a mention in a
    thought lands on the same node as the declaration (ADR-009).
    """

    name: str
    kind: SourceKind
    url: str | None = None
    author: str | None = None
    note: str | None = None

    @property
    def canonical_name(self) -> str:
        return canonical(self.name)


def canonical(name: str) -> str:
    """Phase 1 entity resolution: case- and whitespace-insensitive.

    Deliberately dumb. It is easy to replace once real use shows where it is wrong,
    and the journal means a better implementation can be applied retroactively.
    """
    return " ".join(name.split()).casefold()


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #


class ThoughtView(BaseModel):
    """A thought as read back out of the projection."""

    id: UUID
    text: str
    title: str | None = None
    category: str | None = None
    status: Status
    created_at: datetime
    entities: list[str] = Field(default_factory=list)


class GraphStats(BaseModel):
    total: int
    by_category: dict[str, int] = Field(default_factory=dict)
    pending: int = 0
    top_sources: list[tuple[str, int]] = Field(default_factory=list)


class CaptureResult(BaseModel):
    """What the user is told after a capture."""

    record: CaptureRecord
    classification: Classification | None = None
    linked_sources: list[str] = Field(default_factory=list)
    status: Status = "captured"
    error: str | None = None


# --------------------------------------------------------------------------- #
# Seam protocols
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Video → knowledge
# --------------------------------------------------------------------------- #

NAMESPACE_NOTE = UUID("3c7f1d2e-5a8b-4e6c-9f0a-2b4d6e8c1a35")


def note_id(video_id: str, t_seconds: int, text: str) -> UUID:
    """Deterministic, like `thought_id`.

    Re-distilling a video replaces its notes instead of duplicating them, and an
    unchanged note keeps its identity across re-runs so anything pointing at it survives.
    """
    return uuid5(NAMESPACE_NOTE, f"{video_id}:{t_seconds}:{text.strip()}")


class TranscriptSegment(BaseModel):
    text: str
    start: float
    duration: float


class VideoMeta(BaseModel):
    video_id: str
    url: str
    title: str
    channel: str
    approx_duration_s: int = 0


class VideoArtifact(BaseModel):
    """A fetched transcript, cached on disk.

    Unlike everything else derived in VOS, this is **not** recomputable: the upstream
    can be deleted, privated, or have captions removed. Treated as an input alongside
    the journal — backed up, never auto-pruned.
    """

    meta: VideoMeta
    segments: list[TranscriptSegment]
    fetched_at: datetime
    language: str = "en"
    is_generated: bool | None = None
    """Was this an ASR track rather than human-written subtitles?

    Three-valued on purpose. `None` means *unknown*, which is what artifacts cached
    before this field existed genuinely are — defaulting to `False` would assert
    "human subtitles" about videos nothing ever checked. It matters because ASR
    mangles exactly the proper nouns that become `:Entity` nodes, so a note derived
    from it deserves less trust than its confident phrasing suggests.
    """

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


class VideoNote(BaseModel):
    """One extracted claim, anchored to the second it was said."""

    text: str = Field(
        max_length=400,
        description="A single self-contained claim you could agree or disagree with",
    )
    t_seconds: int = Field(ge=0, description="Where in the video this was said")
    section: str | None = Field(
        default=None,
        max_length=40,
        description="A 2-4 word heading grouping related claims, reused across them",
    )
    score: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "How much this is worth remembering years from now. "
            "0.9-1.0: a specific number, mechanism, or falsifiable claim. "
            "0.5-0.7: useful but widely known. "
            "0.0-0.3: generic advice, promotion, or filler."
        ),
    )
    """Which claims survive the display cap.

    Anchors are spelled out in the field description because that text is part of the
    prompt: asked for an unqualified 0-1 score, a model rates almost everything highly,
    and a ranking where everything is 0.9 is not a ranking.
    """
    entities: list[ExtractedEntity] = Field(default_factory=list)


class VideoDistillation(BaseModel):
    summary: str = Field(max_length=600, description="Two or three sentences")
    notes: list[VideoNote] = Field(default_factory=list)


class NoteView(BaseModel):
    """A note as read back out of the projection."""

    id: UUID
    text: str
    t_seconds: int
    video_id: str
    video_title: str
    url: str
    section: str | None = None
    score: float = 0.5

    @property
    def deep_link(self) -> str:
        """The whole reason notes carry timestamps: tap through and verify."""
        return f"https://youtu.be/{self.video_id}?t={self.t_seconds}"


class VideoResult(BaseModel):
    """Outcome of processing one video — what the user is told."""

    video_id: str
    meta: VideoMeta | None = None
    distillation: VideoDistillation | None = None
    note_count: int = 0
    error: str | None = None
    truncated: bool = False
    is_generated: bool | None = None
    """Provenance of the transcript these notes came from — see
    `VideoArtifact.is_generated`. Carried through so the reply can flag ASR."""
    permanent: bool = True
    """Meaningful only alongside `error`: False means the cause was transient and the
    video is worth retrying. Drives whether the thought stays in the re-queue set."""

    @property
    def ok(self) -> bool:
        return self.error is None and self.distillation is not None


class VideoProcessingError(Exception):
    """Raised when a video cannot be fetched. Carries a message fit to show the user —
    'captions are disabled' is actionable, a stack trace is not."""

    def __init__(self, reason: str, *, permanent: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent  # False => worth retrying (rate limit, network)


@runtime_checkable
class Transcriber(Protocol):
    """Voice input. No implementation in Phase 1 — the protocol exists so that adding
    one is an adapter, not a refactor. Telegram's native transcription is MTProto-only
    and unavailable to bots, so this will be an external provider (ADR-004)."""

    async def transcribe(self, audio: bytes, mime: str) -> str: ...


@runtime_checkable
class Journal(Protocol):
    """The source of truth. `append` must be durable (fsync) before it returns —
    the shell does not acknowledge a message until it does."""

    async def append(self, entry: JournalEntry) -> None: ...

    def read_all(self) -> Iterator[JournalEntry]: ...


@runtime_checkable
class GraphStore(Protocol):
    """The projection. Every write is idempotent so replay and retry are safe."""

    async def upsert_thought(
        self, record: CaptureRecord, classification: Classification | None
    ) -> list[str]: ...

    async def mark_unclassified(self, id: UUID, error: str) -> None: ...
    async def recent(self, n: int = 10) -> list[ThoughtView]: ...
    async def by_category(self, category: str, n: int = 10) -> list[ThoughtView]: ...
    async def search(self, term: str, n: int = 10) -> list[ThoughtView]: ...
    async def last_thought_id(self) -> UUID | None: ...
    async def soft_delete(self, id: UUID) -> None: ...
    async def stats(self) -> GraphStats: ...
    async def follow(self, source: SourceRef) -> None: ...
    async def unfollow(self, name: str) -> bool: ...
    async def following(self) -> list[SourceRef]: ...
    async def pending(self) -> list[ThoughtView]: ...
    async def existing_ids(self) -> set[UUID]: ...


# --------------------------------------------------------------------------- #
# X pulse — trending posts as knowledge
# --------------------------------------------------------------------------- #

NAMESPACE_PULSE = UUID("8b2e4d61-7a09-4c3f-b5d8-1e6a9c40f273")
NAMESPACE_POST = UUID("d4a7c918-3e52-4f6b-8c01-5b9d2e7a6f14")


def pulse_id(topic: str, asked_at: datetime) -> UUID:
    """One digest run. `asked_at` is part of the key because two digests on the same
    topic hours apart are different digests, not a repeat of one."""
    return uuid5(NAMESPACE_PULSE, f"{topic.strip().casefold()}:{asked_at.isoformat()}")


def post_id(url: str) -> UUID:
    """Keyed on the post URL, so the same post appearing in several digests stays one
    node with several `HAS_POST` edges rather than a duplicate per digest."""
    return uuid5(NAMESPACE_POST, url.strip())


class PulsePost(BaseModel):
    """One item from a digest. These field descriptions are part of the prompt."""

    text: str = Field(
        max_length=300,
        description="The claim or news in one self-contained sentence, not a teaser",
    )
    author_handle: str = Field(description="The posting account as @handle")
    url: str = Field(description="Direct link to the post, x.com/<handle>/status/<id>")
    section: str | None = Field(
        default=None,
        max_length=40,
        description="A 2-4 word heading grouping related items, reused across them",
    )
    score: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "How much this is worth remembering months from now. "
            "0.9-1.0: a concrete release, benchmark number, or falsifiable claim. "
            "0.5-0.7: real news but widely covered. "
            "0.0-0.3: opinion, hype, or self-promotion."
        ),
    )
    """Drives both which posts are shown and the order they are shown in.

    Anchored explicitly because that text reaches the model: asked for an unqualified
    0-1 score it rates almost everything highly, and a ranking where everything is
    0.9 is not a ranking. Same reasoning as `VideoNote.score`.
    """


class PulseDigest(BaseModel):
    topic: str
    summary: str = Field(max_length=600, description="Two or three sentences")
    posts: list[PulsePost] = Field(default_factory=list)
    asked_at: datetime
    handles: list[str] = Field(default_factory=list)
    """Which followed handles the search was filtered on. Recorded so a thin digest
    can be explained later — an empty list means it was an unfiltered trending search."""


class PulseArtifact(BaseModel):
    """A fetched digest, cached on disk.

    Non-recomputable, like a transcript: Grok's answer at a moment in time cannot be
    re-derived tomorrow, and re-asking costs real money. Treated as an input alongside
    the journal — backed up, never auto-pruned.
    """

    digest: PulseDigest
    raw_response: dict
    fetched_at: datetime
    model: str
    sources_used: int = 0
    cost_usd: float | None = None


class PostView(BaseModel):
    """A post as read back out of the projection."""

    id: UUID
    text: str
    author_handle: str
    url: str
    section: str | None = None
    score: float = 0.5
    topic: str
    asked_at: datetime

    @property
    def deep_link(self) -> str:
        return self.url


class PulseResult(BaseModel):
    """Outcome of one /pulse — what the user is told."""

    topic: str
    digest: PulseDigest | None = None
    post_count: int = 0
    error: str | None = None
    dropped: int = 0
    """Items discarded for having no usable post link. Reported rather than swallowed:
    an unverifiable link is worse than a missing one because it still looks checkable."""
    cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.digest is not None


class PulseError(Exception):
    """Raised when a digest cannot be fetched. Carries a message fit to show the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Shopping list
# --------------------------------------------------------------------------- #


class ShoppingItem(BaseModel):
    """One thing to buy. These field descriptions are part of the prompt."""

    name: str = Field(
        max_length=80,
        description=(
            "The thing to buy, as it would be written on a list: 'oat milk', "
            "not 'a carton of oat milk'"
        ),
    )
    quantity: str | None = Field(
        default=None,
        max_length=40,
        description="How much, only when stated: '2L', 'a dozen'. Omit if not said.",
    )
    note: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "A qualifier that changes which product to pick: 'unsalted', 'the barista one'"
        ),
    )

    @property
    def canonical_name(self) -> str:
        return canonical(self.name)


class ShoppingExtraction(BaseModel):
    """The extractor's contract. An empty list is a valid answer — plenty of Shopping
    thoughts are deliberations ("Sony or Bose?") rather than things to buy."""

    items: list[ShoppingItem] = Field(default_factory=list)


class ItemView(BaseModel):
    """An item as read back out of the store."""

    id: int
    """Row id. Doubles as the callback reference for the tap-to-buy button, paired
    with a hash of the name so a stale keyboard cannot buy the wrong thing."""
    name: str
    canonical_name: str
    status: Literal["pending", "bought"] = "pending"
    added_at: datetime
    bought_at: datetime | None = None
    quantity: str | None = None
    note: str | None = None
    sources: int = 1
    """How many separate thoughts asked for this. >1 means you mentioned it twice."""


class ShoppingResult(BaseModel):
    """Outcome of extracting one thought — what the user is told."""

    thought_id: UUID
    items: list[ShoppingItem] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@runtime_checkable
class ShoppingStore(Protocol):
    """The shopping list projection.

    Deliberately not the graph (ADR-010): list state is small, mutable, and relational,
    and modelling it as nodes would grow the graph with rows no traversal benefits from.
    Like the graph, it is disposable — every row is rederivable from the journal.
    """

    async def ensure_schema(self) -> None: ...

    async def add(
        self, thought_id: UUID, items: list[ShoppingItem], captured_at: datetime
    ) -> None: ...

    async def mark(self, name: str, action: str, at: datetime) -> str | None: ...
    async def pending_items(self) -> list[ItemView]: ...
    async def bought_count(self) -> int: ...
    async def item_by_id(self, item_id: int) -> ItemView | None: ...
    async def last_bought(self) -> ItemView | None: ...
    async def record_extraction(
        self, thought_id: UUID, error: str | None = None
    ) -> None: ...

    async def extracted_ok_ids(self) -> set[UUID]: ...
    async def wipe(self) -> None: ...
    async def close(self) -> None: ...
