<#
Start VOS on this machine. The everyday counterpart to stop-vos.ps1.

    deploy\start-vos.ps1              # full stack: Neo4j, then the app
    deploy\start-vos.ps1 -Neo4jOnly   # database only - starts NO Telegram poller
    deploy\start-vos.ps1 -Build       # rebuild the image first (local edits)
    deploy\start-vos.ps1 -Follow      # tail the app logs when it is up

Idempotent: every step checks before it acts, so re-running a partially started
stack is a fast no-op. Ordering is the point - Neo4j must be healthy before the
app, and the app must answer /api/health before this script claims success.

Telegram long-polling is exclusive. Exactly one machine may run the app
container; a second one gets 409 Conflict and both misbehave. -Neo4jOnly is the
safe mode while another machine still polls, and the 409 check at the end says
so out loud rather than leaving a quietly broken container running.

This is NOT the installer. First-time server setup is deploy\bootstrap-server.ps1.
#>
param(
    [switch]$Build,
    [switch]$Neo4jOnly,
    [switch]$Follow
)

$ErrorActionPreference = 'Stop'

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "XX  $msg" -ForegroundColor Red; exit 1 }

# Capture a native command's stdout AND stderr as one string without PowerShell
# 5.1's `2>&1` wrapping stderr lines in ErrorRecords (which flips $? on a
# perfectly successful exe). cmd does the merge before PowerShell ever sees it.
function Capture($cmdline) { (& cmd /c "$cmdline 2>&1") -join "`n" }

# Repo root is wherever this script lives - same idiom as pull-deploy.ps1, so
# the checkout can sit on any drive.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
Say "Repo: $repo"

# ------------------------------------------------------------ docker daemon --
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say 'Docker daemon not running - starting Docker Desktop...'
    $desktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $desktop)) { Fail "Docker Desktop not found at $desktop - run deploy\bootstrap-server.ps1 first" }
    Start-Process $desktop
    $deadline = (Get-Date).AddMinutes(3)
    do { Start-Sleep -Seconds 5; docker info *> $null } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) { Fail 'Docker daemon did not come up within 3 minutes' }
}
Say 'Docker daemon running'

# -------------------------------------------------------------------- .env ---
$envPath = Join-Path $repo '.env'
if (-not (Test-Path $envPath)) {
    Fail ".env is missing at $envPath - copy .env.example and fill it in, or Taildrop it from the dev machine (see DEPLOY.md 1.3)"
}
$envText = Get-Content $envPath -Raw

# The three with no default in src/vos/settings.py. NEO4J_PASSWORD additionally
# fails compose interpolation, which would otherwise surface as a cryptic error.
foreach ($key in 'TELEGRAM_BOT_TOKEN', 'VOS_ALLOWED_USER_ID', 'NEO4J_PASSWORD') {
    if ($envText -notmatch "(?m)^\s*$key=.+") { Fail ".env is missing $key" }
}
# The kiosk runs without a PIN, it just has no gate then - warn, do not block.
if ($envText -notmatch '(?m)^\s*VOS_KIOSK_PIN=.+') {
    Warn '.env has no VOS_KIOSK_PIN - the kiosk will be reachable on the tailnet with no PIN prompt'
}
$bytes = [System.IO.File]::ReadAllBytes($envPath)
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    Warn '.env starts with a UTF-8 BOM - if the first variable behaves as unset, rewrite the file as UTF-8 without BOM'
}
Say '.env present with required keys'

# The kiosk is optional: with VOS_KIOSK_ENABLED unset the bot runs happily and
# nothing ever listens on the port, so waiting on /api/health or publishing the
# port would be waiting for something that is never coming.
$kiosk = $envText -match '(?m)^\s*VOS_KIOSK_ENABLED=\s*(1|true|yes|on)\s*$'
if (-not $kiosk) { Say 'VOS_KIOSK_ENABLED is off - starting the bot without the kiosk' }

# Compose hardcodes the 8765 port mapping and tailscale serve targets it, so a
# changed VOS_KIOSK_PORT needs all three edited together (see DEPLOY.md).
$port = '8765'
if ($envText -match '(?m)^\s*VOS_KIOSK_PORT=\s*(\d+)') {
    $port = $Matches[1]
    if ($port -ne '8765') {
        Warn "VOS_KIOSK_PORT=$port but docker-compose.yml publishes 127.0.0.1:8765:8765 - change the mapping and the tailscale serve target too"
    }
}

