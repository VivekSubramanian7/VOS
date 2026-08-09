# Kitchen kiosk — family voice surface

**Status:** shipped · spec at `docs/superpowers/specs/2026-08-09-kitchen-kiosk-design.md`

A tablet in the kitchen runs Chrome pointed at a page the VOS daemon serves. Anyone in
the family can speak a thought or ask a question. Audio is transcribed **on the VOS
machine** by faster-whisper, shown back for correction, and only the confirmed text
goes anywhere. Chat is ephemeral; captures land in the same journal/graph as Telegram
ones, tagged `channel: "kitchen"`.

## Privacy model (what goes where)

| Data | Where it lives | Where it never goes |
|---|---|---|
| Microphone audio | RAM on the VOS machine, one request long | Disk, any cloud, any third party |
| Raw Whisper transcript | The editable confirm box; `transcript` field of a capture the user chose to save | Anywhere, if the user hits Discard |
| Chat conversations | Process memory, TTL-swept (default 30 min), gone on restart | The journal, the graph, any disk |
| Confirmed captures | `journal/` + Neo4j, like every Telegram capture | — |
| Final text prompts | The model API (Claude), same as all classification | — |
| Transport | WireGuard-encrypted tailnet, end to end | The open internet in cleartext; any public URL |

Deliberately rejected: ngrok / Cloudflare Tunnel (public endpoint; the provider can
read traffic after TLS termination). Tailscale has no public endpoint and its DERP
relays only ever forward ciphertext. If Tailscale itself becomes unwanted, the
fallback is plain WireGuard + mkcert, at the cost of manual key and cert management.

## One-time setup

### VOS machine (Windows dev server)

1. `uv sync --extra kiosk` — FastAPI, uvicorn and faster-whisper (~300 MB; the bundled
   PyAV decodes browser audio, no ffmpeg install needed).
2. Install [Tailscale for Windows](https://tailscale.com/download), sign in (free
   personal plan). In the admin console enable **MagicDNS** and **HTTPS certificates**.
3. Publish the kiosk into the tailnet (persists across reboots):

   ```
   tailscale serve --bg https / http://127.0.0.1:8765
   ```

4. In `.env`:

   ```
   VOS_KIOSK_ENABLED=1
   # optional:
   VOS_KIOSK_PIN=4321          # soft gate on top of tailnet membership
   VOS_WHISPER_MODEL=small     # 'base' halves latency at some accuracy cost
   ```

5. Restart the bot (`uv run vos-bot`). First mic use downloads the Whisper model
   (~250 MB) to the Hugging Face cache, then everything is offline.

The kiosk binds to `127.0.0.1` — it is reachable from **no** physical network, only
through `tailscale serve`. Do not open firewall ports for it; none are needed.

### Tablet (Android + Chrome)

1. Install the Tailscale app, sign in to the same account, toggle the VPN on. It
   stays connected in the background.
2. Open Chrome at `https://<machine-name>.<tailnet>.ts.net` (find the exact name with
   `tailscale status` on the server). Real HTTPS, so Chrome grants microphone access
   normally — no flags.
3. Tap **Allow** for the microphone; enter the PIN if one is set.
4. Chrome menu → **Add to Home screen** for a full-screen icon. Set display timeout
   long, keep the tablet plugged in.

## Daily use

- **Capture** mode: tap the dial, talk, tap again. What Whisper heard appears in an
  editable card — fix it, hit **Save it**. The reply says which category it was filed
  under. Typing in the text field works too.
- **Ask** mode: same flow, but the confirmed text goes to the chat agent, which can
  search thoughts, video notes and X posts (read-only) or just answer generally.
- A capture that cannot be classified right now (budget, provider down) is still
  saved and appears in `/pending` on Telegram.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Page unreachable | Tailscale off on either device; `tailscale status` on both |
| Mic button says permission denied | Chrome mic permission for the ts.net origin; check the padlock menu |
| Transcription very slow (>10 s) | `small` model on a busy CPU — set `VOS_WHISPER_MODEL=base` |
| "PIN stopped working" | PIN changed in `.env`; sessionStorage clears when the tab's session ends |
| Everything saved lands in pending | Daily budget exhausted (`/stats` on Telegram) or provider down |
| First transcription after a restart is slow | The model loads lazily on first use; later clips are fast |

## Operational notes

- Chat spend is metered: every chat model call is cassette-logged under a per-session
  key, so `/stats` includes it and `BudgetGuard` refuses chat turns once the daily
  budget is spent. Capture keeps working regardless — enrichment is deferred, never
  the save.
- Kiosk graph writes go through the same single JobQueue worker as everything else
  (ADR-008); the web handlers never touch Neo4j directly.
- A retried POST (tablet WiFi blip) reuses the browser-generated `client_id`, so the
  journal and graph collapse it exactly like a Telegram redelivery.
- Restarting the daemon wipes all chat sessions by design. Nothing to clean up.
