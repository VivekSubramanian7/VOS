# Kitchen Kiosk — family voice interface on a tablet

**Status:** approved design, not yet implemented
**Date:** 2026-08-09

## Context

VOS today has exactly one surface: Vivek's personal Telegram chat, gated by
`AllowOnlyOwner`. The family needs a shared way to talk to the bot — capture
thoughts, ask questions of the graph, and chat — from a tablet mounted in the
kitchen, without installing anything and without handing family audio or
transcripts to any third party.

**Privacy is the headline constraint.** Audio is transcribed locally
(faster-whisper on the dev machine) and never written to disk; chat sessions are
ephemeral (in-memory, gone on restart); only explicit captures persist to the
journal/graph; the only data that ever reaches a cloud service is the final text
prompt sent to the Claude API — same as every existing capture.

### Decisions

| Decision | Choice | Why not the alternative |
|---|---|---|
| Surface | Web page in Chrome on the tablet | Native app = install/update burden; Telegram = audio transits Telegram servers |
| Hosting | FastAPI **inside** the VOS daemon process | Separate service needs an invented internal API and breaks ADR-008 single-writer |
| STT | faster-whisper `small` int8, local CPU | Cloud STT ships family audio off-box; fully-local LLM sacrifices answer quality |
| Transcript UX | Show editable transcript, user corrects, then sends | Silent auto-send propagates Whisper errors into the journal |
| Connectivity | Tailscale tailnet, kiosk bound to `127.0.0.1` | Tablet WiFi and server Ethernet are **separate internet connections** — no shared LAN. ngrok/CF Tunnel create a public endpoint and can read traffic |
| Secure context | `tailscale serve` → real HTTPS at `<machine>.ts.net` | Chrome-flag hack unneeded; mic just works |
| Attribution | `channel="kitchen"` field, same journal/graph pool | Separate family journal splits shopping-list queries across two stores |
| Replies | Text on screen only | TTS adds an engine + latency; add later behind the same interface |
| Auth | Optional shared PIN (`X-VOS-PIN` header) | Tailnet membership is already the real gate; per-person logins are friction |

### Non-goals (v1)

Lists/reminders with check-off (phase 2 — the chat agent's tool seam and the
`channel` field are the hooks); TTS replies; per-person speaker attribution;
wake-word/always-listening (tap-to-talk only, deliberately); any public endpoint.

## Architecture

One process. FastAPI runs as an asyncio task beside `dp.start_polling()`;
uvicorn's signal handlers are disabled so aiogram keeps owning Ctrl+C.

```
Tablet Chrome ──HTTPS (tailnet only)──> tailscale serve ──> 127.0.0.1:8765 FastAPI
  mic tap → MediaRecorder (webm/opus)
  POST /api/transcribe ──> FasterWhisperTranscriber (asyncio.to_thread, Semaphore(1))
       └─ transcript returned, shown in editable confirm box (audio discarded)
  POST /api/capture {text, client_id, source, transcript}
       ├─ journal.append (fsync — durability boundary; OSError → 503 saved:false)
       └─ JobQueue(concurrency=1): upsert_thought → classify_one   ← ADR-008 honest
            └─ await future ≤12s → classification | status:"pending"
  POST /api/chat {session_id, message}
       └─ StateGraph: agent(bind_tools) ⇄ ToolNode, ≤4 tool rounds
            tools = read-only GraphStore: recent, search, by_category,
                    stats, search_notes, search_posts
            SessionStore: in-memory, TTL sweep, 40-msg trim — never journaled
```

- `CaptureRecord.channel: Literal["telegram","kitchen"] = "telegram"` — default
  keeps every existing journal line parsing. Kitchen id =
  `uuid5(NS, f"kitchen:{client_id}")` from a browser UUID → retried POSTs dedupe
  exactly like Telegram redelivery.
- Kitchen voice captures are `source="voice"`: raw Whisper text in `transcript`,
  corrected text in `text` — the fields built for this in §7.1.
- Chat spend is cassette-logged per call (key `uuid5(NS, f"chat:{session_id}")`)
  so `BudgetGuard` sees it; each turn is budget-pre-checked like shell.py does.
- "Capture this" in chat is a UI button funneling into `/api/capture`, not an
  agent tool — leaves a clean `propose_capture` seam for phase-2 lists.
- Dependencies are an optional `kiosk` extra with lazy imports; the daemon stays
  installable and runnable without it (`VOS_KIOSK_ENABLED` defaults off).

## Settings

`VOS_KIOSK_ENABLED` (False), `VOS_KIOSK_HOST` (127.0.0.1), `VOS_KIOSK_PORT`
(8765), `VOS_WHISPER_MODEL` (small), `VOS_KIOSK_SESSION_TTL_S` (1800),
`VOS_KIOSK_PIN` (SecretStr, optional).

## Stories

1. Contracts + settings (channel field, `create_kitchen`, graph write, settings)
2. STT adapter (`web/stt.py`, kiosk extra, wheel verification)
3. Web skeleton + uvicorn co-hosting in `shell.run()`
4. `/api/transcribe` + `/api/capture` endpoints
5. Chat agent (`web/chat_agent.py`, SessionStore)
6. Frontend SPA (`web/static/`, vanilla, no build step)
7. Docs/ops (runbook incl. Tailscale setup, ADR-012, README)

Order 1 → (2 ∥ 3) → 4 → 5 → 6 → 7.

## Risks

- cp313 Windows wheels for ctranslate2/onnxruntime/av — verify at `uv lock`
  (fallback: whisper.cpp behind the same `Transcriber` protocol).
- Tailscale free tier — fallback documented: plain WireGuard + mkcert.
- Kiosk availability depends on both internet connections being up.
- Chat budget check is pre-turn only, bounded by the 4-round tool cap.
