# Deploying VOS to a Windows server

Full docker-compose (Neo4j + app in containers) on a Windows machine, with the
kiosk reachable only over the tailnet (`tailscale serve` → loopback port 8765,
per ADR-015 — never open a firewall port for this).

The invariant that shapes every step: **`journal/` is the source of truth**.
Neo4j and `shopping.db` are disposable projections that rebuild from it. And
**Telegram long-polling is exclusive** — at any instant exactly one machine may
run the bot, or both fight over `getUpdates` with 409 Conflict.

## What migrates vs what rebuilds

**This move is a fresh start**: everything captured so far was testing, so the
only thing that travels is `.env`. The server begins with an empty journal,
empty graph, empty shopping list — no reclassify step, no model spend. The dev
machine keeps the test data untouched.

For future reference (server-to-server moves, backups), the general rules:

| Item | Migrate? | Why |
|---|---|---|
| `.env` | Yes | Secrets, not derivable. Git-ignored — travels out-of-band. |
| `journal/` | Yes* | Source of truth. (*Skipped this move — test data.) |
| `artifacts/` | Yes* | Video transcripts are NOT recomputable. (*Skipped — test data.) |
| `cassettes/` | Optional | Tiny; keeps `/stats` spend history and eval replay. |
| Neo4j volume | No | Startup reprojection restores thoughts (uncategorized, into `/pending`); `vos reclassify --rebuild` restores categories via model calls. |
| `shopping.db*` | No | Replays from the journal on startup; lives at `/data/shopping.db` on a named volume under compose. |
| HF whisper cache | No | Re-downloads once (~250 MB) into the `hf-cache` volume on first mic use. |

## 1. Server bootstrap

