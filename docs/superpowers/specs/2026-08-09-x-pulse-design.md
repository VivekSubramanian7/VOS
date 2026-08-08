# X Pulse — on-demand "best of AI on X" digest

**Status:** approved design, not yet implemented
**Date:** 2026-08-09

## Context

VOS should answer, on demand, *"what were the best things said about AI on X in the
last day?"* — including posts from specific people the user chooses to follow.

Raw X data is the constraint that shapes everything else. The official X API is
pay-per-read (~$0.005/post, ~$30/month to poll a handful of handles); scraper
resellers are cheap but ToS-grey and brittle. The chosen source is **xAI Live
Search**: the Grok API searches X natively, returns curated results with post
citations, and accepts an `x_handles` filter that maps directly onto "people I
follow". One call does discovery *and* curation, so no second model pass is needed.

**Cost is the headline constraint.** Live Search bills **$25 per 1,000 sources**
(`$0.025` per source) on top of tokens. At the chosen cap of 25 sources a digest
costs **~$0.63**, which is roughly a third of the existing `$2.00` daily budget.
This is why the digest is on-demand rather than scheduled, why the source cap is a
setting, and why the call must be recorded against `BudgetGuard`.

### Decisions

| Decision | Choice | Why not the alternative |
|---|---|---|
| Data source | xAI Live Search | Official X API ~$30/mo; scrapers break and violate ToS |
| Trigger | `/pulse [topic]`, on demand only | A scheduler spends money while the user isn't reading |
| Topic | Defaults to AI, optional argument | Free to support; a saved-topics list is YAGNI |
| Persistence | Every item stored in the graph | Digest-only would strand anything interesting |
| Handles | Extend `/follow` with kind `x` | A parallel `/track` list is a second follow system to remember |
| Source cap | 25, configurable | User accepted ~$0.63/digest for the broadest sweep |

### Non-goals

Scheduled/push digests; threading or replies; images and video; engagement metrics
as a ranking signal; following someone's *entire* timeline exhaustively (Live Search
returns what Grok judges relevant, not the firehose).

## Architecture

Mirrors the video pipeline, which already solves the same shape: slow external
fetch → structured extraction → graph projection → capped, section-grouped reply
with `/more` for the rest.

```
/pulse [topic]
  └─ BudgetGuard.exceeded()? → refuse with the spend, do nothing else
  └─ JobQueue.submit(...)                    ← same single worker as video (ADR-008)
       ├─ PulseFetcher.fetch(topic, handles) ← xAI chat completions + Live Search
       │    └─ artifacts/pulses/{id}.json    ← write-once cache (see below)
       ├─ Cassette.record(cost_usd=…)        ← makes the spend visible to /stats
       ├─ graph.save_pulse(digest)           ← MERGE (:Pulse)-[:HAS_POST]->(:Post)
       └─ reply: top 8 by score, grouped by section, "/more for the rest"
```

No followed handles is a normal state: the search runs unfiltered and returns
trending posts.

### Two corrections to the original plan

Both were found by reading the code and are load-bearing.

**1. Digests are artifacts, not journal entries.** The plan said journal the payload.
`JournalEntry` is a *discriminated union of `capture | tombstone`*
(`contracts.py:135`), and `records()` (`journal.py:114`) assumes every live entry is
a `CaptureRecord`. A third kind would change replay semantics — and therefore
`vos reclassify --rebuild` — for a payload that is not a user thought.

The established home for a non-recomputable external fetch is the **artifact cache**,
exactly as `video.py:9` describes for transcripts. A digest qualifies: Grok's answer
at a moment in time cannot be re-derived tomorrow. So it is written to
`artifacts/pulses/{pulse_id}.json`, write-once, backed up alongside the journal.

**2. Posts get their own label, not `:Note`.** The plan said reuse `:Note` so
`search_notes` finds both. It would not have: `search_notes` (`graph.py:571`) matches
`(note)-[:FROM]->(v:Video)`, so pulse items would enter the `note_text` fulltext index
and then be silently filtered out of every result. `_to_note` (`graph.py:72`) also
requires `t_seconds`, `video_id`, `video_title`, `url` — fields a post does not have.

Making `NoteView` polymorphic with four optional fields would weaken a contract the
video path depends on. Instead posts get a `:Post` label, their own uniqueness
constraint and fulltext index, and their own `PostView`. `/notes` queries both and
renders two labelled groups, so the user-visible behaviour is what the plan intended.

## Contracts (`src/vos/contracts.py`)

```python
NAMESPACE_PULSE = UUID(...)   # new fixed namespaces, like NAMESPACE_NOTE
NAMESPACE_POST  = UUID(...)

def pulse_id(topic: str, asked_at: datetime) -> UUID   # uuid5, one per run
def post_id(url: str) -> UUID                          # uuid5, canonical on the post URL
```

