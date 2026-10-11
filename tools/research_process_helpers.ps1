$ErrorActionPreference = 'Stop'

function Get-ResearchProcess([string]$scriptPath) {
    $absoluteScript = (Resolve-Path -LiteralPath $scriptPath).Path
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.IndexOf($absoluteScript, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $_.CommandLine -match '(?i)(?:^|\s)run(?:\s|$)'
        }
}

function Save-InvalidResearchStatus([string]$statusPath) {
    if (-not (Test-Path -LiteralPath $statusPath)) { return }
    $raw = [IO.File]::ReadAllText($statusPath)
    try {
        $snapshot = $raw | ConvertFrom-Json -ErrorAction Stop
        if (-not $snapshot -or -not $snapshot.pid -or -not $snapshot.wall_ms -or $raw.IndexOf([char]0) -ge 0) {
            throw 'Status is not a complete collector snapshot.'
        }
    } catch {
        $archive = Join-Path (Split-Path -Parent $statusPath) 'recovery'
        New-Item -ItemType Directory -Path $archive -Force | Out-Null
        $destination = Join-Path $archive ("status.invalid.{0}.{1}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), [guid]::NewGuid().ToString('N'))
        Move-Item -LiteralPath $statusPath -Destination $destination
        Write-Output "Preserved invalid status at $destination"
    }
}

function Start-ResearchCollector([string]$scriptPath, [string]$dataPath) {
    $existing = @(Get-ResearchProcess $scriptPath)
    if ($existing.Count -gt 1) { throw "Multiple collectors already exist for $scriptPath" }
    if ($existing.Count -eq 1) {
        Write-Output "Collector already running, PID $($existing[0].ProcessId)"
        return
    }
    Save-InvalidResearchStatus (Join-Path $dataPath 'status.json')
    $python = if ($env:FUTURES_RESEARCH_PYTHON) { $env:FUTURES_RESEARCH_PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { throw "Windowless Python is missing: $pythonw" }
    $arguments = @('-B', ('"' + $scriptPath + '"'), 'run')
    $process = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataPath 'service.stdout.log') -RedirectStandardError (Join-Path $dataPath 'service.stderr.log') -PassThru
    Write-Output "Collector started without a console, PID $($process.Id), script $scriptPath"
}
