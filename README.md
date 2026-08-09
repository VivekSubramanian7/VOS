# VOS — Phase 1

Send a thought to Telegram. It is captured durably, classified, and the knowledge graph grows.

Architecture: see `docs/architecture.md`.

## The one thing to understand

**`journal/` is the source of truth. Neo4j is a projection.**

Raw captured text is appended to an append-only, fsync'd journal *before* anything else runs.
Everything downstream — classification, the graph — is derived and rebuildable. This is why:

- No thought is lost when the LLM provider or the database is down.
- Improving the prompt is retroactive: `vos reclassify` replays history through the new one.

**Back up two directories: `journal/` and `artifacts/`.** Everything else regenerates.
`artifacts/` holds fetched video transcripts, which are the one *other* thing that cannot be
recomputed — the upstream video can be deleted or lose its captions, and then the transcript
is gone for good unless you kept it.

## Quick start

```bash
cp .env.example .env          # fill in TELEGRAM_BOT_TOKEN, VOS_ALLOWED_USER_ID, API key, NEO4J_AUTH
docker compose up -d neo4j    # graph browser at http://localhost:7474
uv sync --extra dev
uv run vos-bot                # long-polling; no public URL needed
```

## Commands

| Command | Does |
|---|---|
| *(any text)* | Capture a thought |
| `/recent [n]` · `/category <name>` · `/search <term>` | Read back |
| `/undo` | Soft-delete the last thought |
| `/pending` | Retry thoughts whose classification failed |
| `/stats` | Counts per category, most-referenced sources, spend |
| `/follow person\|book\|channel <…>` · `/following` · `/unfollow <name>` | Declare what shapes your thinking |
| *(a YouTube link)* | Distilled automatically into timestamped notes |
| `/video <url>` · `/notes <term>` · `/redistil <url>` | Process now · search video notes · re-run from cache |

### X pulse

`/pulse` asks xAI Live Search for the best of the last 24 hours on X, defaulting to
AI. `/pulse quantum computing` asks about something else. Every item is stored in the
graph, so `/more` lists them all and `/notes <term>` searches them alongside video
notes.

`/follow x @karpathy` weights an account in the digest. `/following` lists them.

**This costs money.** Live Search bills $0.025 per source, so a 25-source digest is
about $0.63. Lower `VOS_PULSE_MAX_SOURCES` to spend less. The daily budget guard
refuses a pulse once `VOS_DAILY_BUDGET_USD` is reached, and `/stats` shows the spend.

## Operations

| Task | Command |
|---|---|
| Backup | copy `journal/` **and** `artifacts/` |
| Rebuild the graph | `vos reclassify --rebuild` |
| Preview a prompt change | `vos reclassify --dry-run` |
| Change model | edit `VOS_MODEL`, restart |

## Testing

```bash
uv run pytest -m "not integration"   # offline, no Docker, no API spend
uv run pytest                        # includes ephemeral-Neo4j integration tests
```