**Scripted path:** `deploy/bootstrap-server.ps1` automates this whole section —
tool installs (winget), tailnet join, clone, `.env` validation, build, Neo4j
first start, smoke test, `tailscale serve`, and the auto-deploy task. It is
idempotent (re-run after reboots or failures) and never starts the bot; the
cutover is a separate explicit `-Cutover` flag so a second Telegram poller can
never start by accident. In an elevated PowerShell on the server:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\deploy\bootstrap-server.ps1            # bootstrap (repeat until clean)
.\deploy\bootstrap-server.ps1 -Cutover   # only after stopping the dev bot
```

Run from inside a checkout it uses that checkout; run standalone (the file
downloaded on its own) it clones to `C:\VOS`.

Two things stay manual: Autologon (§1.1.5) and the tablet repoint (§2).
Once bootstrapped, day-to-day starting and stopping is §2.1.
The subsections below document what the script does, and serve as the manual
fallback.

### 1.1 Docker Desktop (WSL2 backend)

1. Verify virtualization is enabled (Task Manager → Performance → CPU). Enable in BIOS/UEFI if not.
2. `wsl --install --no-distribution` (or `wsl --update`), reboot if prompted.
3. Install Docker Desktop; keep "Use WSL 2 based engine" checked. Docker Hub sign-in not needed.
4. Settings → General → enable **Start Docker Desktop when you sign in**.
5. **Reboot gotcha:** Docker Desktop only runs inside a logged-in session. After
   a Windows Update reboot the containers stay down until someone signs in.
   Fix: configure auto-logon with Sysinternals
   [Autologon](https://learn.microsoft.com/en-us/sysinternals/downloads/autologon)
   (stores the password encrypted, unlike the netplwiz registry route). A bot
   that silently dies on Patch Tuesday defeats the purpose of the server.
6. Confirm: `docker run --rm hello-world`.

### 1.2 Tailscale

1. Install from tailscale.com, sign in to the **same tailnet the tablet is on**.
2. Admin console → DNS: confirm MagicDNS and HTTPS Certificates are enabled.
3. Note the server's FQDN from `tailscale status` (e.g. `server.tailnet.ts.net`).

Tailscale runs as a Windows service — connectivity and `serve` config survive
reboots without a user session.

### 1.3 Clone and transfer secrets

```powershell
git clone https://github.com/VivekSubramanian7/VOS.git C:\VOS
```

Fresh start: only `.env` travels (it is git-ignored and holds the bot token +
API keys). Via Taildrop, on the dev machine:

```powershell
tailscale file cp D:\VOS\.env <server-hostname>:
```

On the server:

```powershell
tailscale file get C:\VOS\
```

Do NOT copy `journal/`, `artifacts/`, `cassettes/`, `shopping.db*`, or any
Neo4j data — the test data stays on the dev machine (see the table above).
The app creates empty `journal/`, `artifacts/`, `cassettes/` dirs on first run
(they are also bind-mount targets in compose, which creates them as needed).

### 1.4 Finalize `.env` — before the first Neo4j start

Neo4j bakes `NEO4J_AUTH` into its data volume on FIRST start; changing the
password later requires `docker compose down -v`. So settle `.env` now:

- Carried over unchanged: `TELEGRAM_BOT_TOKEN`, `VOS_ALLOWED_USER_ID`,
  `NEO4J_PASSWORD`, the model provider key, `VOS_MODEL`.
- Add: `VOS_KIOSK_ENABLED=1`, `VOS_KIOSK_PIN=<choose one>`.
- Do NOT set `VOS_KIOSK_HOST` or `VOS_SHOPPING_DB` — docker-compose.yml owns
  those in-container values.

### 1.5 Build and start Neo4j only

```powershell
cd C:\VOS
docker compose build
docker compose up -d neo4j
docker compose ps          # wait for (healthy), up to ~2 min
docker compose run --rm --no-deps app python -c "import vos.shell; import vos.web.app; import faster_whisper; print('kiosk deps ok')"
```

**Do not `docker compose up -d app` yet** — the dev machine is still polling
Telegram. The smoke-test line starts no poller and needs no database.

### 1.6 Publish the kiosk on the tailnet

```powershell
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status     # https://<server>.<tailnet>.ts.net -> 127.0.0.1:8765
```

This terminates HTTPS with a real `*.ts.net` certificate (required for Chrome
mic access) and persists across reboots. No firewall changes — ever.

### 1.7 Register the auto-deploy task

```powershell
Register-ScheduledTask -TaskName "VOS deploy" `
  -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)) `
  -Action (New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\VOS\deploy\pull-deploy.ps1")
```

Leave the default "Run only when user is logged on" — the docker CLI needs the
session Docker Desktop lives in (covered by Autologon, §1.1.5).

After this, deployment is: push to GitHub from the dev machine; within 5
minutes the server pulls, rebuilds, and restarts. `deploy/pull-deploy.ps1`
only acts when `origin/main` has new commits, pulls `--ff-only` (a diverged
checkout halts loudly), and builds the image *before* swapping containers — a
broken push leaves the old version running and pings the Telegram chat.
`deploy/deploy.log` shows what happened; `git log -1` shows what's live.

Rejected alternatives: webhook receiver (inbound exposure violates ADR-003),
Watchtower (registry-based; this repo builds locally), GitHub Actions
self-hosted runner (fine but more machinery than one personal server needs).

## 2. Cutover

1. **Stop the dev bot FIRST** — kill any `uv run vos-bot` and
   `docker compose stop app` on the dev machine (`deploy\stop-vos.ps1` if it
   has these scripts). Do NOT `down -v` there: the dev Neo4j volume is the
   rollback state.
2. Server: `deploy\bootstrap-server.ps1 -Cutover` (or `deploy\start-vos.ps1`
   directly, plus `docker compose logs -f app`). Expect a clean start on an
   empty journal and `Kiosk serving on 0.0.0.0:8765`. A Telegram 409 means the
   dev bot still runs — the script says so explicitly.
   (Fresh start — no reclassify needed. When moving with real data, this is
   where `docker compose exec app vos reclassify --rebuild` restores graph
   categories; bound spend with `--from` on a large journal.)
3. Tablet: browse to `https://<server>.<tailnet>.ts.net`, enter the PIN,
   re-grant mic (permission is per-origin), re-pin to home screen.
5. Dev machine hygiene: `tailscale serve reset` so the old URL goes dark.

### Verification checklist

- [ ] `docker compose ps` — both Up, neo4j (healthy)
- [ ] `docker compose logs app` — no tracebacks, no Telegram 409
- [ ] Telegram `/stats` responds; a test thought gets *classified* (proves the model key)
- [ ] Server: `curl.exe http://127.0.0.1:8765/api/health` → 200
- [ ] Tablet: `https://<server>.<tailnet>.ts.net/api/health` → 200, valid cert
- [ ] Tablet mic capture end-to-end (first use downloads the whisper model once — watch the logs)
- [ ] `docker compose restart app`, capture again — no re-download (hf-cache volume works)
- [ ] Shopping: add an item from the kiosk Shopping tab → it appears in the Telegram list
- [ ] New journal lines appear in `C:\VOS\journal\` on the host (bind mount = backupable)
- [ ] **Reboot drill**: reboot now, verify Autologon → Docker → containers → tablet URL

## 2.1 Daily operation

Once the server is bootstrapped and cut over, starting and stopping VOS is one
command each. Both scripts work from any checkout path (they resolve the repo
from their own location) and are idempotent:

```powershell
deploy\start-vos.ps1              # Docker Desktop, Neo4j (wait healthy), app, health check
deploy\start-vos.ps1 -Neo4jOnly   # database only — starts NO Telegram poller
deploy\start-vos.ps1 -Build       # rebuild the image first (uncommitted local edits)
deploy\start-vos.ps1 -Follow      # then tail the app logs

deploy\stop-vos.ps1               # stop the app — releases the Telegram poller
deploy\stop-vos.ps1 -All          # also stop Neo4j
```

`start-vos.ps1` enforces the ordering this runbook otherwise asks a human to
remember: Neo4j must report `(healthy)` before the app starts, and the app must
answer `/api/health` before the script claims success. It then greps the app
log for a Telegram 409 and names the fix, so a second poller shows up as a
message rather than as a bot that silently ignores you.

`-Neo4jOnly` is the mode to use while another machine still polls Telegram —
it is exactly the state §1.5 leaves you in.

`stop-vos.ps1` only ever stops containers. It never runs `docker compose down`,
and never `down -v`: that deletes `neo4j-data` (including the `NEO4J_AUTH`
password baked in at first start), `vos-data`, and the whisper model cache.
Note that `restart: unless-stopped` does **not** resurrect a container you
stopped on purpose — after `stop-vos.ps1` the stack stays down across reboots
until you run `start-vos.ps1`.

## 3. Rollback

The dev machine keeps a complete pre-cutover copy (data was copied, not moved):

1. Server: `docker compose stop app` (leave neo4j; harmless, preserves a retry).
2. Taildrop the server's `journal/` and `artifacts/` back to the dev machine.
   Because this deployment was a fresh start, first archive the dev machine's
   old *test* journal out of the way (e.g. rename to `journal-testdata/`) so
   real records and test records never mix, then drop the server's files in.
   Never let both machines poll simultaneously — that forks the journal and
   forces a line-wise merge by record id; the "exactly one poller" rule exists
   to make that impossible.
3. Dev: start the bot; startup self-heal reprojects the copied-back records.
4. If the kiosk matters during the outage, re-run `tailscale serve` on dev and
   repoint the tablet.

## Operational notes

- Backup = copy `journal/` + `artifacts/` (+ `.env` somewhere safe). Everything
  else rebuilds.
- The compose port mapping hardcodes the default `VOS_KIOSK_PORT` (8765) — if
  you ever change the env var, change the mapping and the `tailscale serve`
  target with it.
- Journal writes cross a Windows bind mount (gRPC-FUSE), whose fsync guarantees
  are weaker than a native filesystem. Acceptable for append-only JSONL: the
  worst case in a hard host crash is losing the final record.
