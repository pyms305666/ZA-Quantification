$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskData = Join-Path $taskRoot '.tqsdk\research\decision_forward'
$taskMarker = Join-Path $taskData 'STOP_SUPERVISOR'
$taskLog = Join-Path $taskData 'supervisor.log'
Set-Location -LiteralPath $taskRoot
. (Join-Path $PSScriptRoot 'research_process_helpers.ps1')

if (-not ('FuturesResearchPower' -as [type])) {
    Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public static class FuturesResearchPower {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
}

function Test-ResearchProcess([string]$statusPath, [string]$scriptName) {
    return (@(Get-ResearchProcess (Join-Path $taskRoot "tools\$scriptName")).Count -gt 0)
}

if ([FuturesResearchPower]::SetThreadExecutionState([uint32]2147483649) -eq 0) {
    throw 'Could not request temporary automatic-sleep prevention.'
}
"$(Get-Date -Format o) Supervisor started; automatic idle sleep prevented while running" | Add-Content -LiteralPath $taskLog
try {
while (-not (Test-Path -LiteralPath $taskMarker)) {
    $parentStatus = Join-Path $taskRoot '.tqsdk\research\forward15m\status.json'
    $childStatus = Join-Path $taskData 'status.json'
    try {
        $parentAlive = Test-ResearchProcess $parentStatus 'forward_research.py'
        $childAlive = Test-ResearchProcess $childStatus 'forward_decision_accuracy.py'
        if (-not $parentAlive -or -not $childAlive) {
            "$(Get-Date -Format o) Restarting missing collectors parent=$parentAlive observer=$childAlive" | Add-Content -LiteralPath $taskLog
            & (Join-Path $PSScriptRoot 'start_decision_accuracy.ps1') | Add-Content -LiteralPath $taskLog
        }
    } catch {
        "$(Get-Date -Format o) Restart error $($_.Exception.GetType().Name): $($_.Exception.Message) at $($_.InvocationInfo.ScriptLineNumber)" | Add-Content -LiteralPath $taskLog
    }
    Start-Sleep -Seconds 20
}
"$(Get-Date -Format o) Supervisor stopped by marker" | Add-Content -LiteralPath $taskLog
} finally {
    [void][FuturesResearchPower]::SetThreadExecutionState([uint32]2147483648)
}
