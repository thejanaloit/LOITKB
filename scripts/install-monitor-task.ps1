# Registers the hourly "LOITKB Monitor" task: sync changed sources, health check,
# daily snapshot backup, daily evaluator. Runs as the current user.
param([int]$EveryMinutes = 60)

$repo = Split-Path -Parent $PSScriptRoot
$py = (Get-Command py -ErrorAction Stop).Source
$log = "C:\LIQA-memory\logs\monitor.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$cmd = "cmd.exe"
$arg = "/c cd /d `"$repo`" && `"$py`" -3 -m loitkb monitor >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute $cmd -Argument $arg
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName "LOITKB Monitor" -Action $action -Trigger $trigger -Settings $settings -Description "LOITKB RAG health, backup and evaluator" -Force | Out-Null
Write-Host "Registered 'LOITKB Monitor' every $EveryMinutes minutes. Log: $log"
