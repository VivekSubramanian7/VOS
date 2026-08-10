<#
VOS server bootstrap + cutover - DEPLOY.md as a script. Idempotent: safe to
re-run after a reboot or a failed step; every step checks before it acts.

Usage, in an ELEVATED PowerShell on the SERVER:

    Set-ExecutionPolicy -Scope Process Bypass -Force
    C:\VOS\deploy\bootstrap-server.ps1            # bootstrap: install, build, serve
    C:\VOS\deploy\bootstrap-server.ps1 -Cutover   # start the bot (dev bot must be STOPPED)

(First run: download just this file, or clone the repo manually - the script
clones/updates C:\VOS itself either way.)

Cutover is a separate flag on purpose: Telegram long-polling is exclusive, and
starting the app while the dev machine still polls means 409 Conflict. The
bootstrap half touches nothing that conflicts with the running dev bot.
#>
param([switch]$Cutover)

$ErrorActionPreference = 'Stop'
$RepoUrl = 'https://github.com/VivekSubramanian7/VOS.git'
$Repo    = 'C:\VOS'

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "XX  $msg" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Ensure-Tool($cmd, $wingetId, $name) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { Say "$name present"; return }
    Say "Installing $name (winget)..."
    winget install -e --id $wingetId --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { Fail "winget install of $name failed (exit $LASTEXITCODE)" }
    Refresh-Path
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Fail "$name installed but '$cmd' is not on PATH yet - open a NEW elevated terminal and re-run this script"
    }
}

# ---------------------------------------------------------------- cutover ----
if ($Cutover) {
    Set-Location $Repo
    $answer = Read-Host 'Is the DEV machine bot fully stopped? Two pollers on one token fight with 409 Conflict. Type yes to continue'
    if ($answer -ne 'yes') { Fail 'Stop the dev bot first, then re-run with -Cutover' }

    Say 'Starting the app container...'
    docker compose up -d app
    if ($LASTEXITCODE -ne 0) { Fail 'docker compose up -d app failed' }

    Say 'Waiting 15s for startup, then checking kiosk health...'
    Start-Sleep -Seconds 15
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 10
        Say "Kiosk health: HTTP $($r.StatusCode)"
    } catch {
        Warn 'Kiosk health check failed - inspect: docker compose logs app'
    }
    docker compose logs --tail 20 app

    $fqdn = (tailscale status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
    Write-Host ''
    Say 'Cutover done. Finish the DEPLOY.md checklist by hand:'
    Write-Host "  - Telegram: /stats responds; a test thought gets classified"
    Write-Host "  - Tablet:   https://$fqdn  (enter PIN, re-grant mic, pin to home screen)"
    Write-Host "  - Mic capture end-to-end (first use downloads the whisper model once)"
    Write-Host "  - Reboot drill: reboot now, verify everything returns on its own"
    Write-Host "  - Dev machine hygiene: tailscale serve reset (old URL goes dark)"
    exit 0
}

# -------------------------------------------------------------- bootstrap ----
Say 'VOS server bootstrap (idempotent - re-run freely)'

# 1. Tools
Ensure-Tool 'git'       'Git.Git'             'Git'
Ensure-Tool 'tailscale' 'Tailscale.Tailscale' 'Tailscale'

# 2. WSL2 (Docker Desktop backend)
wsl --status *> $null
if ($LASTEXITCODE -ne 0) {
    Say 'Installing WSL (no distribution - Docker brings its own)...'
    wsl --install --no-distribution
    Warn 'If WSL asked for a reboot: reboot, then re-run this script.'
}

# 3. Docker Desktop
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Ensure-Tool 'docker' 'Docker.DockerDesktop' 'Docker Desktop'
    Warn 'Fresh Docker Desktop install: launch it once, accept the terms, THEN re-run this script.'
    Warn 'Also enable Settings -> General -> "Start Docker Desktop when you sign in".'
    exit 0
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say 'Docker daemon not running - starting Docker Desktop...'
    Start-Process "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    $deadline = (Get-Date).AddMinutes(3)
    do { Start-Sleep -Seconds 5; docker info *> $null } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) { Fail 'Docker daemon did not come up within 3 minutes' }
}
Say 'Docker daemon running'

