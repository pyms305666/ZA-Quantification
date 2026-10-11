$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskScript = Join-Path $PSScriptRoot 'research_supervisor_launcher.pyw'
$taskData = Join-Path $taskRoot '.tqsdk\research\decision_forward'
$taskName = 'CodexFuturesDecisionResearch'
Remove-Item -LiteralPath (Join-Path $taskData 'STOP_SUPERVISOR') -ErrorAction SilentlyContinue
$taskPython = (Get-Command python -ErrorAction Stop).Source
$taskPythonw = Join-Path (Split-Path -Parent $taskPython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPythonw)) { throw "Windowless Python is missing: $taskPythonw" }
$taskAction = New-ScheduledTaskAction -Execute $taskPythonw -Argument "-B `"$taskScript`"" -WorkingDirectory $taskRoot
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    $oldScript = Join-Path $PSScriptRoot 'supervise_decision_research.ps1'
    if ($existing.Actions.Count -ne 1 -or ($existing.Actions[0].Arguments -notlike "*$taskScript*" -and $existing.Actions[0].Arguments -notlike "*$oldScript*")) {
        throw "Existing Task Scheduler entry '$taskName' has a different action; inspect it manually."
    }
    if ($existing.Principal.UserId -ne $env:USERNAME) {
        throw "Existing Task Scheduler entry '$taskName' is owned by another user."
    }
    Set-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger | Out-Null
} else {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Futures research collector supervisor; starts at user logon and places no brokerage orders.' | Out-Null
}
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
