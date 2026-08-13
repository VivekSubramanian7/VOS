# Reminders — VOS speaks first, once, on time

**Status:** approved design, not yet implemented
**Date:** 2026-08-13

## Context

VOS should be able to say *"call the practice"* on Tuesday at 9am, without being asked on
Tuesday at 9am.

Every feature built so far answers when spoken to. A capture is a reply to a message; a
digest, a video, a shopping list and a slot check all begin with the user typing something.
A reminder is the first thing that begins with a clock. That makes this the first deliberate
crossing of a Phase-1 non-goal — *"proactive behaviour"* (§2) — and the crossing needs to be
scoped rather than assumed, which is what ADR-017 does.

**The single-writer invariant is the constraint that shapes everything else.** The obvious
design is a third service beside the bot and the deploy poller: a reminder daemon with its
own loop. The journal is what makes that expensive. Firing a reminder is harmless, but
*closing* one is a decision the user authored, and by ADR-013 that belongs in the journal —
which ADR-008 guarantees has exactly one writer, and which ADR-014 already refused to share
across processes once, because doing so needs an invented internal API purely to keep the
append safe.

So the scheduler lives inside the daemon. It is not a compromise: the `JobQueue` is already
the seam a second process would have to reinvent, and putting the ticker in front of it costs
one asyncio task and preserves every guarantee. If isolation becomes worth wanting later, the
cut is at the queue and the rest of this design is unchanged.

One nuance worth recording because it is counter-intuitive: Telegram's exclusivity is only on
`getUpdates`. A separate service *may* `sendMessage` freely. The journal is the reason not to
split, not Telegram.

### Decisions

| Decision | Choice | Why not the alternative |
|---|---|---|
| Where the scheduler runs | An asyncio ticker inside the daemon, feeding the existing `JobQueue` | A third container needs a second journal writer or an internal API — ADR-014 rejected that shape already, and it buys isolation nothing here needs |
| Trigger | Time only — a due timestamp passes | Location and event triggers are separate designs with separate failure modes |
| Creation, v1 | `/remind <when> <what>`, parsed, no model | A model call to read "tuesday 9am" is spend and latency on the one path that must never misfire |
| Creation, v2 | `Reminder` category, model extracts the time | Command-only means saying "remind me to…" in passing silently does nothing |
| Repeat behaviour | Fire exactly once, stamp `fired_at` | Re-firing until acknowledged needs backoff, a cap and quiet hours, and is what gets a bot muted |
| Overdue after downtime | Fire late, saying how late | Silently skipping is the one outcome that makes a reminder worse than a sticky note |
| Due list | Disposable SQLite projection, beside `shopping.db` | Nodes for rows that flip between two states, exactly what ADR-012 declined for the shopping list |
| Closing a reminder | Fourth `JournalEntry` kind, shaped like `ItemMark` | Projection-only means `--rebuild` resurrects every completed reminder — the failure ADR-013 exists for |
| Recording a *fire* | Same journal kind, `action="fired"` | SQLite-only is the obvious answer and it is wrong: `--rebuild` wipes projections, so every past reminder would fire again. Nothing else can reconstruct it |
| Timezone | `VOS_TIMEZONE`, `Europe/Berlin`; UTC everywhere internally | UTC-only makes "9am" drift an hour twice a year; zone-aware storage makes every comparison a timezone question |
| Delivery, v1 | Telegram `sendMessage` to the owner | A kiosk banner is a second surface to get right before the first one is proven |

### Non-goals

Recurring reminders ("every second Tuesday"); location triggers; reminders for anyone but
the owner; calendar or ICS sync; quiet hours; reminders that take an action rather than send a
message. Recurrence is the one most likely to be half-built by accident — a parser that
understands "every" without a scheduler that can expand it is worse than one that refuses.

## Architecture

```
                 ┌─ /remind tuesday 9am call the practice   (v1, no model)
   creation ─────┤
                 └─ "remind me to call on tuesday" → Reminder category → extract  (v2)
                            │
                            ▼
                    journal: CaptureRecord          ← durability boundary, unchanged
                            │
                            ▼
                    reminders.db (projection)       ← due_at, fired_at, closed_at

   ReminderTicker (asyncio, 30s)
        └─ due_at <= now() AND fired_at IS NULL
             └─ JobQueue.submit(f"remind:{id}")     ← same single worker (ADR-008)
                  ├─ bot.send_message(owner, text)  ← 1. send FIRST
                  ├─ journal: ReminderMark(fired)   ← 2. then record it
                  └─ store.mark_fired(id, now)      ← 3. projection follows

   /done <n> · /snooze <n> <when>
        └─ journal: ReminderMark                    ← user-authored (ADR-013)
             └─ store.close(id) / store.reschedule(id, due_at)
```