- **`PulsePost`** — the model's structured output per item:
  - `text: str` (max 300) — the claim or news in one self-contained sentence
  - `author_handle: str` — normalised `@handle`, lowercased
  - `url: str` — link to the source post
  - `section: str | None` (max 40) — 2–4 word grouping label
  - `score: float` (0–1, default 0.5) — worth-remembering signal. Field
    `description=` carries explicit anchors, copying `VideoNote.score`
    (`contracts.py:307`): asked for an unqualified 0–1 score a model rates
    everything 0.9, and a ranking where everything is 0.9 is not a ranking.
- **`PulseDigest`** — `topic`, `summary` (max 600), `posts: list[PulsePost]`,
  `asked_at`, `handles: list[str]` (what was actually filtered on).
- **`PulseArtifact`** — `digest`, `raw_response`, `fetched_at`, `model`,
  `sources_used: int`, `cost_usd: float | None`. Cached to disk.
- **`PulseResult`** — ok/error wrapper mirroring `VideoResult`: `digest`,
  `post_count`, `error`, `dropped: int`, `cost_usd`, `.ok` property.
- **`SourceKind`** gains `"x"` (`contracts.py:50`).
- **`PostView`** — read model: `id`, `text`, `author_handle`, `url`, `section`,
  `score`, `topic`, `asked_at`. `deep_link` returns `url`.

## Graph (`src/vos/graph.py`)

```
(:Pulse {id, topic, asked_at, summary})-[:HAS_POST]->(:Post {id, text, url,
                                                            author_handle, score, section})
(:Post)-[:BY]->(:Entity:Source)      when author_handle matches a followed x source
```

New `SCHEMA` entries alongside the existing ones (`graph.py:38`):

```
CREATE CONSTRAINT pulse_id  ... FOR (p:Pulse) REQUIRE p.id IS UNIQUE
CREATE CONSTRAINT post_id   ... FOR (p:Post)  REQUIRE p.id IS UNIQUE
CREATE FULLTEXT INDEX post_text ... FOR (p:Post) ON EACH [p.text]
```

New methods, all idempotent `MERGE` in the existing style:

- `save_pulse(digest) -> int` — MERGEs the `:Pulse` and each `:Post`. **Merge, not
  replace.** `replace_notes` (`graph.py:492`) deletes first because re-distilling a
  video supersedes its own earlier output; a post recurring in two digests is the
  *same post* and must keep one identity, so `post_id` is keyed on URL.
- `posts_for_pulse(pulse_id)`, `latest_pulse_id()`, `search_posts(term, n)`.
- `follow()` (`graph.py:275`) maps kind `x` to entity type `person`.

## xAI client (`src/vos/pulse.py`, new)

Direct `httpx` call to xAI's OpenAI-compatible `/v1/chat/completions` (httpx is
already a dependency, `pyproject.toml:25`); no new SDK. LangChain is deliberately not
used here — `search_parameters` is an xAI-specific extension, and routing it through
`init_chat_model` would hide the one field that governs cost.

```jsonc
"search_parameters": {
  "mode": "on",
  "sources": [{"type": "x", "x_handles": [...]}],   // omitted when nothing is followed
  "from_date": "<today - 1 day>",
  "max_search_results": 25
}
```

- The prompt requests exactly the `PulseDigest` JSON shape; the response is validated
  with pydantic. One retry with the validation error appended (the pattern
  classification already uses), then honest failure.
- **URL validation.** Each returned item must have a URL matching
  `x.com|twitter.com/<handle>/status/<digits>`. Items failing this are dropped and
  **counted** — `PulseResult.dropped` is surfaced in the reply. Silent truncation is
  the defect this codebase already fixed once for video notes; an unverifiable link
  is worse than a missing one because it looks checkable.
- `sources_used` and `cost_usd` come from the response usage block where xAI provides
  them, falling back to `max_search_results × $0.025` so the budget is never
  under-counted.
- Transport injected behind a small protocol so tests stub it, as `video.py` does.

## Settings, shell, render, CLI

**`settings.py`** — `xai_api_key: SecretStr | None` (alias `XAI_API_KEY`, optional:
absent means the feature is off, not a startup failure), `vos_pulse_model`
(default `grok-4.1-fast`), `vos_pulse_max_sources: int = 25`,
`vos_pulse_topic: str = "AI"`.

**`shell.py`**
- `cmd_pulse` — registered as `Command("pulse")` (`shell.py:403` block). No key →
  setup instructions. Budget exceeded → refuse, naming the spend.
- `_parse_follow` (`shell.py:430`) accepts `x`, normalising `@Karpathy`,
  `karpathy`, and `https://x.com/karpathy` to `@karpathy`.
