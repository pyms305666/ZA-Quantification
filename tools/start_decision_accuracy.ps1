$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskData = Join-Path $taskRoot '.tqsdk\research\decision_forward'
$taskScript = Join-Path $taskRoot 'tools\forward_decision_accuracy.py'
. (Join-Path $PSScriptRoot 'research_process_helpers.ps1')
# The selector depends on the original market collector's fresh status.
& (Join-Path $PSScriptRoot 'start_forward_research.ps1')
Start-ResearchCollector $taskScript $taskData
