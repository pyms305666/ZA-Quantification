$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskData = Join-Path $taskRoot '.tqsdk\research\forward15m'
$taskScript = Join-Path $taskRoot 'tools\forward_research.py'
. (Join-Path $PSScriptRoot 'research_process_helpers.ps1')
Start-ResearchCollector $taskScript $taskData
