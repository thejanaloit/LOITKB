# One-command LOITKB setup for a new Windows machine (or re-run safely on an existing one).
#
#   1. checks Docker + Python
#   2. installs pinned Python deps
#   3. creates the data dir and a machine-local loitkb.env with a random Qdrant API key
#      (never committed; readable only by the current user)
#   4. starts Qdrant with that key (an existing container is recreated; the named volume keeps the data)
#   5. writes ACL grants, syncs every source, runs health
#   6. registers the hourly + at-startup monitor task
#
# First sync downloads the embedding models once (~200 MB) into <data_dir>\models.
param(
    [string]$DataDir = $(if ($env:LOITKB_DATA_DIR) { $env:LOITKB_DATA_DIR } else { "C:\LIQA-memory" }),
    [string]$LiqaHome = $env:LIQA_HOME,
    [string]$AgencyHome = $env:LIQA_AGENCY_HOME,
    [string[]]$JiraProjects = @("PF"),
    [switch]$SkipTask
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

Step "Prerequisites"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is required: https://docs.docker.com/desktop/install/windows-install/" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python 3.11+ (py launcher) is required: https://www.python.org/downloads/" }
docker info --format "{{.ServerVersion}}" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker is installed but not running. Start Docker Desktop and re-run." }

Step "Python dependencies"
py -3 -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Step "Data dir and machine secrets: $DataDir"
New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir "logs") | Out-Null
$envFile = Join-Path $DataDir "loitkb.env"
$lines = @()
if (Test-Path $envFile) { $lines = Get-Content $envFile }
if (-not ($lines -match '^LOITKB_QDRANT_API_KEY=.+')) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $lines += "LOITKB_QDRANT_API_KEY=$key"
    Write-Host "Generated a new Qdrant API key."
}
if (-not ($lines -match '^LOITKB_REQUIRE_API_KEY=')) { $lines += "LOITKB_REQUIRE_API_KEY=1" }
$lines | Set-Content -Path $envFile -Encoding ascii
icacls $envFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

Step "Qdrant (Docker named volume, localhost only, API key on)"
docker compose --env-file $envFile up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$key = (Get-Content $envFile | Where-Object { $_ -like 'LOITKB_QDRANT_API_KEY=*' }) -replace '^LOITKB_QDRANT_API_KEY=', ''
$ready = $false
for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:6333/collections" -Headers @{ "api-key" = $key } -TimeoutSec 3 | Out-Null; $ready = $true }
    catch { Start-Sleep -Seconds 2 }
}
if (-not $ready) { throw "Qdrant did not become ready on 127.0.0.1:6333" }

$env:LOITKB_DATA_DIR = $DataDir
if ($LiqaHome) { $env:LIQA_HOME = $LiqaHome }
if ($AgencyHome) { $env:LIQA_AGENCY_HOME = $AgencyHome }

Step "Access grants"
$me = $env:USERNAME.ToLower()
py -3 -m loitkb grant $me "jira:*" "liqa:internal" | Out-Null
$liqaGrants = @("liqa:internal") + ($JiraProjects | ForEach-Object { "jira:project:$_" })
py -3 -m loitkb grant liqa @liqaGrants | Out-Null
py -3 -m loitkb grant evaluator "liqa:internal" | Out-Null
Write-Host "Grants: $me -> jira:*, liqa:internal ; liqa -> $($liqaGrants -join ', ')"

Step "Index every source"
py -3 -m loitkb sync
if ($LASTEXITCODE -ne 0) { throw "sync failed" }

Step "Health"
py -3 -m loitkb health | Out-Null
$health = Get-Content (Join-Path $DataDir "health.json") -Raw | ConvertFrom-Json
$health.checks | ForEach-Object { "{0,-18} {1}" -f $_.check, $_.status }
Write-Host "overall: $($health.overall)"

if (-not $SkipTask) {
    Step "Monitor task"
    $taskArgs = @{}
    if ($LiqaHome) { $taskArgs.LiqaHome = $LiqaHome }
    if ($AgencyHome) { $taskArgs.AgencyHome = $AgencyHome }
    & (Join-Path $PSScriptRoot "install-monitor-task.ps1") -DataDir $DataDir @taskArgs
}
Write-Host "`nLOITKB ready. Next: run E:\LIQA\scripts\bootstrap-new-machine.ps1 (or your LIQA checkout) to wire Cursor." -ForegroundColor Green