- `cmd_more` (`shell.py:225`) extends to pulses: bare `/more` targets the most recent
  pulse *or* video, whichever is newer — comparing `Pulse.asked_at` against
  `Video.fetched_at`, derived from the graph exactly as `/undo` derives the last
  thought, with no session state. An explicit `/more <url>` still selects the video.
- `cmd_notes` queries both stores (up to 10 each) and renders them as two labelled
  groups, video notes first, omitting a group that returned nothing.

**`render.py`**
- **Ordering.** A video reply picks by score but *displays* in video order, because
  chronology is the thing the reader is reconstructing. Posts have no such axis, so
  they display by **score descending** within a section, and sections are ordered by
  their highest-scoring post. Score therefore drives both selection and order here —
  the opposite of the video path, and deliberate.
- `_by_section` (`render.py:200`) currently sorts on `.t_seconds`, which a post does
  not have. Generalise it to take a sort key and constrain the type parameter to a
  small `Sectioned` protocol (`section: str | None`), instead of extending the
  `(VideoNote, NoteView)` tuple constraint indefinitely. Video passes
  `lambda n: n.t_seconds`; pulse passes `lambda p: -p.score`.
- `_note_lines` (`render.py:219`) hardcodes YouTube URLs and is **not** reused. New
  `_post_lines` leads each line with the linked `@handle` — the post's equivalent of
  a timestamp deep link, and the thing that makes a claim checkable.
- `render_pulse` reuses `SHOWN_NOTES = 8` and the "Showing the top N of M — /more for
  the rest" footer, plus the digest cost.
- `render_all_posts` for `/more`. `HELP` gains `/pulse` and `/follow x`.

**`cli.py`** — `doctor` gains an xAI line: key present, and under `--live` a
minimal call that resolves the model without invoking search (search costs money;
doctor must not).

## Errors

| Failure | Behaviour |
|---|---|
| No `XAI_API_KEY` | Setup instructions; no crash, no other feature affected |
| Daily budget exceeded | Refuse before spending, report today's spend |
| xAI HTTP/network error | Error reply; nothing written to disk or graph |
| Response invalid after one retry | Error reply; nothing written |
| Some items have unusable URLs | Keep the rest, report the dropped count |
| Zero usable items | Say so plainly; write nothing |

Nothing is written until a digest validates, so there are no half-written pulses.

## Testing

- **`tests/test_pulse.py`** (new) — parsing a stubbed response; retry on invalid
  JSON then success; failure after the second attempt; `x_handles` assembly from
  followed sources; handles omitted when nothing is followed; `from_date` window;
  URL validation and the dropped count; cost fallback when usage is absent.
- **`tests/test_graph.py`** — Pulse/Post round-trip; the same post in two digests
  stays one node with two `HAS_POST` edges; `BY` edge to a followed source;
  `search_posts` finds posts and `search_notes` still returns only video notes
  (the regression the label split exists to prevent). Testcontainers, as today.
- **`tests/test_render.py`** — pulse layout; top-8 by score shown in digest order;
  section grouping; dropped-count and cost footers; `render_all_posts`; a long
  digest still splits under `TELEGRAM_LIMIT`.
- **`tests/test_shell.py`** — `/pulse` happy path and missing-key path via fakes;
  `/follow x` handle normalisation; `/more` choosing pulse over video by recency.
- **One cassette** recording a real xAI response, so the parser is tested against
  reality rather than only against my idea of the response shape.

## Verification

```
uv run ruff check src tests
uv run pytest -q
```

Then live, once `XAI_API_KEY` is set:

```
docker compose up -d neo4j
uv run vos doctor --live          # xAI key resolves; no search spend
uv run vos-bot
```

In Telegram: `/follow x @karpathy` → `/following` shows it → `/pulse` → check the
digest is grouped, scored, and that tapping a handle opens the real post → `/more`
lists everything → `/notes <term>` returns both posts and video notes → `/stats`
shows the spend including the digest.

## Risks

- **Cost.** ~$0.63 per digest against a $2.00 daily budget. Mitigated by the budget
  refusal and by `vos_pulse_max_sources` being a setting, but the user should watch
  `/stats` for the first week.
- **Grok's curation is opaque.** "Best" is its judgment, not a measurable ranking.
  The scores are the model's self-assessment, not engagement data.
- **URL fabrication.** The validation regex catches malformed links but cannot prove
  a well-formed link resolves. Tapping through remains the check.
- **`search_parameters` is an xAI extension** and may change shape; the transport is
  isolated in `pulse.py` so a change is one module.
- **The default model id is a guess.** `grok-4.1-fast` is what current docs suggest
  supports Live Search, but xAI renames models often. It is a setting, and
  `vos doctor --live` exists to catch a wrong name before `/pulse` does.
