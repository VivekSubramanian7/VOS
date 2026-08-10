# VOS pull-based auto-deploy. Registered as a Task Scheduler job on the server
# (every 5 minutes, run as the signed-in user — docker needs that session).
# Polls origin/main and rebuilds only when there are new commits; no webhooks,
# no inbound ports (ADR-003). See DEPLOY.md for setup and rationale.

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

git fetch --quiet origin main
$local  = git rev-parse HEAD
$remote = git rev-parse origin/main
if ($local -eq $remote) { exit 0 }   # nothing new — silent no-op

$log = Join-Path $PSScriptRoot "deploy.log"
"$(Get-Date -Format s) deploying $remote" | Add-Content $log

# --ff-only: a diverged server checkout halts loudly instead of merging.
git pull --ff-only origin main *>> $log
if ($LASTEXITCODE -eq 0) {
    # Builds the new image BEFORE swapping containers — a broken build leaves
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
