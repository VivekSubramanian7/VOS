"""The journal — source of truth.

Append-only JSONL, one file per month. Everything else in VOS is a projection of this
stream, which is what makes the graph rebuildable, backups a file copy, and prompt
improvements retroactive.

Two invariants carry the whole design:

1. `append()` does not return until the bytes are on disk (`flush` + `fsync`). The
   shell does not acknowledge a Telegram message until it returns. That is the entire
   durability guarantee — there is no other mechanism, and nothing downstream can
   weaken it.

2. Lines are never edited or removed. `/undo` appends a tombstone rather than mutating
   the original capture. Replay is therefore deterministic: the same file always
   reconstructs the same state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from vos.contracts import CaptureRecord, JournalEntry, Tombstone

log = logging.getLogger(__name__)

_entry_adapter: TypeAdapter[JournalEntry] = TypeAdapter(JournalEntry)


class JsonlJournal:
    """File-backed implementation of the `Journal` protocol."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Serialises writers. Phase 1 is single-writer by design (ADR-008), but a
        # half-written line is unrecoverable in a way a queued one is not, so the
        # lock stays regardless of how confident we are about the caller.
        self._lock = asyncio.Lock()

    # -- paths ---------------------------------------------------------- #

    def _path_for(self, when: datetime) -> Path:
        return self._dir / f"{when.astimezone(UTC):%Y-%m}.jsonl"

    def _files(self) -> list[Path]:
        # Lexicographic sort on YYYY-MM is chronological.
        return sorted(self._dir.glob("*.jsonl"))

    # -- write ---------------------------------------------------------- #

    async def append(self, entry: JournalEntry) -> None:
        """Append one entry and fsync before returning.

        Raises whatever the filesystem raises (e.g. `OSError` when the disk is full).
        Callers must let that propagate: failing to ack is correct, silently
        continuing is not.
        """
        when = entry.captured_at if isinstance(entry, CaptureRecord) else entry.deleted_at
        line = entry.model_dump_json()
        async with self._lock:
            await asyncio.to_thread(self._append_sync, self._path_for(when), line)

    @staticmethod
    def _append_sync(path: Path, line: str) -> None:
        # Opened per append. At this volume the extra syscalls are free, and it avoids
        # holding a handle across a month boundary or a rotated file.
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # the durability boundary

    # -- read ----------------------------------------------------------- #

    def read_all(self) -> Iterator[JournalEntry]:
        """Stream every entry in write order.

        A torn final line — the signature of a hard kill mid-write — is discarded with
        a warning; every earlier line remains valid. This is why JSONL was chosen over
        a format where a partial write corrupts the file.
        """
        for path in self._files():
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            last = len(lines) - 1
            for i, raw in enumerate(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield _entry_adapter.validate_python(json.loads(raw))
                except (json.JSONDecodeError, ValidationError) as exc:
                    if i == last:
                        log.warning(
                            "Discarding torn final line in %s (likely an interrupted "
                            "write; every earlier entry is intact): %s",
                            path.name,
                            exc,
                        )
                    else:
                        # Mid-file corruption is not an expected failure mode and
                        # should be loud rather than quietly skipped.
                        log.error("Corrupt entry at %s line %d: %s", path.name, i + 1, exc)

    def records(self) -> list[CaptureRecord]:
        """Live captures in chronological order, deduplicated and tombstones applied.

        Duplicate IDs collapse last-write-wins, which is what makes replaying a
        journal that saw a Telegram redelivery produce the same result as one that
        did not.
        """
        alive: dict[UUID, CaptureRecord] = {}
        for entry in self.read_all():
            if isinstance(entry, Tombstone):
                alive.pop(entry.id, None)
            else:
                alive[entry.id] = entry
        return sorted(alive.values(), key=lambda r: r.captured_at)

    def last_capture(self) -> CaptureRecord | None:
        records = self.records()
        return records[-1] if records else None
