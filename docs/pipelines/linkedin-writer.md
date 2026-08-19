# Pipeline: LinkedIn writer

**Status:** implemented
**Command:** `/write <keyword>, <keyword>, …`

---

## 1. What it does

```
you: /write ai agents, evals
vos: ✍️ Reading X on ai agents, evals…
vos: ✍️ Writing…
vos: ✍️ ai agents, evals

     ┌────────────────────────────────────┐
     │ Evals are mostly theatre.          │
     │                                    │
     │ ...                                │
     │                                    │
     │ #ai #evals                         │
     └────────────────────────────────────┘

     1,847 characters
     🧵 Grounded in: evals are mostly theatre
     Sources: 9 post(s) on X, 3 of your own note(s)
     💸 $0.22
```

You copy it, edit it, post it. VOS never posts anything anywhere.

## 2. Why it is more than a prompt

Anyone can ask a chatbot for a LinkedIn post. What comes back is competent and
forgettable, because it contains nothing only you could say — and that, not sentence
rhythm or word choice, is what makes writing read as generated.

This pipeline has something a chatbot does not: your journal. The same keywords search
**your own captured thoughts** alongside X, and the draft is built on those. The X
material supplies timing and evidence — what is being argued this week, by whom — while
the substance comes from things you already thought.

That is also why the reply says what it was grounded in. A claim you can check is
different from a claim you have to trust.

## 3. Where the state lives

| Store | What it holds | Rebuildable |
|---|---|---|
| Journal | **Nothing** — a draft is generated, not authored by you (ADR-010) | — |
| `artifacts/pulses/` | The X digest, cached by `run_pulse` | No — re-asking costs money |
| `artifacts/drafts/` | The draft, its keywords, its sources, its cost | No — same reasoning |
| Neo4j | The sourced X posts, via `run_pulse` | Yes, from the artifact |

Because `/write` runs a real pulse, the posts it read land in the graph, so `/more` and
`/notes` reach the source material afterwards for free.

## 4. Pipeline shape

```
/write ai agents, evals
  └─ budget exceeded? → refuse, spend nothing
  └─ JobQueue.submit("write:ai agents,evals")   ← single worker (ADR-008)
       ├─ run_pulse(...)                        ← X search, posts into the graph
       ├─ gather_own_material(graph, keywords)  ← graph.search(match="any") + notes
       ├─ build_write_pipeline → compose        ← one call, structured output
       │    └─ artifacts/drafts/<id>.json, cassette entry
       └─ reply: the post in a <pre> block, plus what it was built from
```

A failed X search is **not** fatal. The draft still gets written from your own notes,
which is where the substance was coming from anyway.

## 5. Identity and the grounding rule

`draft_id(keywords, written_at)` keys a draft on both, because asking again an hour later
is a different draft off different trends rather than a repeat of this one.

The rule that does the work is in `WRITE_PROMPT`:

> Build the post on something from THEIR OWN NOTES. Put that thing in `grounded_in`.
> If their notes contain nothing relevant, set `grounded_in` to null and write from the
> trend alone. NEVER invent an anecdote, a client, a metric, a job, or a memory.

When `grounded_in` comes back null the reply says so, in as many words, and suggests
adding your own angle before posting. That is deliberate. "Sounds like you" and "lies
about you" are one instruction apart, and a fabricated anecdote published under your own
name is not a style problem.

## 6. Failure modes

Nothing here can lose a capture: `/write` writes nothing to the journal.

| What happens | What the user sees | What the system does |
|---|---|---|
| No `XAI_API_KEY` | How to enable it | Command off; nothing else changes |
| No keywords given | The usage line | Refuses before spending anything |
| Daily budget reached | The spend so far | Refuses before queueing, as `/pulse` does |
| X search fails | A draft anyway, sources 0 | Writes from your own notes alone |
| Nothing of yours matches | The draft, plus a warning | `grounded_in` null, nothing invented |
| Compose call fails | The provider error name | The digest is kept; only the draft is lost |
| Draft cache write fails | The post, as normal | Logged, not suppressed — a lost draft cost money |
| Model returns junk | "nothing usable" | Structured output rejected it; no partial post |

## 7. Commands

| Command | Does |
|---|---|
| `/write <keywords>` | One LinkedIn post from X plus your own notes |

## 8. Decisions

- **On demand, never scheduled.** Same as `/pulse`: a scheduler spends money while you
  are not reading.
- **Grounded in your own material** — the whole reason this exists rather than a chatbot
  tab. `match="any"`, because three loosely related keywords rarely co-occur in one
  thought and `"all"` would silently return nothing.
- **One post, not variants.** Choosing between three drafts is work; the hook is where a
  post lives or dies and you can rewrite that in ten seconds.
- **It never posts** (Phase-1 non-goals: credentials, any action in the world). LinkedIn
  OAuth is a different risk class and a different design.
- **The draft is an artifact, not a journal entry** (ADR-010). Generated, cost money,
  not authored by you.
- **Cost recorded at the projection layer**, like `/pulse` — a draft has no originating
  thought, so the cassette key is the draft's own id.

## 9. Scope boundary

Not in scope, and not accidentally half-built: posting or scheduling to LinkedIn; image
and carousel generation; multiple variants or an A/B loop; engagement tracking; comment
replies; any network other than X; writing for anyone other than the account owner. Each
needs its own design, and the first would need Phase 1's non-goals revisited rather than
worked around.