### Send first, then record

The order in step 1–2 is deliberate and is the opposite of the capture path's
journal-then-ack. Recording a fire before sending would mean a crash in between leaves a
reminder VOS believes it delivered and never did — silently, forever. That is the exact
failure the feature exists to prevent. Sending first inverts the risk: a crash in the gap
costs one duplicate message on the next tick, because the projection still shows it unfired.

A duplicate reminder is an annoyance. A reminder you trusted and never received is the end of
trusting the feature at all.

The ticker holds no state. Everything it needs is a query against the projection, so a
restart mid-flight loses nothing and re-derives its work — the same property that lets
`JobQueue` stay non-durable (`jobs.py:11-15`).

### Startup recovery

On boot the ticker runs its query once before entering the loop. Anything already due and
unfired is fired immediately, with the message saying how overdue it is:

> ⏰ **call the practice** — was due 3 hours ago

This mirrors `requeue_unprocessed_videos` (`shell.py:557`): outstanding work derived from a
projection rather than remembered in a queue. A reminder more than **7 days** overdue is
closed unfired and reported in a single summary line.

That cutoff is a *policy*, not a safety net. Because the fire event is journalled, replay
restores `fired_at` exactly and nothing can re-fire by accident — the cutoff exists only
because a week-old reminder arriving at boot is noise rather than diligence. The threshold is
a flagged decision rather than a silent one.

## Contracts (`src/vos/contracts.py`)

```python
NAMESPACE_REMINDER = UUID("…")

def reminder_id(thought_id: UUID, due_at: datetime) -> UUID:
    """One reminder. Keyed on the thought plus the due time, so re-running extraction
    over the same thought converges instead of duplicating."""


class Reminder(BaseModel):
    """A thing to be said back at a time. Field descriptions are part of the v2 prompt."""
    text: str = Field(max_length=200, description="What to remind, in the imperative")
    due_at: datetime = Field(description="When, as an ISO timestamp with offset")


class ReminderView(BaseModel):
    """A row as read back out of the projection."""
    id: UUID
    text: str
    due_at: datetime
    fired_at: datetime | None = None
    closed_at: datetime | None = None


class ReminderMark(BaseModel):
    """Appended when a reminder fires, or when the user closes or moves it.

    `done`, `snoozed` and `cancelled` are decisions the person made — the `ItemMark`
    argument, unchanged. `fired` is the odd one: VOS wrote it, not the user.

    It belongs here anyway because it is **not derivable**. Nothing about a reminder's
    text or due time tells you whether the message actually went out, so a projection
    is the only other place it could live — and projections are wiped by
    `vos reclassify --rebuild`, which would re-fire every past reminder. Contrast a
    classification, which replay reconstructs exactly and which therefore stays out of
    the journal. See ADR-017.
    """
    model_config = {"frozen": True}
    kind: Literal["reminder_mark"] = "reminder_mark"
    reminder: UUID
    action: Literal["fired", "done", "snoozed", "cancelled"]
    due_at: datetime | None = None   # set only for `snoozed`
    at: datetime


JournalEntry = Annotated[
    CaptureRecord | Tombstone | ItemMark | ReminderMark, Field(discriminator="kind")
]
```

`journal.py:_stamped_at` (line 39) must learn `ReminderMark.at`, or the entry lands in the
wrong month file. `ReminderStore` is a `@runtime_checkable` Protocol alongside
`ShoppingStore`, so the ticker depends on a seam rather than on SQLite.

## Storage

An addition to the §7.3 table in `docs/architecture.md`:

| Path | Contents | Backup | Rebuildable |
|---|---|---|---|
| `reminders.db` | Derived projection — due list and fired/closed state | No | Yes, from journal |

Every row rederives, with no asterisk: the text and due time from the capture, and
`fired_at`, `closed_at` and any snoozed due time from `ReminderMark` replay. `rebuild()` in
`projection.py:233` wipes and replays it exactly as it does `shopping.db`, and
`replay_item_marks` (line 210) gains a sibling, `replay_reminder_marks`.

That completeness is the whole reason `fired` is a journal entry rather than a column that
only ever lived in SQLite. The alternative — a projection holding the one fact replay cannot
reconstruct — would mean a `--rebuild` silently re-firing every past-due reminder, and
`--rebuild` is an ordinary operation here.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `VOS_TIMEZONE` | `Europe/Berlin` | Parsing and display only. Storage and comparison stay UTC |
| `VOS_REMINDER_DB` | `./reminders.db` | Disposable, like `VOS_SHOPPING_DB`; `/data/` under compose |
| `VOS_REMINDER_TICK_S` | `30` | Flagged: 30 s bounds worst-case lateness at 30 s and costs one query |

