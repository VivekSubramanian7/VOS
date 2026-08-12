# VOS pull-based auto-deploy. Registered as a Task Scheduler job on the server
# (every 5 minutes, run as the signed-in user - docker needs that session).
# Polls origin/main and rebuilds only when there are new commits; no webhooks,
# no inbound ports (ADR-003). See DEPLOY.md for setup and rationale.

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$log = Join-Path $PSScriptRoot "deploy.log"

# Auto-deploy is SERVER-ONLY. The marker file is created by bootstrap-server.ps1
# and is git-ignored, so it can never travel in a clone: a dev checkout that
# happens to have this scheduled task registered pulls nothing and rebuilds
# nothing. Deploying on a second machine would start a second Telegram poller,
# and two pollers on one token fight with 409 Conflict.
if (-not (Test-Path (Join-Path $PSScriptRoot '.is-server'))) { exit 0 }

git fetch --quiet origin main
$local  = git rev-parse HEAD
$remote = git rev-parse origin/main
if ($local -eq $remote) { exit 0 }   # nothing new - silent no-op

# Deploy only when origin/main strictly CONTAINS this checkout. Comparing the
# two hashes for inequality is not enough: a checkout that is AHEAD (an unpushed
# local commit) or diverged also compares unequal, and would then rebuild and
# restart the stack on every single run, every 5 minutes, forever.
git merge-base --is-ancestor HEAD origin/main
if ($LASTEXITCODE -ne 0) {
    "$(Get-Date -Format s) skipped: HEAD $($local.Substring(0,8)) is not behind origin/main (ahead or diverged)" | Add-Content $log
    exit 0
}

"$(Get-Date -Format s) deploying $remote" | Add-Content $log

# --ff-only: a diverged server checkout halts loudly instead of merging.
git pull --ff-only origin main *>> $log
if ($LASTEXITCODE -eq 0) {
    # Builds the new image BEFORE swapping containers - a broken build leaves
    # the old version running untouched.
    docker compose up -d --build *>> $log
}
$code = $LASTEXITCODE
"$(Get-Date -Format s) exit=$code" | Add-Content $log
if ($code -eq 0) { exit 0 }

# Failed deploy: ping the owner through the bot's own token. The previously
# deployed version is still running, so the bot itself still delivers this.
$envLines = Get-Content (Join-Path $repo ".env") -ErrorAction SilentlyContinue
$token = ($envLines | Where-Object { $_ -match '^TELEGRAM_BOT_TOKEN=' } | Select-Object -First 1) -replace '^TELEGRAM_BOT_TOKEN=', ''
$chat  = ($envLines | Where-Object { $_ -match '^VOS_ALLOWED_USER_ID=' } | Select-Object -First 1) -replace '^VOS_ALLOWED_USER_ID=', ''
if ($token -and $chat) {
    $body = @{ chat_id = $chat; text = "VOS deploy of $($remote.Substring(0,8)) FAILED (exit $code). Old version still running. See deploy/deploy.log on the server." }
    try { Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body $body | Out-Null } catch {}
}
exit $code
