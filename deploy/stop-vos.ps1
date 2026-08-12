<#
Stop VOS on this machine. The counterpart to start-vos.ps1.

    deploy\stop-vos.ps1        # stop the app - releases the Telegram poller
    deploy\stop-vos.ps1 -All   # also stop Neo4j

Stopping the app is what frees the exclusive Telegram long-poll, so this is the
first half of handing the bot back to another machine. Neo4j keeps running by
default: it holds nothing that conflicts, and leaving it up makes the next
start-vos.ps1 near-instant.

This only ever stops containers. It never runs `docker compose down`, and
especially not `down -v` - that would delete the neo4j-data volume (along with
the NEO4J_AUTH password baked into it on first start), vos-data (shopping.db),
and hf-cache (the ~250 MB whisper model). journal/, artifacts/ and cassettes/
are host bind mounts and survive either way, but the projections would need a
full rebuild for nothing.
#>
param([switch]$All)

$ErrorActionPreference = 'Stop'

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "XX  $msg" -ForegroundColor Red; exit 1 }

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
Say "Repo: $repo"

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say 'Docker daemon is not running - nothing to stop.'
    exit 0
}

Say 'Stopping the app (Telegram poller released)...'
docker compose stop app
if ($LASTEXITCODE -ne 0) { Fail 'docker compose stop app failed' }

if ($All) {
    Say 'Stopping Neo4j...'
    docker compose stop neo4j
    if ($LASTEXITCODE -ne 0) { Fail 'docker compose stop neo4j failed' }
}

Write-Host ''
docker compose ps
Write-Host ''
Say 'Stopped.'
if (-not $All) { Write-Host '  Neo4j is still running (use -All to stop it too)' }
Write-Host '  Start:  deploy\start-vos.ps1'
Warn 'restart: unless-stopped means an explicitly stopped container STAYS stopped,'
Warn 'across reboots too. Nothing brings VOS back but start-vos.ps1.'

exit 0