No new secret. Reminders need no credential and reach no third party.

## Time parsing

The v1 parser is small and refuses loudly. It accepts what a person actually types —
`tomorrow 9am`, `tuesday 9:00`, `in 2 hours`, `25 dec 08:00`, `18:30` — resolves against
`VOS_TIMEZONE`, and converts to UTC once. Anything it cannot read gets an error naming what
it did understand, never a guess:

> I can read "tomorrow 9am", "tuesday 9:00", "in 2 hours". I couldn't read "soonish".

A wrong time is worse than a rejected one: a reminder that fires at the wrong hour is a
reminder you stop trusting, and trust is the whole product.

## Errors

| Failure | Behaviour |
|---|---|
| Unparseable time | Refused at `/remind` with examples; nothing stored |
| Due time in the past | Accepted and fired on the next tick — the user may mean "now" |
| `send_message` fails | Logged, no `fired` mark written, retried on the next tick |
| Telegram down at boot | Recovery query re-runs every tick; nothing is lost |
| Crash between send and the `fired` mark | One duplicate on the next tick. Deliberate: the reverse order risks a reminder believed sent that never was |
| Journal write fails after send | Same as above — logged, re-fires once. The journal is never left claiming a fire that did not happen |
| Ticker task dies | Logged and restarted by the daemon; missed reminders fire late by design |
| Reminder >7 days overdue at boot | Closed unfired, reported in one summary line |
| Clock jumps (DST, NTP) | Comparison is UTC, so unaffected. Display shifts, which is correct |

No row loses data: the capture is durable before any of this runs, and every failure either
retries or degrades to a late message.

## Testing

- `tests/test_reminders.py` — store semantics on real SQLite in `tmp_path`, no mocks, and
  the same replay-order property `test_shopping.py` asserts: marks are commutative, so the
  journal replays in any order and converges. **The round trip is the headline test:** fire
  a reminder, wipe the store, replay the journal, assert `fired_at` comes back identical and
  the reminder does not fire a second time.
- `tests/test_reminder_time.py` — the parser, table-driven over accepted and rejected
  strings, with DST boundaries pinned (the last Sunday in March and October) since that is
  the one date arithmetic that silently breaks.
- `tests/test_reminder_ticker.py` — an injected clock and a fake `JobQueue`: due fires,
  not-yet-due does not, fired-once never fires twice, overdue-at-boot fires late, and
  >7-days-overdue closes unfired.
- `tests/test_shell_reminders.py` — `/remind`, `/done`, `/snooze` against the shared fakes
  imported from `test_shell`, jobs driven with `start` → `drain` → `stop`.

No test may sleep for real time; the ticker takes an injectable clock and an injectable
interval for exactly that reason.

## Verification

```bash
uv run pytest -m "not integration"
uv run ruff check src tests
```

Then, by hand:

```
/remind in 2 minutes test the reminder     → confirmation with the resolved local time
                                            (wait)  → ⏰ fires once
/remind tuesday 9am call the practice      → confirmation
/reminders                                 → both listed, next first
/done 1                                    → closes; journal gains a reminder_mark
docker compose restart app                 → no duplicate fire of the one already fired
vos reclassify --rebuild                   → open reminders survive, closed stay closed,
                                             and the already-fired one does NOT fire again
```

The last two lines are the ones that matter. Everything else is a feature; those two are the
invariants — and the second is only true because the fire is in the journal.

## Risks

**Proactive behaviour is a door, not a step.** Once VOS can speak first, every future feature
will want to. ADR-017 scopes this to *the user's own reminder, at the user's own stated time,
once* — and that sentence is the test any future proactive feature has to pass.

**A system-authored journal entry sets a precedent.** `fired` is the first thing VOS writes
to the journal that the user did not author, and the next feature that wants to record
something will cite it. The test it had to pass is narrow and should stay narrow: *not
derivable by replay*. A classification fails that test and stays out; a fire passes it,
because nothing in the reminder's text or due time reveals whether the message was sent. One
line per fired reminder is negligible at this system's volume — the precedent is the cost,
not the bytes.

**The v2 classifier path can misfire.** A thought that merely mentions Tuesday is not a
reminder. Mitigation: v2 ships only after v1 is trusted, extraction requires an explicit
imperative, and a reminder created by classification says so in its confirmation — so a wrong
one is visible immediately, not at 9am on Tuesday.

**A ticker that dies silently is worse than no ticker.** It logs on start and on every fire,
and `/reminders` shows the next due time, so a dead ticker is visible the first time someone
looks at the list.