# ------------------------------------------------------------------- build ---
if ($Build) {
    Say 'Building the app image...'
    docker compose build
    if ($LASTEXITCODE -ne 0) { Fail 'docker compose build failed' }
}

# ------------------------------------------------------------------- neo4j ---
# First start ever bakes NEO4J_AUTH into the volume and pulls the image, so this
# can take minutes; afterwards it is seconds.
Say 'Starting Neo4j...'
docker compose up -d neo4j
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up -d neo4j failed' }

$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 5
    $health = docker inspect --format '{{.State.Health.Status}}' vos-neo4j
} while ($health -ne 'healthy' -and (Get-Date) -lt $deadline)
if ($health -ne 'healthy') { Fail "Neo4j is '$health' after 3 min - docker compose logs neo4j" }
Say 'Neo4j healthy'

if ($Neo4jOnly) {
    Write-Host ''
    docker compose ps
    Say 'Neo4j only - no Telegram poller started. Run without -Neo4jOnly to start the app.'
    exit 0
}

# --------------------------------------------------------------------- app ---
# depends_on: service_healthy makes the gate above redundant on paper; doing it
# explicitly turns a dependency timeout into a named failure.
Say 'Starting the app (Telegram bot + kiosk)...'
docker compose up -d app
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up -d app failed - docker compose logs app' }

# /api/health is the one route exempt from the PIN middleware (src/vos/web/app.py).
$healthUrl = "http://127.0.0.1:$port/api/health"
if ($kiosk) {
    Say "Waiting for $healthUrl ..."
    $deadline = (Get-Date).AddSeconds(90)
    $ok = $false
    do {
        Start-Sleep -Seconds 3
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $ok = $true }
        } catch { }
    } while (-not $ok -and (Get-Date) -lt $deadline)

    if ($ok) { Say 'Kiosk healthy (HTTP 200)' }
    else     { Warn "No 200 from $healthUrl after 90s - docker compose logs app" }
} else {
    # No health endpoint to gate on, but the container still needs a moment
    # before its logs are worth reading for the 409 check below.
    Start-Sleep -Seconds 10
}

# Non-interactive 409 detection. bootstrap-server.ps1 -Cutover asks a human
# instead, which is right for a one-time switch-over and wrong for a script you
# run daily.
# Matched on Telegram's own wording and aiogram's exception type rather than a
# bare "Conflict", which appears in plenty of innocent log lines.
$appLogs = Capture 'docker compose logs --no-color --tail 50 app'
if ($appLogs -match 'terminated by other getUpdates|TelegramConflictError') {
    Write-Host ''
    Warn 'TELEGRAM 409 CONFLICT - another machine is polling this bot token.'
    Warn 'Stop the bot there (uv run vos-bot / docker compose stop app), then here:'
    Warn '    deploy\stop-vos.ps1 ; deploy\start-vos.ps1'
}

# ------------------------------------------------------------ tailscale serve --
# Publishes loopback as HTTPS on the tailnet (ADR-015). Persists across reboots,
# so this is normally already configured - only set it when it is not.
if ($kiosk) {
    if (Get-Command tailscale -ErrorAction SilentlyContinue) {
        $serve = Capture 'tailscale serve status'
        if ($serve -match 'No serve config') {
            Say "Configuring tailscale serve -> http://127.0.0.1:$port"
            tailscale serve --bg "http://127.0.0.1:$port"
            if ($LASTEXITCODE -ne 0) { Warn 'tailscale serve failed - the kiosk is up on loopback but not published on the tailnet' }
        } else {
            Say 'tailscale serve already configured'
        }
    } else {
        Warn 'tailscale not on PATH - the kiosk is reachable on loopback only'
    }
}

# ----------------------------------------------------------------- summary ---
Write-Host ''
docker compose ps
Write-Host ''
Say 'VOS is up.'
if ($kiosk) {
    if (Get-Command tailscale -ErrorAction SilentlyContinue) {
        try {
            $fqdn = (tailscale status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
            if ($fqdn) { Write-Host "  Kiosk:  https://$fqdn" }
        } catch { }
    }
    Write-Host "  Local:  $healthUrl"
}
Write-Host '  Logs:   docker compose logs -f app'
Write-Host '  Stop:   deploy\stop-vos.ps1'

if ($Follow) {
    Write-Host ''
    docker compose logs -f app
}

# Explicit so a caller (bootstrap-server.ps1 -Cutover) sees a real exit code
# rather than whatever the last native command happened to leave behind.
exit 0
