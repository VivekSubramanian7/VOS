# Pipeline: Video → Knowledge

**Status:** proposed · design only, not implemented
**Category routed:** `VideoKnowledge`

---

## 1. What it does

You send a YouTube link. A few seconds later the thought is filed as usual. Half a minute
after that, a second message arrives with the video's distilled takeaways — each one a
deep link to the second it was said — and the claims are in the graph, connected to the
same entities your own thoughts touch.

```
you:  https://youtube.com/watch?v=abc123
vos:  ✅ Filed under Video knowledge · 🔗 Veritasium
vos:  📺 "Why No One Has Measured The Speed Of Light"  — Veritasium
      • You cannot measure one-way light speed; only round-trip  [2:14]
      • The Einstein synchronisation convention hides this       [7:41]
      • Anisotropic-speed models are experimentally indistinguishable [14:03]
      🔗 Einstein · relativity · Veritasium
```

## 2. Why this one first

- **Self-contained.** No credentials beyond what VOS already has, and no new authority
  — still strictly read-only.
- **It grows the graph.** Every other processor reads what capture produced; this one
  adds material, which makes later retrieval worth building.
- **It rehearses the errand runner.** `fetch → distil → project` is the same shape as
  `gather → verify → synthesise`, minus the fan-out. Learning where fetch-and-distil
  breaks is much cheaper here than inside a multi-node research graph.

## 3. What makes it more than a summariser

**Timestamps.** Transcripts are timestamped, so each extracted note carries the second
it came from and renders as `youtu.be/abc123?t=134`. Two consequences:

1. Notes are *verifiable* — you can jump to the source in one tap and check the model
   didn't invent it. That is the difference between notes you trust and notes you skim.
2. Notes are *addressable*, which is what justifies storing them as nodes rather than a
   blob of summary text on the thought.

**Shared entities.** A claim from a video and a thought you had last week both attach to
the same `:Entity`. "Everything I know about X" spans both without any extra machinery —
the payoff the graph choice was supposed to deliver.

## 4. Graph schema additions

```cypher
(:Video {id, url, title, channel, approx_duration_s, fetched_at, transcript_path})
  -[:PUBLISHED_BY]-> (:Entity:Source {kind:'channel'})

(:Thought)-[:ABOUT]->(:Video)

(:Note {id, text, t_seconds})-[:FROM]->(:Video)
(:Note)-[:MENTIONS]->(:Entity)
```

- `:Video.id` is the YouTube video ID — a natural key, so `MERGE` makes re-sending the
  same link idempotent, the same way `thought_id` does for messages.
- `:Note.id` is `uuid5(video_id, t_seconds + text)`, so re-distilling replaces notes
  rather than duplicating them.
- `PUBLISHED_BY` reuses the existing `:Entity:Source` node. If you already `/follow
  channel Veritasium`, the video attaches to that node; if not, the channel is created
  as a plain `:Entity` and a later `/follow` converges on it (ADR-009 again).

## 5. Pipeline shape

The classification graph gains a conditional edge. This is the growth the architecture
anticipated — processors arrive as nodes, not as a restructure.

```
                        ┌──────────┐
      ThoughtState ────►│ analyze  │
                        └────┬─────┘
                             │  category == VideoKnowledge AND a YouTube URL present?
                   ┌─────────┴─────────┐
                  no                  yes
                   │                   ▼
                   │            ┌──────────────┐
                   │            │    fetch     │  oEmbed metadata + transcript
                   │            └──────┬───────┘  cached to artifacts/
                   │                   ▼
                   │            ┌──────────────┐
                   │            │    distil    │  chunk → notes with timestamps
                   │            └──────┬───────┘  map-reduce over long transcripts
                   │                   ▼
                   │            ┌──────────────┐
                   │            │   project    │  :Video, :Note, :MENTIONS
                   │            └──────┬───────┘
                   └───────────────────┴────► END
```

## 6. Transcript acquisition

`youtube-transcript-api` — pure Python, no binary dependency, no API key. Reads the
captions YouTube already has (manual where available, auto-generated otherwise).

**No metadata API key needed either:** `https://www.youtube.com/oembed?url=…&format=json`
returns `title` and `author_name` unauthenticated. Duration is derived from the last
transcript timestamp rather than fetched.

### The transcript is an artifact, and that changes where it lives

