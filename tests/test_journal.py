"""Journal tests.

The journal is the source of truth, so these tests assert the two properties the whole
architecture rests on:

  1. `append()` reaches disk (fsync) before it returns.
  2. A torn final line — a hard kill mid-write — costs at most the in-flight entry,
     never the file.

Everything else in VOS is rebuildable. This is not.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vos import journal as journal_module
from vos.contracts import CaptureRecord, ItemMark, Tombstone
from vos.journal import JsonlJournal


def _record(n: int, *, when: datetime | None = None, text: str | None = None) -> CaptureRecord:
    return CaptureRecord.create(
        chat_id=42,
        message_id=n,
        text=text if text is not None else f"thought {n}",
        captured_at=when or datetime(2026, 8, 7, 12, 0, tzinfo=UTC) + timedelta(minutes=n),
    )


@pytest.fixture
def jrnl(tmp_path: Path) -> JsonlJournal:
    return JsonlJournal(tmp_path / "journal")


# --- durability ---------------------------------------------------------- #


async def test_append_fsyncs_before_returning(jrnl: JsonlJournal, monkeypatch):
    """The ack path depends on this. If fsync stops being called, a power loss
    silently drops acknowledged thoughts — the one failure the design forbids."""
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", spy)
    await jrnl.append(_record(1))
    assert synced, "append() returned without fsync — durability guarantee is broken"


async def test_append_then_read_round_trip(jrnl: JsonlJournal):
    original = _record(1, text="buy oat milk")
    await jrnl.append(original)
    (read_back,) = list(jrnl.read_all())
    assert read_back == original


async def test_append_preserves_write_order(jrnl: JsonlJournal):
    for n in (1, 2, 3):
        await jrnl.append(_record(n))
    assert [e.text for e in jrnl.read_all()] == ["thought 1", "thought 2", "thought 3"]


async def test_files_split_by_month(jrnl: JsonlJournal, tmp_path: Path):
    await jrnl.append(_record(1, when=datetime(2026, 8, 7, tzinfo=UTC)))
    await jrnl.append(_record(2, when=datetime(2026, 9, 2, tzinfo=UTC)))
    names = sorted(p.name for p in (tmp_path / "journal").glob("*.jsonl"))
    assert names == ["2026-08.jsonl", "2026-09.jsonl"]
    # Reading must still stitch them back into one chronological stream.
    assert [e.text for e in jrnl.read_all()] == ["thought 1", "thought 2"]


# --- corruption tolerance ------------------------------------------------ #


async def test_torn_final_line_is_discarded_and_earlier_entries_survive(
    jrnl: JsonlJournal, tmp_path: Path, caplog
):
    """Simulates a hard kill partway through a write."""
    await jrnl.append(_record(1))
    await jrnl.append(_record(2))

    path = next((tmp_path / "journal").glob("*.jsonl"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"kind":"capture","id":"not-valid-js')  # truncated mid-write

    entries = list(jrnl.read_all())
    assert [e.text for e in entries] == ["thought 1", "thought 2"]
    assert any("torn final line" in r.message.lower() for r in caplog.records)


async def test_corruption_mid_file_is_logged_as_error(jrnl: JsonlJournal, tmp_path: Path, caplog):
    """Mid-file corruption is not an expected failure mode; it must be loud."""
    await jrnl.append(_record(1))
    path = next((tmp_path / "journal").glob("*.jsonl"))
    existing = path.read_text(encoding="utf-8")
    path.write_text("{ broken\n" + existing, encoding="utf-8")

    entries = list(jrnl.read_all())
    assert [e.text for e in entries] == ["thought 1"]
    assert any(r.levelname == "ERROR" for r in caplog.records)


# --- replay semantics ---------------------------------------------------- #


async def test_duplicate_id_collapses_last_write_wins(jrnl: JsonlJournal):
    """Telegram redelivery writes the same ID twice. Replay must not double-count."""
    await jrnl.append(_record(1, text="first"))
    await jrnl.append(_record(1, text="corrected"))

    assert len(list(jrnl.read_all())) == 2  # the raw stream keeps both
    records = jrnl.records()  # the replay view collapses them
    assert len(records) == 1
    assert records[0].text == "corrected"


async def test_tombstone_removes_record_from_replay(jrnl: JsonlJournal):
    r = _record(1)
    await jrnl.append(r)
    await jrnl.append(Tombstone(id=r.id, deleted_at=datetime.now(UTC)))

    assert len(list(jrnl.read_all())) == 2  # nothing is ever erased
    assert jrnl.records() == []  # but replay honours the delete


async def test_undo_does_not_edit_the_original_line(jrnl: JsonlJournal, tmp_path: Path):
    r = _record(1, text="secret thought")
    await jrnl.append(r)
    await jrnl.append(Tombstone(id=r.id, deleted_at=datetime.now(UTC)))

    raw = next((tmp_path / "journal").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "secret thought" in raw, "append-only violated: the original line was rewritten"


async def test_records_are_chronological_regardless_of_write_order(jrnl: JsonlJournal):
    late = _record(1, when=datetime(2026, 8, 9, tzinfo=UTC), text="late")
    early = _record(2, when=datetime(2026, 8, 8, tzinfo=UTC), text="early")
    await jrnl.append(late)
    await jrnl.append(early)
    assert [r.text for r in jrnl.records()] == ["early", "late"]


async def test_retried_kitchen_capture_collapses_to_one_record(jrnl: JsonlJournal):
    """A tablet retrying a POST after a dropped response is the kitchen's version of
    Telegram redelivery — same client_id, same ID, last write wins."""
    when = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    first = CaptureRecord.create_kitchen(client_id="c-7", text="buy milk", captured_at=when)
    retry = CaptureRecord.create_kitchen(client_id="c-7", text="buy milk", captured_at=when)
    await jrnl.append(first)
    await jrnl.append(retry)
    (only,) = jrnl.records()
    assert only.id == first.id
    assert only.channel == "kitchen"


async def test_last_capture(jrnl: JsonlJournal):
    assert jrnl.last_capture() is None
    await jrnl.append(_record(1))
    await jrnl.append(_record(2))
    assert jrnl.last_capture().text == "thought 2"  # type: ignore[union-attr]


async def test_empty_journal_reads_cleanly(jrnl: JsonlJournal):
    assert list(jrnl.read_all()) == []
    assert jrnl.records() == []


# --- shopping item marks -------------------------------------------------- #


async def test_item_marks_are_not_captures(jrnl: JsonlJournal):
    """The union gained a third kind, so `records()` had to stop assuming that
    anything which is not a tombstone is a thought."""
    await jrnl.append(_record(1))
    await jrnl.append(ItemMark(name="oat milk", action="bought", at=datetime.now(UTC)))

    assert [r.text for r in jrnl.records()] == ["thought 1"]
    assert jrnl.last_capture().text == "thought 1"  # type: ignore[union-attr]


async def test_item_marks_read_back_in_write_order(jrnl: JsonlJournal):
    """No dedup, unlike `records()`: each mark is an event, and the store decides
    which one wins by timestamp."""
    at = datetime(2026, 8, 9, 10, tzinfo=UTC)
    await jrnl.append(ItemMark(name="bananas", action="bought", at=at))
    await jrnl.append(ItemMark(name="bananas", action="unbought", at=at + timedelta(minutes=1)))

    assert [(m.name, m.action) for m in jrnl.item_marks()] == [
        ("bananas", "bought"),
        ("bananas", "unbought"),
    ]


async def test_item_mark_lands_in_the_month_file_of_its_own_timestamp(
    jrnl: JsonlJournal, tmp_path: Path
):
    await jrnl.append(ItemMark(name="oranges", at=datetime(2026, 3, 4, tzinfo=UTC)))
    assert (tmp_path / "journal" / "2026-03.jsonl").exists()


async def test_undo_does_not_disturb_the_shopping_list(jrnl: JsonlJournal):
    """Tombstones are keyed by thought id; marks are keyed by item name. Undoing a
    thought must not un-buy anything."""
    record = _record(1)
    await jrnl.append(record)
    await jrnl.append(ItemMark(name="oat milk", at=datetime.now(UTC)))
    await jrnl.append(Tombstone(id=record.id, deleted_at=datetime.now(UTC)))

    assert jrnl.records() == []
    assert [m.name for m in jrnl.item_marks()] == ["oat milk"]


async def test_a_torn_mark_costs_only_itself(jrnl: JsonlJournal, tmp_path: Path):
    await jrnl.append(_record(1))
    await jrnl.append(ItemMark(name="bananas", at=datetime.now(UTC)))
    path = next((tmp_path / "journal").glob("*.jsonl"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "item_mark", "name": "half-writ')

    assert [m.name for m in jrnl.item_marks()] == ["bananas"]
    assert [r.text for r in jrnl.records()] == ["thought 1"]
