# VOS v2 — Software Architecture

**Phase 1: Telegram capture into a knowledge graph**
Status: proposed · Author: architecture review · Date: 2026-08-07

---

## 1. Context

VOS v1 reached Epic 0 and Epic 1 code-complete and then stalled. Everything it was built from —
LangGraph kernel, Slack shell, NATS bus, syscall gate, Graphiti/Neo4j memory, self-hosted Docker —
is discarded. *(Slack is named only because it was v1's surface; this system is Telegram-only.)*
`D:\VOS` is empty and is not a git repository; this is a clean restart.

Three causes of the v1 failure, taken as design constraints rather than history:

| Cause | Constraint it imposes |
|---|---|
| The ground shifted under a mid-2026 architecture | Prefer boring, replaceable components; isolate anything vendor-specific behind a seam |
| The purpose was never pinned | Exactly one user-visible job in Phase 1; everything else is explicitly out |
| **Nothing to test, no quick feedback** | The system must be usable end-to-end on day one, and every LLM interaction must be replayable offline |

### 1.1 The job

Send a thought to Telegram. It is captured durably, classified, and the knowledge graph grows. A
reply names where it landed and what it connected to. Separately, declare the people, books, and
channels you follow, so the graph knows what shapes your thinking.

### 1.2 Why this is the right first system

It establishes the spine everything later hangs off: **an item arrives, is classified, and is routed
to a processor.** Capture is the lightest possible processor; a deep research errand is the heaviest.
The category is the routing decision. This is the front door to the OS, not a side project.

---

## 2. Goals and non-goals

**Goals**

- G1 — Zero thought loss. A message that reaches the bot is durably recorded before anything else can fail.
- G2 — Useful on day one, with classification quality good enough to trust within a week of real use.
- G3 — Every model interaction replayable offline, so tuning costs seconds and no API spend.
- G4 — Reclassification of the entire history is possible after a prompt change.
- G5 — Model-agnostic: swapping LLM provider is a configuration change.
- G6 — Portable from dev machine to a VPS with no code change.

**Non-goals (Phase 1)**

Voice input · processors of any kind · the research errand runner · proactive behaviour · any action
in the world · credentials or payments · message bus · tracing stack (Langfuse, Logfire, OTel) ·
multi-user · high availability. All of v1's Epic 0 is gone; ElevenLabs is dropped entirely.

*On observability:* v1 shipped a proven obs stack and died anyway. Tracing does not create value; it
makes existing value debuggable. The cassette log (§7.3) plus the Neo4j browser is the Phase 1 story.
A tracer earns its place when one request produces a multi-step trace worth visualising.

---

## 3. Quality attributes

Numbers are targets, not guesses about load — this is a single-user system at roughly 10–50 thoughts
per day. They exist so "is it working?" has an answer.

| Attribute | Target | Mechanism |
|---|---|---|
| Durability | No captured thought is ever lost, including on classifier or database failure | Write-ahead journal, fsync before ack (§4.1) |
| Capture latency | Ack ≤ 1 s from message receipt | Ack fires after journal write, before classification |
| Enrichment latency | p95 ≤ 10 s to classified reply | Single model call |
| Correctness | Re-delivery of the same Telegram update never duplicates a node | Deterministic IDs + `MERGE` (§9.1) |
| Cost | ≤ $0.01 per thought | One call; prompt-cached prefix |
| Recoverability | Full graph rebuildable from the journal alone | Graph is a projection (§4.1) |
| Testability | Full pipeline runs offline in seconds, no network | Injected model + fake transport (§12) |

---

## 4. Architectural principles

Three principles carry most of the design. Everything below follows from them.

### 4.1 The journal is the source of truth; the graph is a projection

Raw captured text is appended to an immutable, append-only journal *before* any classification is
attempted. Neo4j holds a **derived** view built from that journal.

This single decision buys four properties that would otherwise each need their own machinery:

- **Durability is independent of the LLM and the database.** Classification failure, an API outage, a
  Neo4j container that won't start — none can lose a thought.
- **Reclassification is free.** When the prompt improves or a category is added, replay the journal
  and rebuild. Your first twenty thoughts get the benefit of everything you learn from them.
- **Backup is copying a file.** The graph is disposable.
- **The classifier can be swapped and re-evaluated against real history** (§13.3), which is what makes
  G5 real rather than nominal.

The cost is one extra write per thought and a rebuild command. That is a very cheap price.

### 4.2 Capture and enrichment are separate failure domains

Capture must never depend on enrichment succeeding. A thought whose classification fails is stored
with `status = unclassified` and surfaced by `/pending`; it is retried, never dropped. A user whose
LLM provider is down still captures thoughts all day and enriches them later.

This is a correction to the obvious design (`classify → write`), which makes an LLM call load-bearing
for data safety.

### 4.3 Every seam that will be crossed later is a protocol today

Voice input, non-Claude models, and processors are all coming. Each gets a named protocol in Phase 1
with exactly one implementation (or none). This costs a few lines now and prevents a refactor later.
It also makes the test suite trivial, which is the real payoff.

---

## 5. System overview

```
              ┌──────────────────────────────────────────────┐
   Telegram   │                  vos.shell                   │
  ──────────► │   aiogram · long-polling · command router    │
   (message)  └───────┬──────────────────────────────┬───────┘
                      │ 1. append (fsync)            │ 5. reply
                      ▼                              │
              ┌───────────────┐                      │
              │  vos.journal  │  append-only JSONL    │
              │  SOURCE OF    │  ◄─── rebuild ────┐   │
              │    TRUTH      │                   │   │
              └───────┬───────┘                   │   │
                      │ 2. enqueue                │   │
                      ▼                           │   │
              ┌──────────────────────────┐        │   │
              │      vos.pipeline        │        │   │
              │  LangGraph · one node    │        │   │
              │  Pydantic state schema   │        │   │
              └───────┬──────────────────┘        │   │
                      │ 3. init_chat_model(VOS_MODEL)  │
                      ▼                           │   │
              ┌──────────────────────────┐        │   │
              │   any LLM provider       │        │   │
              │   + vos.cassette (log)   │        │   │
              └───────┬──────────────────┘        │   │
                      │ 4. upsert                 │   │
                      ▼                           │   │
              ┌──────────────────────────┐        │   │
              │   vos.graph — Neo4j      │────────┘   │
              │   PROJECTION (derived)   │            │
              └───────┬──────────────────┘            │
                      └──── vos.render ───────────────┘
```

**Control flow is strictly linear and single-writer.** aiogram processes updates for a chat in order;
there is no concurrency to reason about in Phase 1. This is a deliberate simplification that removes
an entire class of graph write races (§9.2), and it is revisited only when processors introduce
genuinely parallel work.

---

## 6. Component model and interfaces

Modules communicate only through the contracts in `vos.contracts`. No module imports another's
internals. Async throughout — one event loop shared by aiogram, the Neo4j driver, and the model client.

### 6.1 `vos.contracts`

Pydantic v2 models. Written first; no dependencies on any other module.

```python
Category = Literal["Shopping", "TripPlanning", "Family", "Career",
                   "StudyResearch", "StockResearch", "VideoKnowledge", "Other"]
EntityType = Literal["person", "place", "product", "org", "ticker", "topic", "url"]
SourceKind = Literal["person", "book", "channel"]
Status     = Literal["captured", "classified", "unclassified", "deleted"]

class CaptureRecord(BaseModel):      # the journal record — immutable, append-only
    id: UUID                         # deterministic: uuid5(NS, f"{chat_id}:{message_id}")
    chat_id: int
    message_id: int
    text: str
    source: Literal["text", "voice"] = "text"
    transcript: str | None = None
    captured_at: datetime

class ExtractedEntity(BaseModel):
    name: str
    type: EntityType
    salience: float = Field(ge=0.0, le=1.0)

class Classification(BaseModel):     # the model's structured output
    category: Category
    title: str = Field(max_length=120)
    summary: str = Field(max_length=500)
    entities: list[ExtractedEntity] = []
    confidence: float = Field(ge=0.0, le=1.0)

class SourceRef(BaseModel):          # a declared follow
    name: str
    kind: SourceKind
    url: str | None = None
    author: str | None = None

class CaptureResult(BaseModel):      # what the user is told
    record: CaptureRecord
    classification: Classification | None
    linked_sources: list[str] = []
    status: Status
    error: str | None = None
```

Also declares the seam protocols (§4.3):

```python
class Transcriber(Protocol):                       # no implementation in Phase 1
    async def transcribe(self, audio: bytes, mime: str) -> str: ...

class Journal(Protocol):
    async def append(self, record: CaptureRecord) -> None: ...
    def read_all(self) -> Iterator[CaptureRecord]: ...

class GraphStore(Protocol):
    async def upsert_thought(self, r: CaptureRecord, c: Classification | None) -> None: ...
    async def recent(self, n: int) -> list[ThoughtView]: ...
    async def by_category(self, c: Category, n: int) -> list[ThoughtView]: ...
    async def search(self, term: str, n: int, *, match: MatchMode) -> list[ThoughtView]: ...
    async def delete_thought(self, id: UUID) -> None: ...
    async def stats(self) -> GraphStats: ...
    async def follow(self, s: SourceRef) -> None: ...
    async def unfollow(self, name: str) -> None: ...
    async def following(self) -> list[SourceRef]: ...
    async def pending(self) -> list[UUID]: ...      # status == unclassified
```

### 6.2 `vos.journal` — durable capture

Append-only JSONL, one file per month (`journal/2026-08.jsonl`), one JSON object per line.

`append()` writes the line, then `flush()` + `os.fsync()` before returning. **The shell does not ack
until this returns.** That is the entire durability guarantee, and it is worth the syscall.

Chosen over SQLite because the requirements are append, scan, and copy — nothing that wants a query
planner. It is greppable, diffable, trivially backed up, and survives partial writes (a torn final
line is discarded on read with a warning; every earlier line remains valid).

### 6.3 `vos.pipeline` — LangGraph, one node

Named `pipeline`, **not** `graph` — `vos.graph` is Neo4j, and two things called "graph" in one
codebase is a readability trap worth avoiding up front.

A `StateGraph` over a Pydantic state schema with a single `analyze` node. LangGraph validates the
state model before each node executes, so the contract is enforced rather than advisory, and adding
processors later means adding nodes rather than restructuring.

```python
class ThoughtState(BaseModel):
    record: CaptureRecord
    followed: list[SourceRef] = []
    classification: Classification | None = None
    error: str | None = None
```

Inside the node:

```python
model = init_chat_model(settings.vos_model)          # "anthropic:claude-opus-5"
result = await model.with_structured_output(Classification).ainvoke(prompt)
```

`VOS_MODEL` is one environment variable; `anthropic:claude-opus-5` → `openai:…` → `ollama:…` is a
config change, and `with_structured_output()` returns a validated Pydantic instance on every provider.

**Default model `claude-opus-5`** ($5 / $25 per MTok). This is a small, high-frequency call, so it is
the one place a cheaper tier is genuinely worth considering — `claude-haiku-4-5` is $1 / $5. With the
config seam it is a one-line experiment.

**Dependency rule:** `langgraph` + `langchain-core` + `langchain` + one provider adapter per model
you actually intend to run. Nothing else.

`langchain` is present only for `init_chat_model`. The "avoid the mega-package" instinct is about
LangChain **0.x**; in 1.x it is a thin layer over `langchain-core` and `langgraph` and resolves to a
single extra wheel (measured: adding it installs one package). What stays out is unchanged: chains,
document loaders, legacy agents, and adapters for providers you do not actually run.

Adapters are added one at a time, and their cost is measured before adoption rather than assumed.
`langchain-anthropic` is nearly free; `langchain-google-genai` pulls nine wheels because
`google-genai` brings Google's auth and crypto chain (`cryptography`, `google-auth`, `pyasn1`). That
is a fair price for a second provider, and it is the reason the rule is "per model you actually
intend to run" rather than "install them all".

**Provider keys reach the SDK through the environment, not through `Settings`.** Every provider
adapter reads its own conventionally-named variable (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, …), so
`vos.settings` loads `.env` into `os.environ` and never names a key itself — adding a provider stays
a config change. Real environment variables win over `.env`, which is what a VPS or CI run expects.

**Prompt construction** is cache-aware: stable system prompt and follow list first, thought text last,
so the prefix caches (minimum cacheable prefix on Opus 5 is 512 tokens). The follow list is passed
into the call so entity extraction resolves to canonical nodes you already declared instead of
creating near-duplicates — the cheapest possible entity resolution, and what makes replies feel like
the system knows what you care about.

### 6.4 `vos.graph` — Neo4j projection

Neo4j Python driver 6.2, async (`AsyncGraphDatabase.driver`). All writes are `MERGE`-based and
idempotent. Constraints and indexes are created on startup (§7.2).

### 6.5 `vos.shell` — Telegram gateway

aiogram 3.x, long-polling (`getUpdates`). Async-native since v3, so it shares one event loop with the
Neo4j and model clients rather than bridging thread pools, and it provides command routing, polling
retries, and an FSM for multi-step `/follow` and confirmation flows.

**No public URL, no webhook, no tunnel** — identical behaviour on the dev machine and on a VPS later.

| Command | Behaviour |
|---|---|
| *(plain text)* | Capture a thought |
| *(voice note)* | Explicit "voice isn't wired up yet" reply — never a silent drop |
| `/recent [n]` | Last *n* thoughts |
| `/category <name>` | Thoughts in a category |
| `/search <term>` | Thoughts containing every word; widens to any word, labelled, if that finds nothing |
| `/undo` | Soft-delete the last thought |
| `/stats` | Counts per category + most-referenced sources |
| `/pending` | Thoughts that failed classification, with retry |
| `/follow person\|book\|channel <…>` | Declare a followed source |
| `/following` · `/unfollow <name>` | List / remove |

`/undo` and `/pending` matter more than they look: capture must be correctable and recoverable in one
step, or trust erodes and daily use stops — and this whole system depends on daily use.

### 6.6 `vos.cassette` — replay log

Records every model call (rendered prompt, raw response, model ID, token counts, cost, latency) to
`cassettes/`, keyed by `CaptureRecord.id`. Replay mode serves from disk and **fails loudly on a cache
miss**, so a replay can never silently reach the network.

### 6.7 `vos.render`

`CaptureResult` → the Telegram confirmation string. Separated so a second client can reuse the same
result object with different rendering later.

### 6.8 `vos.shopping` — the shopping list projection

A small SQLite store (`items`, `adds`, `extractions`) behind the `ShoppingStore` protocol. Sibling to
`vos.graph` rather than a layer on it: both are projections of the journal, both are wiped and
replayed by `--rebuild`, and neither is a source of truth. Stdlib `sqlite3` on a worker thread, with
the same lock-and-`to_thread` shape as `JsonlJournal.append`, so it adds no dependency.

Every state transition is gated on a timestamp, which makes `add` and `mark` commutative and lets
replay arrive in any order. See ADR-012, ADR-013, and `docs/pipelines/shopping.md`.

---

## 7. Data architecture

### 7.1 Graph schema

```cypher
(:Thought {id, text, title, summary, source, transcript,
           status, confidence, created_at})
   -[:IN_CATEGORY]->  (:Category {name})
   -[:MENTIONS {salience}]-> (:Entity {canonical_name, name, type})
   -[:NEXT]->          (:Thought)      // temporal thread within a category

(:Entity)-[:RELATES_TO {via_thought_id}]->(:Entity)

(:Entity:Source {kind, url, author, followed_at, note})
```

**Followed sources are entities, not a parallel structure.** Declaring one adds a `:Source` label and
follow metadata to an ordinary `:Entity`. When a thought mentions someone you follow, the normal
`MENTIONS` edge lands on that same node — no new relationship type, no duplicate node, and "which of
my thoughts trace back to what I follow" is one query. This is the graph choice paying for itself on
the first feature.

`source` and `transcript` exist from day one though Phase 1 only writes `source='text'`, so adding
voice needs no migration.

**Categories** are seeded exactly as specified: `Shopping`, `TripPlanning`, `Family`, `Career`,
`StudyResearch`, `StockResearch`, `VideoKnowledge`, `Other`. Seeding happens in `ensure_schema()`, so
all eight exist from the first startup whether or not anything has been filed in them — an empty
category is a real thing containing nothing, not a category that does not exist. `Other` is an honest
fallback: a low-confidence thought is filed there and flagged, never forced into a wrong bucket.
Those flagged thoughts are the signal for which category to add next.

**The shopping list is not in the graph.** Thoughts filed under `Shopping` project exactly like any
other thought, entities included; the list of things to buy lives in SQLite instead (ADR-012, and
`docs/pipelines/shopping.md`).

**Entity resolution** in Phase 1 is case-insensitive canonical-name matching, with declared sources
taking priority. Deliberately dumb, and easy to replace once you can see where it is wrong.

### 7.2 Constraints and indexes (created on startup)

```cypher
CREATE CONSTRAINT thought_id   IF NOT EXISTS FOR (t:Thought)  REQUIRE t.id IS UNIQUE;
CREATE CONSTRAINT category_nm  IF NOT EXISTS FOR (c:Category) REQUIRE c.name IS UNIQUE;
CREATE CONSTRAINT entity_canon IF NOT EXISTS FOR (e:Entity)   REQUIRE e.canonical_name IS UNIQUE;
CREATE INDEX     thought_time  IF NOT EXISTS FOR (t:Thought)  ON (t.created_at);
CREATE INDEX     thought_stat  IF NOT EXISTS FOR (t:Thought)  ON (t.status);
CREATE FULLTEXT INDEX thought_search IF NOT EXISTS
  FOR (t:Thought) ON EACH [t.text, t.title, t.summary]
  OPTIONS {indexConfig: {`fulltext.analyzer`: 'english'}};
```

The uniqueness constraints are what make `MERGE` idempotent under retry; they are correctness
infrastructure, not tuning.

**The analyzer is load-bearing.** Neo4j's default, `standard-no-stop-words`, filters no stop
words, so `to`, `a` and `the` were ordinary search terms — and since `queryNodes` OR-s terms,
any phrase containing one matched most of the graph. `english` filters Lucene's standard
English stop words and stems, so `banana` finds "bananas". An analyzer cannot be changed in
place and `IF NOT EXISTS` will not replace an existing index, so `SCHEMA` drops the older
`thought_text` / `note_text` / `post_text` names before creating these; new names are what
make that idempotent across restarts. Nothing is lost — a fulltext index is derived state,
repopulated from the nodes on creation. `ensure_schema` then waits on `db.awaitIndexes`,
because population is asynchronous and a search against a half-built index returns a wrong
answer that looks like a real one.

The other half of the fix lives in `graph._lucene_query`: `queryNodes` takes a Lucene query
*language*, not a phrase, so the user's words are escaped and each is marked required. Passing
the term through raw meant `*` matched everything and `(` raised out of the handler.

### 7.3 Storage layout

| Path | Contents | Backup | Rebuildable |
|---|---|---|---|
| `journal/YYYY-MM.jsonl` | **Source of truth** — raw captures and user actions | **Yes — this is the backup** | No |
| Neo4j volume | Derived projection | Optional | Yes, from journal |
| `shopping.db` | Derived projection — the shopping list | No | Yes, from journal |
| `artifacts/` | Fetched transcripts and pulse digests | Yes — not recomputable | No |
| `cassettes/` | Model call log | Optional (useful for eval) | No, but non-critical |

Backup policy is therefore: copy `journal/` and `artifacts/`. Everything else
regenerates — including the shopping list, whose ticks replay from `item_mark` entries
and whose items are re-extracted in the background on the next start (ADR-012, ADR-013).

---

## 8. Key flows

### 8.1 Capture (happy path)

```
1. aiogram receives update (update_id, chat_id, message_id, text)
2. id ← uuid5(NAMESPACE_VOS, f"{chat_id}:{message_id}")        deterministic
3. journal.append(CaptureRecord)  →  fsync                     ← DURABILITY BOUNDARY
4. graph.upsert_thought(record, classification=None)           status = captured
5. reply "Captured." if step 6 is slow                         (ack ≤ 1 s)
6. pipeline.ainvoke(ThoughtState(record, followed))            model call, logged to cassette
7. graph.upsert_thought(record, classification)                status = classified
8. reply "Filed under Trip planning · linked: Japan, October, flights"
```

Steps 1–3 are the contract. Everything after is enrichment and may fail.

### 8.2 Classification failure

Step 6 raises (API down, malformed output, budget exceeded) → `status = unclassified`, the thought
is already in the journal and the graph, and the user is told plainly: *"Captured, but I couldn't
classify it — it's in /pending."* `/pending` retries. No data is lost and nothing is silently wrong.

### 8.3 Undo

`/undo` soft-deletes: `status = deleted` on the node, and a tombstone record appended to the journal.
The original capture line is never edited — the journal stays append-only, which is what makes replay
deterministic.

### 8.4 Reclassify (the payoff of §4.1)

```
vos reclassify [--from 2026-08-01] [--category Other] [--model openai:gpt-…]
```

Streams the journal, re-runs the pipeline, rebuilds the projection. Used after a prompt change, when
adding a category, or to evaluate a different model against real history. `--dry-run` writes to a
scratch database and prints a diff of category assignments, so you see what would change before it
changes.

---

## 9. Cross-cutting concerns

### 9.1 Idempotency

Telegram may redeliver an update; the process may crash between journal and graph writes. Both are
handled by construction:

- `CaptureRecord.id = uuid5(NAMESPACE_VOS, f"{chat_id}:{message_id}")` — deterministic, so a retry
  produces the same ID rather than a second thought.
- Every graph write is `MERGE` on that ID under a uniqueness constraint.
- Journal append is idempotent at read time: duplicate IDs collapse, last write wins.

Net effect: **at-least-once delivery from Telegram yields exactly-once capture.**

### 9.2 Consistency

Journal and graph are eventually consistent by design; the journal leads. On startup the shell scans
the journal tail against the graph and re-projects anything missing, which closes the crash window
between steps 3 and 4. Single-writer processing means no write races on `NEXT` edges.

### 9.3 Secrets

`.env`, git-ignored, `.env.example` committed with keys but no values. `TELEGRAM_BOT_TOKEN`,
`NEO4J_PASSWORD` (compose derives `NEO4J_AUTH` from it, so the secret has one copy), `VOS_MODEL`,
and one `<PROVIDER>_API_KEY`. No secret is ever written to the journal, the cassettes, or a log line;
`SecretStr` on every credential field is what enforces that against accidental interpolation. Neo4j
is bound to localhost and is not exposed by the compose file.

VOS reads `<PROVIDER>_API_KEY` values but never declares them — `.env` is loaded into the
environment and the adapter picks up its own key. That keeps provider-agnosticism honest, at the
cost that a key typo surfaces as a classifier auth failure in `/pending` rather than at startup.

### 9.4 Cost control

One model call per thought, prompt-cached prefix, and a configurable daily ceiling
(`VOS_DAILY_BUDGET_USD`). On breach, capture continues and classification is deferred to `/pending` —
degradation, never data loss. Per-call cost is recorded in the cassette, so `/stats` can report real
spend rather than an estimate.

### 9.5 Failure modes

| Failure | Detection | Response | Data loss |
|---|---|---|---|
| Telegram polling drops | aiogram reconnect | Automatic backoff and resume; `update_id` offset prevents gaps | None |
| Disk full on journal append | `OSError` on write | **Refuse to ack.** The user sees an explicit failure and can retry | None |
| Neo4j down | Driver error | Capture proceeds; projection retried on startup | None |
| LLM provider down / rate-limited | Exception in node | `status=unclassified` → `/pending` | None |
| Malformed model output | Pydantic validation | Same as above; raw response kept in cassette for diagnosis | None |
| Budget exceeded | Pre-call check | Classification deferred, capture unaffected | None |
| Torn final journal line (hard kill) | JSON parse error on read | Discard that line, warn; all earlier lines valid | Last in-flight thought only, un-acked |
| Bad prompt change degrades quality | `/stats`, `/pending` volume, your own judgement | `vos reclassify --dry-run`, then rebuild | None |

The column that matters is the last one.

---

## 10. Architecture decision records

| # | Decision | Alternatives rejected | Rationale |
|---|---|---|---|
| **001** | **Journal is source of truth; graph is a projection** | Graph as sole store | Durability independent of LLM and DB; free reclassification; backup is a file copy. Cost is one extra write |
| **002** | **Neo4j, own Cypher schema, no Graphiti** | Graphiti; embedded Kuzu; relational | Graph is a decided requirement. Graphiti's bi-temporal reasoning and auto entity-resolution buy nothing at "categorise a thought" and are a large layer to carry. Neo4j Browser at `localhost:7474` gives visible growth for free — motivationally load-bearing given cause #3 |
| **003** | **Telegram, long-polling** | WhatsApp; Slack | WhatsApp Business API needs a BSP and pre-approved templates outside a 24 h window, breaking delayed replies. Long-polling needs no public URL, so dev-machine and VPS behave identically |
| **004** | **Text-only Phase 1; `Transcriber` protocol defined, unimplemented** | ElevenLabs Scribe; local faster-whisper | Provider undecided; Telegram's native transcription is MTProto-only and unavailable to bots. The seam means voice is an adapter, not a refactor |
| **005** | **LangGraph with one node, Pydantic state** | Plain function; Pydantic AI; hand-rolled | State validation before each node; processors become nodes rather than a restructure. Held to `langchain-core` + `langchain` (thin in 1.x — one wheel, needed for `init_chat_model`) + adapters. No chains, loaders, or legacy agents |
| **006** | **`init_chat_model()` for provider-agnostic model access** | Anthropic SDK direct; LiteLLM; custom protocol | Any-LLM is a hard requirement, and this is the native mechanism in the chosen stack. The Anthropic SDK is Claude-only |
| **007** | **Cassettes only; no tracing stack** | Langfuse; Logfire; OTel | v1 shipped a proven obs stack and still died. Cassettes give replay *and* eval; a tracer earns its place when one request yields a multi-step trace |
| **008** | **Single-writer, sequential processing** | Worker pool; queue | Removes an entire class of write races at ~50 thoughts/day. Revisit when processors introduce real parallelism |
| **009** | **Followed sources are `:Entity:Source`, not a separate node type** | Parallel `:Source` hierarchy | Mentions land on the same node as declarations with no extra relationship type; "thoughts tracing back to what I follow" is one query |
| **010** | **X pulse digests are cached artifacts, not a third journal entry type** | A `PulseDigest` variant on `JournalEntry` | The journal is a closed `capture \| tombstone` union (§4.1, §9.1) — it holds what VOS itself captured. A digest is Grok's answer at a moment in time, fetched rather than authored, and re-asking costs real money — the same shape as a video transcript (§`docs/pipelines/video-knowledge.md`). It is cached to `artifacts/pulses/` and treated as a non-recomputable input, same backup rule as transcripts, and the journal stays two variants |
| **011** | **Posts get their own `:Post` label, not `:Note` reused** | Store pulse items as `:Note` | `:Note` means a claim distilled from a video transcript, timestamped to the second it was said. A post is an external item fetched from X, scored and sectioned differently and with no `t_seconds`. Reusing `:Note` would force one label to carry two unrelated meanings and one fulltext index to serve two search intents (`/notes` vs `/pulse` history); `:Post` gets its own uniqueness constraint and fulltext index instead |
| **012** | **Shopping list state lives in SQLite; the graph holds none of it** | `:Entity:Item` label reuse (the ADR-009 pattern); a separate `:Item` node type | A shopping list is a handful of rows that flip between two values and are read back in one order. As nodes that is volume with no traversal benefiting from it, and the graph's job is to stay worth looking at. SQLite is a projection in exactly the sense Neo4j is — every row rederivable from the journal, wiped and replayed by `--rebuild` — so this adds a second *derived* store, not a second source of truth. Stdlib `sqlite3` on a worker thread, so no new dependency (ADR-005) |
| **013** | **A shopping tick is a third journal entry kind (`item_mark`)** | Bought-state only in the projection | Refines ADR-010. That ADR's closed-union argument is about *fetched* content: a digest is Grok's answer at a moment in time, authored elsewhere. Ticking an item off is a decision the user made, which is the same category as a `Tombstone` — and `/undo` set the precedent. The concrete cost of the alternative is that `vos reclassify --rebuild`, an ordinary operation here, would silently reset the list to pending and the user would find out in a shop. The rule the journal actually holds is therefore "what VOS's user authored", not "exactly two variants" |
| **014** | **Kitchen kiosk is co-hosted inside the daemon, not a second service** | Standalone FastAPI container; a public webhook | One process keeps ADR-008 true with two transports live: FastAPI handlers run concurrently with aiogram handlers, so every kiosk graph write goes through the same JobQueue worker, and the journal keeps one writer. A second service would need an invented internal API purely to share the journal safely. uvicorn runs with signal capture disabled — left alone it replaces the SIGINT handler and Ctrl+C stops reaching aiogram. The `kiosk` extra + lazy imports keep the daemon installable and runnable without any of it. Resolves ADR-004's open provider question: local faster-whisper behind the existing `Transcriber` protocol — an adapter, exactly as the seam intended |
| **015** | **Kiosk exposure is tailnet-only; no public endpoint of any kind** | ngrok; Cloudflare Tunnel; port forwarding + auth; LAN binding | Tablet WiFi and server Ethernet are separate internet connections, so *some* tunnel is unavoidable. Public tunnels invert the privacy requirement: anyone can reach the endpoint, and the provider proxies (ngrok) or TLS-terminates (Cloudflare) family transcripts. Tailscale does the same NAT traversal with no public endpoint, end-to-end WireGuard, and `tailscale serve` provides real HTTPS — which is what satisfies Chrome's secure-context requirement for `getUserMedia` without flag hacks. The app binds 127.0.0.1, reachable from no physical network; chat history is process-memory only, so the surface holds no data at rest |

---

## 11. Deployment and operations

```
docker-compose.yml
  neo4j:  neo4j:2026.06.0-community ports 7474 (browser), 7687 (bolt)   volume: neo4j-data
  app:    python:3.13-slim          volumes: ./journal, ./cassettes     env_file: .env
```

Neo4j moved to **calendar versioning** — there is no "5.x latest" any more; the v1-era scheme is gone.

Runs on the dev machine now. The move to a VPS is `docker compose up` elsewhere plus copying
`journal/` — no code change, because long-polling needs no inbound network and nothing assumes a local
path outside the two mounted volumes.

**Operational runbook**

| Task | Command |
|---|---|
| Start | `docker compose up -d` |
| Inspect graph | `localhost:7474` |
| Backup | copy `journal/` |
| Restore / rebuild | `vos reclassify --rebuild` |
| Retry failures | `/pending` in Telegram |
| Change model | edit `VOS_MODEL`, restart |

---

## 12. Testing strategy

Testable from day one — the direct answer to cause #3.

| Layer | Approach |
|---|---|
| Contracts | Round-trip and validation tests on every Pydantic model |
| Journal | Append/read/replay; **torn-line recovery**; fsync called before return |
| Pipeline | Injected fake chat model — full pipeline offline in seconds, no network, no spend |
| Graph | Real ephemeral Neo4j (testcontainers); assert re-capture creates no duplicate nodes or edges |
| Shell | Fake Bot API transport; command routing and the voice-not-supported path |
| Idempotency | Same update delivered twice → one `:Thought`; crash between journal and graph → startup re-projects |
| Cross-provider | Replay a cassette against a second `VOS_MODEL` and diff category assignments |

That last row is what makes G5 real: `with_structured_output()` is uniform in *interface*, not in
*mechanism* — some providers back it with native JSON-schema output, others with function-calling.
A prompt tuned on one model will not automatically hold accuracy on another. Swapping is a config
change; swapping *safely* requires this check.

**Stack, verified against current releases 2026-08-07**

| Piece | Version | Note |
|---|---|---|
| Python | 3.13 | 3.14 is stable (free-threading GA, PEP 779) but buys nothing for async I/O |
| Neo4j server | 2026.06.0-community | Calendar versioning. Verified against the registry — there is no 2026.07; 2026.06.0 is the newest calendar release |
| Neo4j driver | 6.2, async | Supports 3.13 and 3.14 |
| Telegram | aiogram 3.x | Async-native |
| Orchestration | langgraph 1.x | Pulls `langchain-core`; pin at install |
| Model layer | `langchain-anthropic`, `langchain-google-genai` 4.3 (+ adapters as needed) | Thin wrappers over vendor SDKs. Prefixes: `anthropic:`, `google_genai:` |
| Rest | Pydantic v2, `httpx`, `uv`, `pytest` + `pytest-asyncio`, `ruff` | |

*Windows note:* async event-loop policy bit you in v1 (`WindowsSelectorEventLoopPolicy` for psycopg).
The async Neo4j and httpx clients don't need it — set nothing unless a specific failure demands it.

---

## 13. Evolution

Extension points are designed in, not retrofitted.

| Next capability | What it uses | New work |
|---|---|---|
| **Voice input** | `Transcriber` protocol; `source`/`transcript` fields already in schema | One adapter + a provider decision |
| **Ask the graph back** | `GraphStore` read methods | A retrieval node + query intent detection |
| **Processors, one category at a time** | LangGraph nodes; category is the routing key | One node per processor, conditional edge |
| **Ingest followed channels** (`VideoKnowledge` made real) | `:Source` nodes already declared | Fetcher + distiller; introduces genuine parallelism, so revisit ADR-008 |
| **Research errand runner** | Heaviest processor; `StockResearch` / `StudyResearch` thoughts become errands | Multi-node graph, checkpointing, budget ceilings |
| **VPS move** | Nothing assumes the dev machine | Copy `journal/`, `compose up` |

---

## 14. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Classification accuracy too low to trust | Medium | **Fatal** — daily use stops | Cassette replay for fast prompt iteration; `Other` + `/pending` make errors visible rather than silent; reclassify makes fixes retroactive |
| Framework creep returns (v1's failure) | Medium | High | ADR-005's dependency rule is explicit and testable: no chains, loaders, or legacy agents, and no provider adapter for a model you don't run |
| Graph adds ceremony without payoff | Low | Medium | ADR-009 makes the graph earn its place on the first feature; if it doesn't, the journal means the projection is disposable |
| Scope creep into processors before capture is trusted | **High** | High | Phase 1 done is defined below and gated on real use, not on code completion |
| Neo4j operational burden on the dev machine | Low | Low | Single container; graph is rebuildable, so a corrupt volume is an inconvenience not an incident |

---

## 15. Phase 1: definition of done

Not "the code is written" — v1 was code-complete twice.

1. Declare a handful of people, books, and channels you follow.
2. Send a thought from your phone. Get a reply naming the category and linked entities.
3. Open `localhost:7474` and see the thought connected to entities you recognise, and where relevant
   to a source you declared.
4. **Send twenty real thoughts across a week.** If the categories are right often enough that you
   trust it — and `/undo` and `/pending` caught the times it wasn't — Phase 1 is done.

Criterion 4 is the only one that matters. The rest is prerequisite.
