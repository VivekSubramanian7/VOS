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
| `/recent [n]` · `/category <name>` · `/search <term>` | Read back — `/search` wants every word, and matches word endings |
| `/undo` | Soft-delete the last thought |
| `/pending` | Retry thoughts whose classification failed |
| `/stats` | Counts per category, most-referenced sources, spend |
| `/follow person\|book\|channel <…>` · `/following` · `/unfollow <name>` | Declare what shapes your thinking |
| *(a YouTube link)* | Distilled automatically into timestamped notes |
| `/video <url>` · `/notes <term>` · `/redistil <url>` | Process now · search video notes · re-run from cache |
| `/shopping` · `/bought <name\|number>` | The shopping list, with a tap-to-buy button per item |

### X pulse

`/pulse` asks xAI to search X for the best of the last 24 hours, defaulting to
AI. `/pulse quantum computing` asks about something else. Every item is stored in the
graph, so `/more` lists them all and `/notes <term>` searches them alongside video
notes.

`/follow x @karpathy` weights an account in the digest. `/following` lists them.

**This costs money.** A digest measured at about $0.20 — cheaper than it used to be,
but not free, and tokens now dominate because the search reasons between queries.
xAI reports the exact amount it billed and that is what counts against
`VOS_DAILY_BUDGET_USD`, so no estimate is involved. Lower `VOS_PULSE_MAX_TOOL_CALLS`
to spend less. The budget guard refuses a pulse once the daily limit is reached,
and `/stats` shows the spend.

### Shopping

Say what you need the way you would say it to a person — "out of coffee, and 2L oat
milk" — and the things to buy are pulled out of the thought and put on a list. There is
no syntax and no "add item" mode, so putting something on the list costs no more than
mentioning it.

`/shopping` shows the list with a button per item; tapping one ticks it off and edits
the message in place, so at the shop you keep looking at one message rather than a
growing thread. `↩ Undo last` reverses a mis-tap. `/bought oat milk` or `/bought 2` does
the same by typing, for when the list has scrolled away.

Ticks are written to the journal, so they survive `vos reclassify --rebuild`. The list
itself lives in `shopping.db`, which is disposable — see
[docs/pipelines/shopping.md](docs/pipelines/shopping.md).

### Kitchen kiosk

A tablet in the kitchen can talk to VOS: the daemon serves a web page where anyone in
the family speaks a thought, corrects the locally-transcribed text, and saves it —
or asks questions over the graph in chat. A Shopping tab shows tap-to-add cards for
the household staples; a tapped card lands on the shared list (no model call) and the
card hides until the item is marked bought on Telegram. Audio never leaves the machine
(faster-whisper runs on it), chat is ephemeral, and the page is reachable only inside
your Tailscale tailnet — no public URL exists.

```bash
uv sync --extra kiosk                                # whisper + fastapi + uvicorn
tailscale serve --bg http://127.0.0.1:8765   # once, after installing Tailscale
# .env: VOS_KIOSK_ENABLED=1  (optional: VOS_KIOSK_PIN=…)
```

Full setup (tablet included) and the privacy model: `docs/pipelines/kitchen-kiosk.md`.

## Operations

| Task | Command |
|---|---|
| Backup | copy `journal/` **and** `artifacts/` |
| Rebuild the graph | `vos reclassify --rebuild` |
| Preview a prompt change | `vos reclassify --dry-run` |
| Change model | edit `VOS_MODEL`, restart |
| Start / stop on the server | `deploy\start-vos.ps1` · `deploy\stop-vos.ps1` |

Server deployment (Windows + docker compose + tailnet-only kiosk) is documented
in [DEPLOY.md](DEPLOY.md): `deploy/bootstrap-server.ps1` sets a machine up once,
`deploy/start-vos.ps1` and `deploy/stop-vos.ps1` run it day to day, and
`deploy/pull-deploy.ps1` auto-deploys new commits every 5 minutes.

## Testing

```bash
uv run pytest -m "not integration"   # offline, no Docker, no API spend
uv run pytest                        # includes ephemeral-Neo4j integration tests
```
