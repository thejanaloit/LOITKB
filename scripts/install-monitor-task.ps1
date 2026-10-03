# Registers the "LOITKB Monitor" task: sync changed sources, health check, daily
# snapshot backup, daily evaluator. Survives reboots (at-startup trigger) and, when
# run from an elevated shell, runs whether or not the user is logged on (S4U).
#
# Paths are baked into the task so a wrong default can never point sync at an empty
# folder. Secrets are not: they stay in <data_dir>\loitkb.env.
param(
    [int]$EveryMinutes = 60,
    [string]$LiqaHome = $(if ($env:LIQA_HOME) { $env:LIQA_HOME } else { Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "LIQA" }),
    [string]$AgencyHome = $(if ($env:LIQA_AGENCY_HOME) { $env:LIQA_AGENCY_HOME } else { Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "agency-agents" }),
    [string]$DataDir = $(if ($env:LOITKB_DATA_DIR) { $env:LOITKB_DATA_DIR } else { "C:\LIQA-memory" })
)
$ErrorActionPreference = "Stop"

# Original workstation layout, used only when the sibling checkout is absent.
if (-not (Test-Path $LiqaHome) -and (Test-Path "E:\LIQA")) { $LiqaHome = "E:\LIQA" }
if (-not (Test-Path $AgencyHome) -and (Test-Path "E:\agency-agents")) { $AgencyHome = "E:\agency-agents" }

foreach ($p in @($LiqaHome, $AgencyHome)) {
    if (-not (Test-Path $p)) { throw "Path not found: $p. Pass -LiqaHome / -AgencyHome or set LIQA_HOME / LIQA_AGENCY_HOME." }
}

$repo = Split-Path -Parent $PSScriptRoot
$py = (Get-Command py -ErrorAction Stop).Source
$log = Join-Path $DataDir "logs\monitor.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# Keep monitor.log bounded: roll it at 10 MB before each run.
$roll = "if exist `"$log`" for %%F in (`"$log`") do if %%~zF GTR 10485760 move /y `"$log`" `"$log.1`" >nul"
$envs = "set `"LIQA_HOME=$LiqaHome`"&& set `"LIQA_AGENCY_HOME=$AgencyHome`"&& set `"LOITKB_DATA_DIR=$DataDir`""
$runner = Join-Path $DataDir "run-monitor.cmd"
@"
@echo off
$roll
$envs
cd /d "$repo"
"$py" -3 -m loitkb monitor >> "$log" 2>&1
"@ | Set-Content -Path $runner -Encoding ascii

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`""
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew

$user = "$env:USERDOMAIN\$env:USERNAME"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    $mode = "S4U (runs without an interactive logon)"
    $boot = New-ScheduledTaskTrigger -AtStartup
    $bootLabel = "startup"
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $mode = "Interactive (re-run elevated for S4U so it runs while logged off)"
    # AtStartup triggers need admin; a non-admin task catches up at logon instead.
    $boot = New-ScheduledTaskTrigger -AtLogOn -User $user
    $bootLabel = "logon"
}
$boot.Delay = "PT5M"   # give Docker Desktop time to start Qdrant
Register-ScheduledTask -TaskName "LOITKB Monitor" -Action $action -Trigger @($repeat, $boot) -Settings $settings -Principal $principal `
    -Description "LOITKB RAG sync, health, backup and evaluator" -Force | Out-Null
Write-Host "Registered 'LOITKB Monitor' every $EveryMinutes min + at $bootLabel. Mode: $mode"
Write-Host "LIQA_HOME=$LiqaHome  LIQA_AGENCY_HOME=$AgencyHome  LOITKB_DATA_DIR=$DataDir"
Write-Host "Log: $log"
