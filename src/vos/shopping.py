"""The shopping list — a SQLite projection.

Deliberately *not* in Neo4j (ADR-010). A shopping list is small, mutable, and
relational: rows that flip between two states and get read back in one order. Modelling
that as nodes would grow the graph with data no traversal benefits from, and the graph
is the thing that has to stay worth looking at.

It is a projection in exactly the sense the graph is: every row here is rederivable
from the journal. Captures replay through extraction; ticks replay from `ItemMark`
entries. So this file may be deleted at any time, and `vos reclassify --rebuild` does
precisely that.

Two design points carry the module:

1. **Timestamps are compared as strings.** Every ordering and every status decision is
   a string compare on `_iso()` output, which is fixed-width UTC so that lexicographic
   order *is* chronological order.

2. **Events are commutative.** `add` and `mark` both gate on `status_at`, so replaying
   the journal in any order converges on the same list. That is what lets startup
   recovery, out-of-order re-extraction, and a rebuild share one code path without a
   sequencer — the same property `Neo4jGraph._link_thread` was written for.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from vos.contracts import ItemView, ShoppingItem, canonical

log = logging.getLogger(__name__)

T = TypeVar("T")

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS items (
        id             INTEGER PRIMARY KEY,
        canonical_name TEXT NOT NULL UNIQUE,
        display_name   TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'bought')),
        added_at       TEXT NOT NULL,
        bought_at      TEXT,
        status_at      TEXT NOT NULL
    )
    """,
    # Quantity and note live here rather than on the item: they belong to the *asking*,
    # not to the thing. "2L oat milk" today and "oat milk" next week are one item with
    # two requests, and the list should show the most recent qualifier.
    """
    CREATE TABLE IF NOT EXISTS adds (
        item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        thought_id TEXT NOT NULL,
        quantity   TEXT,
        note       TEXT,
        added_at   TEXT NOT NULL,
        PRIMARY KEY (item_id, thought_id)
    )
    """,
    # The completion marker. A successful extraction can legitimately produce zero
    # items, so "has no adds row" cannot mean "not yet extracted" — this table is the
    # only honest answer to what startup recovery still owes.
    """
    CREATE TABLE IF NOT EXISTS extractions (
        thought_id   TEXT PRIMARY KEY,
        extracted_at TEXT NOT NULL,
        error        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS items_status ON items(status, added_at)",
)


def _iso(when: datetime) -> str:
    """Fixed-width UTC, so SQLite's string comparison *is* chronological order.

    `datetime.isoformat()` omits `.000000` when the microsecond happens to be zero,
    which makes its output variable-width and mis-orders rows around whole seconds.
    Every status gate in this module rests on that comparison, so the width is pinned.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _at(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _to_item(row: sqlite3.Row) -> ItemView:
    return ItemView(
        id=int(row["id"]),
        name=row["display_name"],
        canonical_name=row["canonical_name"],
        status=row["status"],
        added_at=_at(row["added_at"]),  # type: ignore[arg-type]
        bought_at=_at(row["bought_at"]),
        quantity=row["quantity"],
        note=row["note"],
        sources=int(row["sources"] or 0),
    )


# One row shape for every read, so `_to_item` has a single contract. Qualifiers come
# from the most recent request outright, including when that request carried none:
# asking plainly for "oat milk" after asking for "2L oat milk" means you no longer
# want 2L, and a list that kept showing it would be reporting a stale intention.
_SELECT = """
    SELECT i.id, i.canonical_name, i.display_name, i.status, i.added_at, i.bought_at,
           (SELECT a.quantity FROM adds a WHERE a.item_id = i.id
             ORDER BY a.added_at DESC, a.rowid DESC LIMIT 1) AS quantity,
           (SELECT a.note FROM adds a WHERE a.item_id = i.id
             ORDER BY a.added_at DESC, a.rowid DESC LIMIT 1) AS note,
           (SELECT count(*) FROM adds a WHERE a.item_id = i.id) AS sources
      FROM items i
"""


class SqliteShoppingStore:
    """Implementation of the `ShoppingStore` protocol.

    Stdlib `sqlite3` on a worker thread rather than an async driver: at this volume
    (a handful of writes a day, single-writer by ADR-008) an extra dependency would buy
    nothing. The lock and `to_thread` shape mirror `JsonlJournal.append` exactly.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- plumbing ------------------------------------------------------- #

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            # `to_thread` hands work to an arbitrary pool thread, and sqlite3 refuses
            # cross-thread use by default. Safe to disable here precisely because
            # `_lock` serialises every call — the check would be guarding an invariant
            # that is already held one level up.
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL and NORMAL are right *because* this store is disposable: the journal
            # is the durability boundary, and paying fsync here would buy nothing the
            # journal has not already guaranteed.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._conn = conn
        return self._conn

    async def _call(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run one unit of work as a single transaction, off the event loop."""

        def run() -> T:
            conn = self._connection()
            try:
                result = work(conn)
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            return result

        async with self._lock:
            return await asyncio.to_thread(run)

    # -- schema --------------------------------------------------------- #

    async def ensure_schema(self) -> None:
        """Idempotent; safe to run on every startup."""

        def work(conn: sqlite3.Connection) -> None:
            for statement in SCHEMA:
                conn.execute(statement)

        await self._call(work)

    # -- writes --------------------------------------------------------- #

    async def add(
        self, thought_id: UUID, items: list[ShoppingItem], captured_at: datetime
    ) -> None:
        """Project one thought's extracted items.

        Replaces that thought's requests rather than adding to them, so re-extracting
        after a prompt change corrects the list instead of doubling it — the same
        "replace, never accumulate" discipline `upsert_thought` uses.
        """
        at = _iso(captured_at)
        tid = str(thought_id)

        def work(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM adds WHERE thought_id = ?", (tid,))
            for item in items:
                item_id = _upsert_item(conn, item, at)
                conn.execute(
                    "INSERT OR REPLACE INTO adds"
                    " (item_id, thought_id, quantity, note, added_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (item_id, tid, item.quantity, item.note, at),
                )
            # An item the previous extraction produced and this one did not is gone
            # from the list — unless it was already bought, which is history and may
            # be what a replayed mark is pointing at.
            conn.execute(
                "DELETE FROM items WHERE status = 'pending'"
                " AND id NOT IN (SELECT item_id FROM adds)"
            )

        await self._call(work)

    async def mark(self, name: str, action: str, at: datetime) -> str | None:
        """Tick an item off, or put it back. Returns the display name, or None if the
        mark was stale — a later event had already decided this item."""
        key = canonical(name)
        stamp = _iso(at)
        bought = action == "bought"

        def work(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT id, display_name, status_at FROM items WHERE canonical_name = ?",
                (key,),
            ).fetchone()

            if row is None:
                # Replay reaches marks before the thoughts behind them have been
                # re-extracted. Creating the item here means the later `add` finds it
                # and — its capture being older — leaves the tick standing, instead of
                # the tick being silently dropped.
                conn.execute(
                    "INSERT INTO items (canonical_name, display_name, status, added_at,"
                    " bought_at, status_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        name.strip(),
                        "bought" if bought else "pending",
                        stamp,
                        stamp if bought else None,
                        stamp,
                    ),
                )
                return name.strip()

            if row["status_at"] >= stamp:
                return None

            conn.execute(
                "UPDATE items SET status = ?, bought_at = ?, status_at = ? WHERE id = ?",
                (
                    "bought" if bought else "pending",
                    stamp if bought else None,
                    stamp,
                    row["id"],
                ),
            )
            return row["display_name"]

        return await self._call(work)

    async def record_extraction(self, thought_id: UUID, error: str | None = None) -> None:
        """Mark a thought as extracted, successfully or not.

        An error row deliberately does *not* count as done: extraction failures here
        are model or network failures, all transient, and one cheap retry per restart
        is bounded. This is the one place the shopping pipeline departs from video,
        which needs a permanence flag because a video can lose its captions forever.
        """

        def work(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO extractions (thought_id, extracted_at, error)"
                " VALUES (?, ?, ?)",
                (str(thought_id), _iso(datetime.now(UTC)), error[:500] if error else None),
            )

        await self._call(work)

    async def wipe(self) -> None:
        """Destroy the projection. Safe because the journal is the source of truth —
        the same reasoning that makes `Neo4jGraph.wipe` an ordinary operation."""

        def work(conn: sqlite3.Connection) -> None:
            for table in ("adds", "extractions", "items"):
                conn.execute(f"DELETE FROM {table}")

        await self._call(work)

    # -- reads ---------------------------------------------------------- #

    async def pending_items(self) -> list[ItemView]:
        """Everything still to buy, oldest first.

        Stable ordering is a feature, not a detail: it is what makes `/bought 2` mean
        the same row the user just read, and new items append at the bottom rather
        than shuffling the numbering.
        """

        def work(conn: sqlite3.Connection) -> list[ItemView]:
            rows = conn.execute(
                _SELECT + " WHERE i.status = 'pending' ORDER BY i.added_at ASC, i.id ASC"
            ).fetchall()
            return [_to_item(r) for r in rows]

        return await self._call(work)

    async def bought_count(self) -> int:
        def work(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT count(*) AS n FROM items WHERE status = 'bought'"
            ).fetchone()
            return int(row["n"])

        return await self._call(work)

    async def item_by_id(self, item_id: int) -> ItemView | None:
        def work(conn: sqlite3.Connection) -> ItemView | None:
            row = conn.execute(_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
            return _to_item(row) if row else None

        return await self._call(work)

    async def last_bought(self) -> ItemView | None:
        """Most recently ticked off — what the Undo button reverses."""

        def work(conn: sqlite3.Connection) -> ItemView | None:
            row = conn.execute(
                _SELECT
                + " WHERE i.status = 'bought' ORDER BY i.bought_at DESC, i.id DESC LIMIT 1"
            ).fetchone()
            return _to_item(row) if row else None

        return await self._call(work)

    async def extracted_ok_ids(self) -> set[UUID]:
        """Thoughts extraction has finished with. The complement of this set, within
        the Shopping category, is what startup recovery owes."""

        def work(conn: sqlite3.Connection) -> set[UUID]:
            rows = conn.execute(
                "SELECT thought_id FROM extractions WHERE error IS NULL"
            ).fetchall()
            return {UUID(r["thought_id"]) for r in rows}

        return await self._call(work)

    async def close(self) -> None:
        def work() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        async with self._lock:
            await asyncio.to_thread(work)


def _upsert_item(conn: sqlite3.Connection, item: ShoppingItem, at: str) -> int:
    """Insert or reconcile one item, returning its row id.

    The reconciliation is the whole time rule: buying something ends its life on the
    list, but asking for it again afterwards starts a new one.
    """
    row = conn.execute(
        "SELECT id, status, status_at, added_at FROM items WHERE canonical_name = ?",
        (item.canonical_name,),
    ).fetchone()

    if row is None:
        cursor = conn.execute(
            "INSERT INTO items (canonical_name, display_name, status, added_at, status_at)"
            " VALUES (?, ?, 'pending', ?, ?)",
            (item.canonical_name, item.name.strip(), at, at),
        )
        return int(cursor.lastrowid or 0)

    if row["status"] == "bought" and row["status_at"] < at:
        # Asked for again after it was bought: a new request, so it re-opens and goes
        # to the bottom of the list rather than back where it originally sat.
        conn.execute(
            "UPDATE items SET status = 'pending', bought_at = NULL, status_at = ?,"
            " added_at = ?, display_name = ? WHERE id = ?",
            (at, at, item.name.strip(), row["id"]),
        )
    elif row["added_at"] > at:
        # An older thought replaying: this has been on the list for longer than the
        # current row claims.
        conn.execute("UPDATE items SET added_at = ? WHERE id = ?", (at, row["id"]))

    return int(row["id"])