Everywhere else in VOS, derived data is disposable because it can be recomputed. A
transcript cannot: **the upstream can disappear.** Videos get deleted, made private, or
region-locked, and captions get turned off. If VOS holds only a summary and the video
vanishes, the underlying material is gone for good.

So transcripts are cached to `artifacts/videos/{video_id}.json` and treated as *inputs*,
alongside the journal — backed up, never auto-pruned. That keeps `vos reclassify` honest
for videos too: re-distilling an old video with a better prompt reads the cached
transcript rather than re-fetching and failing.

This is a genuine addition to the storage model in §7.3 of the architecture:

| Path | Contents | Backup | Rebuildable |
|---|---|---|---|
| `journal/` | Raw captures | Yes | No |
| `artifacts/videos/` | **Fetched transcripts** | **Yes** | **No — upstream can vanish** |
| Neo4j | Projection | Optional | Yes |
| `cassettes/` | Model calls | Optional | No, non-critical |

## 7. Distillation

Chunk the transcript into ~4k-token windows with overlap, each chunk carrying its start
timestamp. Map each chunk to candidate notes via one structured call, then reduce:
deduplicate near-identical claims and keep the highest-value 5–12 overall.

Structured output, same pattern as classification:

```python
class VideoNote(BaseModel):
    text: str            # one self-contained claim, not a topic label
    t_seconds: int       # where it was said
    entities: list[ExtractedEntity]

class VideoDistillation(BaseModel):
    notes: list[VideoNote]
    summary: str
```

Prompt rules that matter:

- A note must be a **claim you could disagree with**, not a section heading. "Discusses
  relativity" is worthless; "One-way light speed cannot be measured" is a note.
- Never assert what the transcript doesn't say. Auto-captions garble names constantly —
  the model must prefer omission over invention.
- `t_seconds` must come from the chunk's own timestamps, never guessed.

## 8. Failure modes

| Failure | Response | Data lost |
|---|---|---|
| Captions disabled | Thought stays filed as `VideoKnowledge`; reply says transcript unavailable | None |
| Video private / deleted / region-locked | Same, with the specific reason | None |
| Not a YouTube URL (Vimeo, podcast, article) | Explicit "only YouTube for now" — never a silent no-op | None |
| Very long video (3h+) | Chunk count capped; reply states coverage was truncated | None, but stated |
| Daily budget reached | Distillation deferred; `/pending` retries | None |
| Transcript fetch rate-limited | Retry with backoff, then defer to `/pending` | None |

Same principle as capture: the thought is already durable before any of this runs, so
every row resolves to "no data lost".

## 9. New commands

| Command | Does |
|---|---|
| `/video <url>` | Process explicitly, bypassing classification |
| `/notes <search>` | Search notes (distinct from `/search`, which covers your own thoughts) |
| `/redistil <video>` | Re-run distillation from the cached transcript, e.g. after a prompt change |

## 10. Decisions

**Where the work runs — in-process task queue, concurrency 1.** Capture replies
immediately; the video job runs on a background worker and posts a second message when
done. Concurrency 1 preserves the single-writer property of ADR-008 for graph writes
while freeing the polling loop. This is the "revisit when processors introduce genuine
parallelism" case the architecture anticipated, resolved in the narrowest way that works.

> **This changes the pipeline shape from §5.** A conditional edge inside the
> classification graph only works if the video work runs in the same invocation. With a
> worker it does not — re-entering that graph would re-run classification. So there are
> **two graphs**: classification stays a single `analyze` node, and the video pipeline
> (`fetch → distil`) is its own graph that the worker invokes. Projection happens outside
> the graph, exactly as `classify_one` already does for classification.

**Queue durability.** The queue is in-memory, so a restart loses queued jobs. Rather than
add a durable queue, unprocessed videos are *derivable*: a thought classified
`VideoKnowledge` carrying a URL but with no `(:Thought)-[:ABOUT]->(:Video)` edge has not
been processed. Startup re-enqueues those, and `/pending` retries them on demand. The
journal remains the only thing that must survive, which is the property worth protecting.

**Captions only.** `youtube-transcript-api`; when captions are unavailable the reply says
so plainly. No audio download, no STT bill, no reopened vendor decision. The gap is
reported rather than hidden.

## 11. Scope boundary

Explicitly **not** in this pipeline: non-YouTube video, podcasts, articles, playlists or
channel-wide ingestion, proactive fetching of new uploads from followed channels. Each is
a later, separate decision — channel-wide ingestion in particular reintroduces proactive
behaviour, which is out of scope by decision, not by oversight.
