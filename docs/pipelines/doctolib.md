# Pipeline: Doctolib

**Status:** implemented
**Command:** `/doctor`

---

## 1. What it does

Answers one question — *when could we actually get in?* — without opening a browser,
picking the practice, picking the reason for the visit, and reading a calendar widget.

```
you: /doctor
vos: 🩺 Checking Doctolib…
vos: 🩺 Open appointments

     Wed 19 Aug
       10:20

     As of now — book on Doctolib, slots go fast.
```

Booking still happens on Doctolib. This tells you whether it is worth opening.

## 2. Why it is more than a bookmark

The booking page is a JavaScript app: the HTML it serves contains no slots at all, so
"just open the link" is the only thing a bookmark can do. Underneath, the app asks
`availabilities.json`, which answers in plain JSON. Reading that directly turns a
page-load, three clicks and a scan of a calendar into one word in a chat.

The awkward part is `agenda_ids`. The endpoint refuses without them, and they appear
nowhere public — not in the practice profile JSON, not on the booking page, not in the
per-practitioner JSON. The booking app fetches them separately from a route that is not
documented and will move.

So VOS does not discover them. **You paste the request your own browser made**, and this
pipeline reuses its query string. Configuration is copy-paste, re-configuration after a
practice reorganises its calendars is another copy-paste, and there is no second
undocumented endpoint to break.

## 3. Where the state lives

| Store | What it holds | Rebuildable |
|---|---|---|
| Journal | **Nothing.** Availability is fetched, not authored (ADR-010) | — |
| Neo4j | **Nothing.** A slot is not knowledge and no traversal wants it (ADR-012's reasoning) | — |
| `artifacts/doctolib/` | One snapshot per check: what was offered, when | No — it is the only record |

Nothing is written that a `vos reclassify --rebuild` would need to replay, because
nothing derived exists. If you book something and want that remembered, tell VOS in the
normal way and it becomes an ordinary capture.

## 4. Pipeline shape

```
/doctor ──► JobQueue ──► SlotFetcher.fetch()
                              │
                              ├─ parse the configured URL  ──► DoctolibError (permanent)
                              ├─ GET availabilities.json    ──► DoctolibError (transient)
                              ├─ flatten days → slots
                              └─ write artifacts/doctolib/<ts>.json
                                        │
                                        ▼
                                  render_doctor
```

Queued rather than run inline for the same reason `/pulse` is: it is a call to somebody
else's server with a 30 s timeout, and the polling loop must not stall behind it.

## 5. Identity and the freshness rule

`slot_id(agenda_ids, starts_at)` keys a slot on the calendars plus the exact start time,
because that pair is what makes two slots the same slot. It exists for the day something
wants to compare two snapshots; nothing stores slots yet.

Every reply is stamped *"As of now"*. This is the most perishable thing VOS reports — the
slot can be taken while the message is being read — and a timestamped answer is honest in
a way a bare list is not.

## 6. Failure modes

The thing being protected here is small: no capture is at risk, because `/doctor` writes
nothing. What matters is that a wrong answer never looks like a right one.

| What happens | What the user sees | What the system does |
|---|---|---|
| `VOS_DOCTOLIB_URL` unset | How to set it, and where to copy it from | Command is off; nothing else changes |
| Booking page URL pasted instead of the XHR | "That is the booking page, not the availabilities request" | Permanent error — retrying will not help |
| URL missing `agenda_ids` | "The URL is missing agenda_ids" | Permanent. **This is the important one:** without them the endpoint answers `200` with an empty list, which reads as a full calendar |
| Doctolib returns `{"error": [...]}` at 200 | The reason Doctolib gave | Permanent — every cause is a wrong parameter |
| Bot check (403) or rate limit (429) | "Try again later" | Transient |
| Practice gone (410) | "Capture a fresh availabilities URL" | Permanent |
| Timeout, connection reset, 5xx | "Doctolib did not answer in time" / "having trouble" | Transient |
| One malformed timestamp | The other slots, as normal | Skipped and logged; nine good slots are not lost to one bad one |
| Genuinely no free slots | "Nothing free in the next two weeks" | Not an error |

## 7. Commands and surfaces

| Where | Does |
|---|---|
| `/doctor` (Telegram) | Open slots at the configured practice, next 15 days |
| Doctor tab (kiosk) | The same slots as chips, grouped by day, refreshed every 2 minutes |

The kiosk tab is the reason this pipeline writes nothing. A wall tablet that anyone in
the house can poke is not a surface you want journaling a record per glance, and because
`GET /api/doctor` is pure read the tab can refresh on a timer without consequence. It
polls at 2 minutes rather than the shopping tab's 45 seconds: this one leaves the house.

Failures reach the tab as `200 {"ok": false, "detail": …}` rather than a 5xx, so a rate
limit renders as the sentence Doctolib gave rather than as a browser network error.

## 8. Decisions

- **On demand, never scheduled** (the `/pulse` precedent). Polling for a cancellation is
  exactly the "proactive behaviour" Phase 1 rules out, and it would turn one polite
  request per command into thousands.
- **The configured URL is the config** (ADR-016). Re-implementing agenda discovery
  against an undocumented route buys nothing and breaks silently.
- **No login, ever** (ADR-016). Public endpoint only. Credentials are a Phase-1 non-goal
  and a logged-in scrape is a different risk class entirely.
- **No model call.** The response is structured JSON. There is nothing to extract, so
  there is no cassette entry, no budget gate, and no provider that can be down.
- **Fetched, so it is an artifact** (ADR-010). Snapshots sit beside pulse digests.
- **A slot is not a graph node** (ADR-012's reasoning). Transient rows no traversal
  benefits from would grow the graph without making it worth looking at.

## 9. Scope boundary

Not in scope, and not accidentally half-built: booking, cancelling or rescheduling
anything; watching for cancellations and notifying; more than one practice; logging in as
a patient; reading your existing appointments; anything touching a medical record. Each
would need its own design, and the first two would need Phase 1's non-goals revisited
rather than worked around. None is blocked by anything here.
