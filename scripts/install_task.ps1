# Registers a Task Scheduler task that starts the screener when this user logs on.
# Run once on the VPS from the repo folder:  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
param(
    [string]$TaskName = "XAU Screener",
    [int]$DelaySeconds = 60  # give the MT5 terminal time to start and log in first
)
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "run_screener.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$script`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT${DelaySeconds}S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
# Interactive: MT5's Python API only works in the same desktop session as the terminal.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered '$TaskName'. Start it now with: Start-ScheduledTask -TaskName '$TaskName'"