# 4. Tailnet - must be the SAME tailnet the tablet is on
tailscale status *> $null
if ($LASTEXITCODE -ne 0) {
    Say 'Tailscale not logged in - a browser window will open. Use the account whose tailnet has the tablet.'
    tailscale login
    if ($LASTEXITCODE -ne 0) { Fail 'tailscale login failed' }
}
$fqdn = (tailscale status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
Say "On tailnet as: $fqdn"

# 5. Repo
if (-not (Test-Path (Join-Path $Repo '.git'))) {
    Say "Cloning $RepoUrl -> $Repo"
    git clone $RepoUrl $Repo
    if ($LASTEXITCODE -ne 0) { Fail 'git clone failed' }
} else {
    Say 'Repo present - pulling latest'
    Set-Location $Repo
    git pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) { Fail 'git pull failed (diverged checkout?)' }
}
Set-Location $Repo

# 6. Secrets - .env travels out-of-band (fresh start: ONLY .env, no test data)
$envPath = Join-Path $Repo '.env'
if (-not (Test-Path $envPath)) {
    Warn '.env is missing. On the DEV machine run:'
    Write-Host "    tailscale file cp D:\VOS\.env $(hostname):"
    Write-Host 'then here:'
    Write-Host "    tailscale file get $Repo\"
    Fail 'Re-run this script once .env is in place'
}
$envText = Get-Content $envPath -Raw
foreach ($key in 'TELEGRAM_BOT_TOKEN', 'VOS_ALLOWED_USER_ID', 'NEO4J_PASSWORD', 'VOS_KIOSK_ENABLED', 'VOS_KIOSK_PIN') {
    if ($envText -notmatch "(?m)^\s*$key=.+") {
        Fail ".env is missing $key - set it NOW: NEO4J_PASSWORD bakes into the Neo4j volume on first start (changing later needs 'docker compose down -v')"
    }
}
Say '.env present with required keys'

# 7. Build image + start Neo4j (NOT the app - the dev bot may still be polling)
Say 'Building the app image...'
docker compose build
if ($LASTEXITCODE -ne 0) { Fail 'docker compose build failed' }

Say 'Starting Neo4j (first start bakes NEO4J_AUTH into its volume)...'
docker compose up -d neo4j
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up -d neo4j failed' }
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 5
    $health = docker inspect --format '{{.State.Health.Status}}' vos-neo4j
} while ($health -ne 'healthy' -and (Get-Date) -lt $deadline)
if ($health -ne 'healthy') { Fail "Neo4j is '$health' after 3 min - docker compose logs neo4j" }
Say 'Neo4j healthy'

Say 'Kiosk dependency smoke test (starts no Telegram poller)...'
docker compose run --rm --no-deps app python -c "import vos.shell; import vos.web.app; import faster_whisper; print('kiosk deps ok')"
if ($LASTEXITCODE -ne 0) { Fail 'kiosk smoke test failed' }

# 8. Publish the kiosk on the tailnet (loopback -> HTTPS *.ts.net; ADR-015)
Say 'Configuring tailscale serve...'
tailscale serve --bg http://127.0.0.1:8765
if ($LASTEXITCODE -ne 0) { Fail 'tailscale serve failed' }
tailscale serve status

# 9. Auto-deploy task: poll origin/main every 5 minutes (see pull-deploy.ps1)
if (Get-ScheduledTask -TaskName 'VOS deploy' -ErrorAction SilentlyContinue) {
    Say "Scheduled task 'VOS deploy' already registered"
} else {
    Say "Registering scheduled task 'VOS deploy' (every 5 min, current user session)..."
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $Repo\deploy\pull-deploy.ps1"
    Register-ScheduledTask -TaskName 'VOS deploy' -Trigger $trigger -Action $action | Out-Null
}

Write-Host ''
Say 'Bootstrap complete. Two manual items remain:'
Write-Host '  1. Autologon (Sysinternals) so containers survive unattended reboots'
Write-Host '     https://learn.microsoft.com/en-us/sysinternals/downloads/autologon'
Write-Host '  2. When ready to switch: STOP the dev bot, then run'
Write-Host "     $Repo\deploy\bootstrap-server.ps1 -Cutover"
