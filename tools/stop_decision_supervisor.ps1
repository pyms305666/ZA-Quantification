$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskData = Join-Path $taskRoot '.tqsdk\research\decision_forward'
$taskName = 'CodexFuturesDecisionResearch'
'stop' | Set-Content -LiteralPath (Join-Path $taskData 'STOP_SUPERVISOR')
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
Write-Output 'Supervisor stopped. Market collectors are unchanged.'
