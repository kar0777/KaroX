param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "KaroX"
$AppDir = Join-Path $RuntimeDir "app"
$SessionsDir = Join-Path $RuntimeDir "sessions"

function Get-CommandLine([int]$ProcessId) {
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        return [string]$process.CommandLine
    } catch { return "" }
}

function Test-KaroXRoleProcess([int]$ProcessId, [string]$Role) {
    $command = (Get-CommandLine $ProcessId).ToLowerInvariant().Replace("\", "/")
    if (!$command) { return $false }
    switch ($Role) {
        "server" {
            return ($command -match "uvicorn" -and $command -match "repo_tools:app|notion_entry:app|notion_gateway")
        }
        "tunnel" {
            return (($command -match "cloudflared" -and $command -match "tunnel") -or ($command -match "tailscale" -and $command -match "funnel"))
        }
        "runner" {
            return ($command -match "run-server|run-tunnel" -and $command -match "karox|repopilotbridge")
        }
    }
    return $false
}

function Stop-ProcessTree([int]$ProcessId, [string]$Reason) {
    if ($ProcessId -le 0 -or $ProcessId -eq $PID) { return }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (!$process) { return }
    Write-Host "Stopping KaroX $Reason process PID $ProcessId..." -ForegroundColor Yellow
    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
}

function Stop-RecordedSessions {
    if (!(Test-Path -LiteralPath $SessionsDir)) { return }
    foreach ($file in Get-ChildItem -LiteralPath $SessionsDir -Filter session.json -Recurse -File -ErrorAction SilentlyContinue) {
        try { $session = Get-Content -Raw -LiteralPath $file.FullName | ConvertFrom-Json } catch { continue }
        foreach ($entry in @(
            @{ Name = "tunnel"; Value = $session.tunnelPid },
            @{ Name = "server"; Value = $session.serverPid },
            @{ Name = "runner"; Value = $session.serverRunnerPid }
        )) {
            if (!$entry.Value) { continue }
            $candidate = 0
            if (![int]::TryParse([string]$entry.Value, [ref]$candidate)) { continue }
            if (Test-KaroXRoleProcess $candidate $entry.Name) {
                Stop-ProcessTree $candidate "$($entry.Name)/session"
            }
        }
    }
}

function Stop-OrphanedRuntimeProcesses {
    $ancestorIds = New-Object 'System.Collections.Generic.HashSet[int]'
    $cursor = $PID
    for ($i = 0; $i -lt 20 -and $cursor -gt 0; $i++) {
        [void]$ancestorIds.Add([int]$cursor)
        try {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$cursor" -ErrorAction Stop
            $cursor = [int]$current.ParentProcessId
        } catch { break }
    }
    foreach ($process in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
        $id = [int]$process.ProcessId
        if ($ancestorIds.Contains($id)) { continue }
        $command = ([string]$process.CommandLine).ToLowerInvariant().Replace("\", "/")
        if (!$command) { continue }
        $isServer = $command -match "uvicorn" -and $command -match "repo_tools:app|notion_entry:app|notion_gateway"
        $isTunnel = ($command -match "cloudflared" -and $command -match "tunnel") -or ($command -match "tailscale" -and $command -match "funnel")
        $isSessionRunner = $command -match "run-server|run-tunnel" -and $command -match "karox|repopilotbridge"
        if ($isServer -or $isTunnel -or $isSessionRunner) {
            Stop-ProcessTree $id "runtime"
        }
    }
}

function Wait-AppReleased {
    for ($i = 0; $i -lt 30; $i++) {
        $busy = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $command = ([string]$_.CommandLine).ToLowerInvariant().Replace("\", "/")
            $command -match "uvicorn" -and $command -match "repo_tools:app|notion_entry:app|notion_gateway"
        })
        if ($busy.Count -eq 0) { return }
        Start-Sleep -Milliseconds 500
    }
}

function Stop-KaroXForUpdate {
    Stop-RecordedSessions
    Stop-OrphanedRuntimeProcesses
    Wait-AppReleased
}

if (!(Test-Path -LiteralPath $Installer)) { throw "KaroX installer was not found: $Installer" }
Stop-KaroXForUpdate

$attempts = 4
for ($attempt = 1; $attempt -le $attempts; $attempt++) {
    Write-Host "KaroX installation attempt $attempt/$attempts" -ForegroundColor Cyan
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Installer)
    if ($Start) { $arguments += "-Start" }
    & powershell @arguments
    $code = $LASTEXITCODE
    if ($code -eq 0) { exit 0 }
    if ($attempt -lt $attempts) {
        Write-Host "Installer returned exit code $code. Releasing KaroX processes and retrying..." -ForegroundColor Yellow
        Stop-KaroXForUpdate
        Start-Sleep -Seconds 2
    }
}
throw "KaroX installer failed after $attempts attempts. Close remaining KaroX windows and retry."
