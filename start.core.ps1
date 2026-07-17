$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$KaroXVersion = ""
try { $KaroXVersion = (Get-Content -Raw -LiteralPath (Join-Path $Root "VERSION") -ErrorAction Stop).Trim() } catch {}
$ConfigDir = Join-Path $env:APPDATA "RepoPilotBridge"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$ReposFile = Join-Path $ConfigDir "repos.json"
$SettingsFile = Join-Path $ConfigDir "settings.json"
$LogsDir = Join-Path $RuntimeDir "logs"
$SessionsDir = Join-Path $RuntimeDir "sessions"
$PythonExe = Join-Path $RuntimeDir ".venv\Scripts\python.exe"

function Resolve-ServerDir($root) {
    $candidates = @(
        (Join-Path $root "server"),
        (Join-Path (Join-Path $root "server") "server"),
        $root
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate "repo_tools.py")) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $language = ([string]$env:KAROX_LANGUAGE).Trim().ToLowerInvariant()
    $prefix = if ($language -eq "ru") { "repo_tools.py не найден. Проверенные папки: " } else { "repo_tools.py was not found. Checked: " }
    throw ($prefix + ($candidates -join ', '))
}

$ServerDir = Resolve-ServerDir $Root

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
New-Item -ItemType Directory -Force -Path $SessionsDir | Out-Null

function Ask-Yes($text, $defaultYes = $true) {
    $suffix = if ((Get-SelectedLanguage) -eq "ru") {
        if ($defaultYes) { "Д/н" } else { "д/Н" }
    } else {
        if ($defaultYes) { "Y/n" } else { "y/N" }
    }
    $answer = Read-Host "$text [$suffix]"
    if (!$answer) { return $defaultYes }
    return $answer -match "^[ДдYy]"
}

function Has-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $extra = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps"
    ) -join ";"
    $env:Path = "$machine;$user;$extra"
}

function Install-WingetPackage($id, $name) {
    Refresh-Path
    if (!(Has-Command "winget")) {
        Write-Host (L "winget was not found. Install App Installer from Microsoft Store and retry." "winget не найден. Установите App Installer из Microsoft Store и повторите установку.") -ForegroundColor Red
        return $false
    }

    Write-Host ((L "Installing via winget: " "Устанавливаю через winget: ") + $name) -ForegroundColor Yellow
    & winget install -e --id $id --accept-source-agreements --accept-package-agreements
    $ok = ($LASTEXITCODE -eq 0)
    Refresh-Path
    return $ok
}

function Find-Cloudflared {
    Refresh-Path
    $cmd = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    $known = @(
        "$env:LOCALAPPDATA\KaroX\bin\cloudflared.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\cloudflared.exe",
        "$env:ProgramFiles\Cloudflare\cloudflared.exe"
    )
    foreach ($p in $known) { if (Test-Path $p) { return $p } }
    throw (L "cloudflared was not found. KaroX can download it automatically: launch again and confirm, or install manually: winget install Cloudflare.cloudflared" "cloudflared не найден. KaroX может скачать его автоматически: запустите ещё раз и подтвердите скачивание, либо установите вручную: winget install Cloudflare.cloudflared")
}

function Install-Cloudflared {
    $target = "$env:LOCALAPPDATA\KaroX\bin\cloudflared.exe"
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Write-Host ((L "Downloading cloudflared: " "Скачиваю cloudflared: ") + $url) -ForegroundColor Yellow
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing
        if ((Get-Item -LiteralPath $target).Length -gt 10MB) {
            Write-Host ((L "cloudflared installed: " "cloudflared установлен: ") + $target) -ForegroundColor Green
            return $target
        }
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        Write-Host (L "Downloaded file looks too small, discarded." "Скачанный файл подозрительно мал, удалён.") -ForegroundColor Yellow
    } catch {
        Write-Host ((L "Download failed: " "Не удалось скачать: ") + $_.Exception.Message) -ForegroundColor Yellow
    }
    if (Install-WingetPackage "Cloudflare.cloudflared" "cloudflared") {
        try { return Find-Cloudflared } catch {}
    }
    return $null
}

function Find-Tailscale {
    Refresh-Path
    foreach ($name in @("tailscale.exe", "tailscale")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    }

    $known = @(
        "$env:ProgramFiles\Tailscale\tailscale.exe",
        "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\tailscale.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\tailscale.exe"
    )
    foreach ($p in $known) { if ($p -and (Test-Path $p)) { return $p } }

    throw (L "tailscale was not found. Install Tailscale, sign in, and select Tailscale Funnel in KaroX settings again." "tailscale не найден. Установите Tailscale, войдите в аккаунт и выберите Tailscale Funnel в настройках KaroX ещё раз.")
}

function Normalize-TunnelProvider($provider) {
    $value = ([string]$provider).Trim().ToLowerInvariant()
    if ($value -in @("tailscale", "ts")) { return "tailscale" }
    return "cloudflare"
}

function Get-ProviderLabel($provider) {
    $provider = Normalize-TunnelProvider $provider
    if ($provider -eq "tailscale") { return "Tailscale Funnel" }
    return "Cloudflare Tunnel"
}

function Normalize-AiClient($client) {
    $value = ([string]$client).Trim().ToLowerInvariant()
    if ($value -in @("letaido")) { return "letaido" }
    if ($value -in @("promptql", "prompt.ql", "prompt_ql")) { return "promptql" }
    return "other"
}

function Get-AiClientLabel($client) {
    $client = Normalize-AiClient $client
    if ($client -eq "letaido") { return "letaido.com" }
    if ($client -eq "promptql") { return "prompt.ql.app" }
    return (L "Other client" "Сторонний сервис")
}


function Normalize-Language($language) {
    $value = ([string]$language).Trim().ToLowerInvariant()
    if ($value -in @("ru", "русский", "russian")) { return "ru" }
    return "en"
}

function Get-SelectedLanguage {
    $override = ([string]$env:KAROX_LANGUAGE).Trim().ToLowerInvariant()
    if ($override -in @("en", "ru")) { return $override }
    $settings = Load-Settings
    return (Normalize-Language $settings.language)
}

function Select-Language {
    $override = ([string]$env:KAROX_LANGUAGE).Trim().ToLowerInvariant()
    if ($override -in @("en", "ru")) { return $override }
    Clear-Host
    Write-Host ""
    Write-Host "                         ★" -ForegroundColor Yellow
    Write-Host "                 STAR FOR KAROX" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "  Choose your language / Выберите язык" -ForegroundColor White
    Write-Host ""
    Write-Host "  [1] English" -ForegroundColor Cyan
    Write-Host "  [2] Русский" -ForegroundColor Cyan
    Write-Host ""
    $rawChoice = Read-Host "  Language / Язык"
    if ($null -eq $rawChoice) {
        throw "Interactive language input is unavailable. Set KAROX_LANGUAGE=en or KAROX_LANGUAGE=ru."
    }
    $choice = ([string]$rawChoice).Trim()
    if ($choice -eq "2" -or $choice -match "^[Rr][Uu]$") { return "ru" }
    if ($choice -eq "1" -or $choice -match "^[Ee][Nn]$") { return "en" }
    throw "Choose 1 or 2 / Выберите 1 или 2."
}

function Ensure-Language {
    $needsChoice = $true
    if (Test-Path $SettingsFile) {
        try {
            $raw = Get-Content -Raw -LiteralPath $SettingsFile | ConvertFrom-Json
            $needsChoice = (!$raw -or ($raw.PSObject.Properties.Name -notcontains "language"))
        } catch {}
    }
    if ($needsChoice) {
        $language = Select-Language
        $settings = Load-Settings
        if ($settings.PSObject.Properties.Name -notcontains "language") {
            $settings | Add-Member -NotePropertyName "language" -NotePropertyValue $language -Force
        } else { $settings.language = $language }
        Save-Settings $settings
    }
}

function L($en, $ru) {
    if ((Get-SelectedLanguage) -eq "ru") { return $ru }
    return $en
}


function Load-Settings {
    $fallback = [pscustomobject]@{ tunnelProvider = "cloudflare"; aiClient = "promptql"; language = "en" }
    if (!(Test-Path $SettingsFile)) { return $fallback }
    try {
        $settings = Get-Content -Raw -LiteralPath $SettingsFile | ConvertFrom-Json
        if (!$settings) { return $fallback }
        if ($settings.PSObject.Properties.Name -notcontains "tunnelProvider") {
            $settings | Add-Member -NotePropertyName "tunnelProvider" -NotePropertyValue "cloudflare" -Force
        }
        if ($settings.PSObject.Properties.Name -notcontains "aiClient") {
            $settings | Add-Member -NotePropertyName "aiClient" -NotePropertyValue "promptql" -Force
        }
        if ($settings.PSObject.Properties.Name -notcontains "language") {
            $settings | Add-Member -NotePropertyName "language" -NotePropertyValue "en" -Force
        }
        $settings.tunnelProvider = Normalize-TunnelProvider $settings.tunnelProvider
        $settings.aiClient = Normalize-AiClient $settings.aiClient
        $settings.language = Normalize-Language $settings.language
        return $settings
    } catch { return $fallback }
}


function Save-Settings($settings) {
    $settings.tunnelProvider = Normalize-TunnelProvider $settings.tunnelProvider
    if ($settings.PSObject.Properties.Name -notcontains "aiClient") {
        $settings | Add-Member -NotePropertyName "aiClient" -NotePropertyValue "promptql" -Force
    }
    if ($settings.PSObject.Properties.Name -notcontains "language") {
        $settings | Add-Member -NotePropertyName "language" -NotePropertyValue "en" -Force
    }
    $settings.aiClient = Normalize-AiClient $settings.aiClient
    $settings.language = Normalize-Language $settings.language
    $settings | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $SettingsFile -Encoding UTF8
}

function Get-SelectedTunnelProvider {
    $settings = Load-Settings
    return (Normalize-TunnelProvider $settings.tunnelProvider)
}

function Get-SelectedAiClient {
    $settings = Load-Settings
    return (Normalize-AiClient $settings.aiClient)
}

function Ensure-Installed {
    $missing = @()
    if (!(Test-Path $PythonExe)) { $missing += "Python runtime / virtualenv RepoPilot" }
    if (!(Get-Command git -ErrorAction SilentlyContinue)) { $missing += "Git" }

    if ($missing.Count -gt 0) {
        Write-Host (L "Missing components:" "Не хватает компонентов:") -ForegroundColor Yellow
        foreach ($m in $missing) { Write-Host " - $m" }
        if (Ask-Yes (L "Run install.ps1 now?" "Запустить install.ps1 сейчас?") $true) {
            powershell -ExecutionPolicy Bypass -File (Join-Path $Root "install.ps1")
        } else { throw (L "Setup cancelled." "Настройка отменена.") }
    }
}

function Load-Repos {
    if (!(Test-Path $ReposFile)) { "[]" | Set-Content $ReposFile -Encoding UTF8 }
    $raw = Get-Content $ReposFile -Raw
    if (!$raw) { return @() }
    try {
        $items = $raw | ConvertFrom-Json
        if ($null -eq $items) { return @() }
        return @($items)
    } catch { return @() }
}

function Save-Repo($path) {
    $path = (Resolve-Path -LiteralPath $path).Path
    $repos = @(Load-Repos)
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($r in $repos) {
        if ($r -and (Test-Path -LiteralPath $r)) {
            if (!$list.Contains([string]$r)) { $list.Add([string]$r) }
        }
    }
    if (!$list.Contains($path)) { $list.Add($path) }
    $list | ConvertTo-Json | Set-Content $ReposFile -Encoding UTF8
}

function Ensure-GitRepo($path) {
    $path = $path.Trim().Trim([char]34)
    if (!(Test-Path -LiteralPath $path)) {
        throw ((L "Folder not found: " "Папка не найдена: ") + $path)
    }

    $path = (Resolve-Path -LiteralPath $path).Path
    if (Test-Path -LiteralPath (Join-Path $path ".git")) {
        return $path
    }

    Write-Host ""
    Write-Host ((L "This folder is not a Git repository: " "В этой папке нет Git-репозитория: ") + $path) -ForegroundColor Yellow
    Write-Host (L "KaroX uses Git for diffs, branches, and safe commits." "KaroX использует Git для diff, веток и безопасного commit.")
    if (!(Ask-Yes (L "Initialize Git in this folder now?" "Инициализировать Git в этой папке сейчас?") $true)) {
        throw ((L "Folder is not a Git repository: " "Папка не является git-репозиторием: ") + $path)
    }

    & git -C $path init | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ((L "Could not run git init in: " "Не удалось выполнить git init в папке: ") + $path) }

    & git -C $path config user.email "repopilot@example.local" | Out-Null
    & git -C $path config user.name "Star For KaroX" | Out-Null
    & git -C $path commit --allow-empty -m "chore: initialize repository for RepoPilot" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ((L "Could not create the initial empty commit in: " "Не удалось создать стартовый empty commit в: ") + $path) }

    Write-Host (L "Git repository initialized." "Git-репозиторий инициализирован.") -ForegroundColor Green
    return $path
}

function Get-Branch($repo) { return (& git -C $repo branch --show-current).Trim() }

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Wait-LocalApi($apiKey, $port) {
    $headers = @{ "X-API-Key" = $apiKey }
    for ($i=0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -Headers $headers -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
    }
    return $false
}

function Get-LogTailText($paths, $tail = 80) {
    $chunks = @()
    foreach ($path in @($paths)) {
        if (Test-Path -LiteralPath $path) {
            $lines = @(Get-Content -LiteralPath $path -Tail $tail -ErrorAction SilentlyContinue)
            if ($lines.Count -gt 0) {
                $chunks += "---- $path"
                $chunks += $lines
            }
        }
    }
    if ($chunks.Count -eq 0) { return "" }
    return [string]::Join([Environment]::NewLine, $chunks)
}

function Get-LogText($paths) {
    $chunks = @()
    foreach ($path in @($paths)) {
        if (Test-Path -LiteralPath $path) {
            $text = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
            if ($text) { $chunks += $text }
        }
    }
    if ($chunks.Count -eq 0) { return "" }
    return [string]::Join([Environment]::NewLine, $chunks)
}

function Wait-ServerPidFile($pidFile, $runnerPid, $logPaths) {
    for ($i=0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-Path -LiteralPath $pidFile) {
            $raw = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue).Trim()
            if ($raw -match "^\d+$") { return [int]$raw }
        }
        if ($runnerPid -and !(Test-ProcessAlive $runnerPid)) {
            $tail = Get-LogTailText $logPaths
            if (!$tail) { $tail = L "Logs are empty." "Логи пустые." }
            throw ((L "Local API failed to start. The runner exited early." "Локальный API не смог запуститься. Runner завершился раньше времени.") + "`n" + $tail)
        }
    }
    return $null
}

function Wait-LocalApiProcess($apiKey, $port, $serverPid, $runnerPid, $logPaths) {
    $headers = @{ "X-API-Key" = $apiKey }
    for ($i=0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if ($serverPid -and !(Test-ProcessAlive $serverPid)) {
            $tail = Get-LogTailText $logPaths
            if (!$tail) { $tail = L "Logs are empty." "Логи пустые." }
            throw ((L "Local API exited before becoming ready." "Локальный API завершился до готовности.") + "`n" + $tail)
        }
        if ($runnerPid -and !(Test-ProcessAlive $runnerPid) -and !$serverPid) {
            $tail = Get-LogTailText $logPaths
            if (!$tail) { $tail = L "Logs are empty." "Логи пустые." }
            throw ((L "Local API runner exited before becoming ready." "Локальный API runner завершился до готовности.") + "`n" + $tail)
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -Headers $headers -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
    }
    return $false
}

function Escape-PsSingleQuoted($value) {
    return ([string]$value).Replace("'", "''")
}

function ConvertTo-Base64Utf8($value) {
    return [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes([string]$value))
}

function Write-Utf8BomText($path, $text) {
    $encoding = [System.Text.UTF8Encoding]::new($true)
    [System.IO.File]::WriteAllText($path, $text, $encoding)
}

function Write-ServerRunner($scriptPath, $pidFile, $repo, $apiKey, $mode, $branch, $sessionTitle, $commitAllowed, $runtimeDir, $serverLog, $runsDir, $serverDir, $pythonExe, $port, $serverOutLog, $serverErrLog) {
    $repoEsc = Escape-PsSingleQuoted $repo
    $apiKeyEsc = Escape-PsSingleQuoted $apiKey
    $modeEsc = Escape-PsSingleQuoted $mode
    $branchEsc = Escape-PsSingleQuoted $branch
    $sessionTitleEsc = Escape-PsSingleQuoted $sessionTitle
    $commitAllowedEsc = Escape-PsSingleQuoted $commitAllowed
    $runtimeDirEsc = Escape-PsSingleQuoted $runtimeDir
    $serverLogEsc = Escape-PsSingleQuoted $serverLog
    $runsDirEsc = Escape-PsSingleQuoted $runsDir
    $serverDirEsc = Escape-PsSingleQuoted $serverDir
    $pythonExeEsc = Escape-PsSingleQuoted $pythonExe
    $serverOutLogEsc = Escape-PsSingleQuoted $serverOutLog
    $serverErrLogEsc = Escape-PsSingleQuoted $serverErrLog
    $pidFileEsc = Escape-PsSingleQuoted $pidFile

    $content = @"
`$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > `$null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    `$OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

`$env:REPO_ROOT = '$repoEsc'
`$env:REPO_TOOLS_API_KEY = '$apiKeyEsc'
`$env:REPO_TOOLS_MODE = '$modeEsc'
`$env:REPO_TOOLS_BRANCH = '$branchEsc'
`$env:REPO_TOOLS_SESSION_TITLE = '$sessionTitleEsc'
`$env:REPO_TOOLS_INITIAL_TASK = ''
`$env:REPO_TOOLS_COMMIT_ALLOWED = '$commitAllowedEsc'
`$env:REPO_TOOLS_HOME = '$runtimeDirEsc'
`$env:REPO_TOOLS_LOG_FILE = '$serverLogEsc'
`$env:REPO_TOOLS_RUNS_DIR = '$runsDirEsc'
`$env:PYTHONPATH = '$serverDirEsc' + [System.IO.Path]::PathSeparator + `$env:PYTHONPATH

Set-Location -LiteralPath '$serverDirEsc'
`$p = Start-Process -FilePath '$pythonExeEsc' -ArgumentList @('-m', 'uvicorn', 'repo_tools:app', '--host', '127.0.0.1', '--port', '$port') -WorkingDirectory '$serverDirEsc' -RedirectStandardOutput '$serverOutLogEsc' -RedirectStandardError '$serverErrLogEsc' -WindowStyle Hidden -PassThru
Set-Content -LiteralPath '$pidFileEsc' -Value `$p.Id -Encoding ASCII
Wait-Process -Id `$p.Id
"@
    Write-Utf8BomText $scriptPath $content
}

function Invoke-SessionApi($apiKey, $port, $path) {
    $headers = @{ "X-API-Key" = $apiKey }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$port$path" -Headers $headers -TimeoutSec 5
        $content = $null
        if ($response.RawContentStream) {
            $response.RawContentStream.Position = 0
            $reader = [System.IO.StreamReader]::new($response.RawContentStream, [System.Text.Encoding]::UTF8)
            try {
                $content = $reader.ReadToEnd()
            } finally {
                $reader.Dispose()
            }
        }
        if (!$content) { $content = [string]$response.Content }
        $value = $content | ConvertFrom-Json
        return [pscustomobject]@{ ok = $true; value = $value; error = $null }
    } catch {
        return [pscustomobject]@{ ok = $false; value = $null; error = $_.Exception.Message }
    }
}

function Assert-SessionIsolation($apiKey, $port, $expectedRepo, $expectedBranch, $expectedMode) {
    $health = Invoke-SessionApi $apiKey $port "/health"
    if (!$health.ok) {
        throw ((L "The new session did not answer /health: " "Новая сессия не отвечает на /health: ") + $health.error)
    }

    $actualRepo = [string]$health.value.repoRoot
    if ($actualRepo -and ((Resolve-Path -LiteralPath $actualRepo).Path -ne (Resolve-Path -LiteralPath $expectedRepo).Path)) {
        throw ((L "The new session returned the wrong project. Expected: " "Новая сессия отвечает не тем проектом. Ожидался: ") + $expectedRepo + (L ", received: " ", получен: ") + $actualRepo)
    }

    $session = Invoke-SessionApi $apiKey $port "/session"
    if (!$session.ok) {
        throw ((L "The new session did not answer /session: " "Новая сессия не отвечает на /session: ") + $session.error)
    }

    $actualBranch = [string]$session.value.branch
    $actualMode = [string]$session.value.mode
    if ($actualBranch -and $actualBranch -ne $expectedBranch) {
        throw ((L "The new session returned the wrong branch. Expected: " "Новая сессия отвечает не той веткой. Ожидалась: ") + $expectedBranch + (L ", received: " ", получена: ") + $actualBranch)
    }
    if ($actualMode -and $actualMode -ne $expectedMode) {
        throw ((L "The new session returned the wrong mode. Expected: " "Новая сессия отвечает не тем режимом. Ожидался: ") + $expectedMode + (L ", received: " ", получен: ") + $actualMode)
    }

    return [pscustomobject]@{
        repoRoot = $actualRepo
        branch = $actualBranch
        mode = $actualMode
    }
}

function Get-TunnelUrlPattern($provider) {
    $provider = Normalize-TunnelProvider $provider
    if ($provider -eq "tailscale") { return "https://[a-zA-Z0-9.-]+\.ts\.net(?::\d+)?" }
    return "https://[a-zA-Z0-9-]+\.trycloudflare\.com"
}

function Get-TailscaleFunnelEnableUrlFromText($text) {
    if (!$text) { return $null }
    $match = [regex]::Match([string]$text, "https://login\.tailscale\.com/f/funnel\?[^\s]+")
    if (!$match.Success) { return $null }
    return $match.Value.TrimEnd([char[]]".,;)]")
}

function Get-TailscaleFunnelEnableUrlFromLogs($paths) {
    return Get-TailscaleFunnelEnableUrlFromText (Get-LogText $paths)
}

function Invoke-TailscaleFunnelEnableFlow($enableUrl) {
    if (!$enableUrl) { return }

    Write-Host ""
    Write-Host (L "Tailscale requires Funnel to be enabled for this tailnet." "Tailscale просит включить Funnel для этого tailnet.") -ForegroundColor Yellow
    Write-Host (L "Enable URL:" "Ссылка включения:") -ForegroundColor Cyan
    Write-Host $enableUrl -ForegroundColor White

    try {
        Set-Clipboard -Value $enableUrl
        Write-Host (L "URL copied to the clipboard." "Ссылка скопирована в буфер обмена.") -ForegroundColor Green
    } catch {
        Write-Host ((L "Could not copy the URL: " "Не удалось скопировать ссылку в буфер: ") + $_.Exception.Message) -ForegroundColor Yellow
    }

    try {
        Start-Process $enableUrl | Out-Null
        Write-Host (L "Opened the URL in your browser. Approve Funnel and start the session again." "Открыл ссылку в браузере. Подтвердите Funnel и запустите сессию ещё раз.") -ForegroundColor Green
    } catch {
        Write-Host (L "Could not open the browser automatically. Open the URL manually." "Не удалось открыть браузер автоматически. Откройте ссылку вручную.") -ForegroundColor Yellow
    }
}

function Wait-TunnelUrl($tunnelLog, $tunnelErrLog, $provider) {
    $pattern = Get-TunnelUrlPattern $provider
    $provider = Normalize-TunnelProvider $provider
    $maxWait = if ($provider -eq "tailscale") { 35 } else { 80 }
    for ($i=0; $i -lt $maxWait; $i++) {
        Start-Sleep -Seconds 1
        $log = Get-LogText @($tunnelLog, $tunnelErrLog)
        if ($log) {
            $m = [regex]::Match($log, $pattern)
            if ($m.Success) { return $m.Value }
            if ($provider -eq "tailscale" -and (Get-TailscaleFunnelEnableUrlFromText $log)) {
                return $null
            }
            if ($provider -eq "tailscale" -and $log -match "Funnel is not enabled") {
                return $null
            }
        }
    }
    return $null
}

function Test-TailscaleReady($tailscaleExe) {
    try {
        $status = & $tailscaleExe status --json 2>$null
        if ($LASTEXITCODE -ne 0 -or !$status) { return $false }
        $value = ($status -join [Environment]::NewLine) | ConvertFrom-Json
        if ($value.BackendState -and $value.BackendState -ne "Running") { return $false }
        return $true
    } catch {
        return $false
    }
}

function Invoke-TailscaleUp {
    $ts = Find-Tailscale
    Write-Host ""
    Write-Host (L "Starting Tailscale login/up in this window..." "Запускаю Tailscale login/up в текущем окне...") -ForegroundColor Cyan
    Write-Host (L "If Tailscale opens a browser, sign in and return here." "Если Tailscale откроет браузер, войдите в аккаунт и вернитесь сюда.") -ForegroundColor Yellow
    & $ts up
    $ok = ($LASTEXITCODE -eq 0)
    if ($ok -and (Test-TailscaleReady $ts)) {
        Write-Host (L "Tailscale connected." "Tailscale подключён.") -ForegroundColor Green
        return $true
    }
    Write-Host (L "Tailscale is not ready yet. Check the login window or run the check again." "Tailscale пока не готов. Проверьте окно логина или выполните проверку ещё раз.") -ForegroundColor Yellow
    return $false
}

function Get-ProviderLogStem($provider) {
    $provider = Normalize-TunnelProvider $provider
    if ($provider -eq "tailscale") { return "tailscale" }
    return "cloudflared"
}

function Start-Tunnel($provider, $localPort, $tunnelOutLog, $tunnelErrLog) {
    $provider = Normalize-TunnelProvider $provider
    if ($provider -eq "tailscale") {
        $ts = Find-Tailscale
        if (!(Test-TailscaleReady $ts)) {
            throw (L "Tailscale is not ready. Open Tailscale, sign in, and verify that `tailscale status` works." "Tailscale не готов. Откройте Tailscale, войдите в аккаунт и убедитесь, что `tailscale status` работает.")
        }

        return Start-Process -FilePath $ts `
            -ArgumentList @("funnel", "--yes", "http://127.0.0.1:$localPort") `
            -RedirectStandardOutput $tunnelOutLog `
            -RedirectStandardError $tunnelErrLog `
            -WindowStyle Hidden `
            -PassThru
    }

    $cf = Find-Cloudflared
    return Start-Process -FilePath $cf `
        -ArgumentList @("tunnel", "--url", "http://localhost:$localPort") `
        -RedirectStandardOutput $tunnelOutLog `
        -RedirectStandardError $tunnelErrLog `
        -WindowStyle Hidden `
        -PassThru
}

function Normalize-ProviderIdPart($value) {
    $part = ([string]$value).ToLowerInvariant() -replace "[^a-z0-9-]", "-"
    $part = $part -replace "-+", "-"
    return $part.Trim("-")
}

function Get-ProviderIdFromUrl($tunnelUrl, $provider, $sessionId = "") {
    $provider = Normalize-TunnelProvider $provider
    $tunnelHost = ([uri]$tunnelUrl).Host
    if ($provider -eq "cloudflare") {
        $tunnelHost = $tunnelHost -replace "\.trycloudflare\.com$", ""
    }
    $providerSuffix = Normalize-ProviderIdPart $tunnelHost
    if ($provider -eq "tailscale" -and $sessionId) {
        $sessionSuffix = Normalize-ProviderIdPart $sessionId
        if ($sessionSuffix) {
            $providerSuffix = "$providerSuffix-$sessionSuffix"
        }
    }
    return "repo-tools-$providerSuffix"
}

function New-Branch($repo, $prefix) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $branch = "promptql/$prefix-$stamp"
    & git -C $repo switch -c $branch
    if ($LASTEXITCODE -ne 0) { throw ((L "Could not create branch " "Не удалось создать ветку ") + $branch) }
    return $branch
}

function Test-ProcessAlive($processId) {
    if (!$processId) { return $false }
    try {
        $p = Get-Process -Id ([int]$processId) -ErrorAction Stop
        return ($null -ne $p)
    } catch {
        return $false
    }
}

function Stop-Pid($processId) {
    if (!$processId) { return }
    Stop-Process -Id ([int]$processId) -Force -ErrorAction SilentlyContinue
}

function Stop-Old {
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*karox_supervisor.py*" -or
        $_.CommandLine -like "*repo_tools:app*" -or
        $_.CommandLine -like "*cloudflared*tunnel*--url*localhost*" -or
        $_.CommandLine -like "*RepoPilotBridge*sessions*run-server.ps1*" -or
        $_.CommandLine -like "*RepoPilotBridge*sessions*run-tunnel.ps1*"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}


function Show-KaroXIntro {
    $interactive = $true
    try { $interactive = -not [Console]::IsOutputRedirected } catch {}
    if ($env:KAROX_NO_ANIMATION -eq "1") { $interactive = $false }
    Clear-Host
    if ($interactive) {
        $frames = @("  ★","       ★","            ★","                 ★","                      ★","                           ★","                                ★","                                     ★","                                          ★")
        foreach ($frame in $frames) {
            Write-Host ("`r" + $frame.PadRight(62)) -NoNewline -ForegroundColor Yellow
            Start-Sleep -Milliseconds 55
        }
        Write-Host ""
        Write-Host "                                          │" -ForegroundColor DarkYellow
        Start-Sleep -Milliseconds 80
        Write-Host "                                          ▼" -ForegroundColor Yellow
        Start-Sleep -Milliseconds 120
    }
    Write-Host ""
    Write-Host "                         ★" -ForegroundColor Yellow
    Write-Host "                 STAR FOR KAROX" -ForegroundColor Magenta
    if ($KaroXVersion) { Write-Host ("                     v" + $KaroXVersion) -ForegroundColor DarkMagenta }
    Write-Host ("              " + (L "local code, guided safely" "локальный код под безопасным управлением")) -ForegroundColor DarkGray
    Write-Host ""
    if ($interactive) { Start-Sleep -Milliseconds 450 }
}

function Get-UIWidth {
    $width = 72
    try { $width = [int]$Host.UI.RawUI.WindowSize.Width } catch {}
    if ($width -lt 52) { return 52 }
    if ($width -gt 88) { return 88 }
    return $width
}

function UI-Fit($text, $max) {
    $value = [string]$text
    if ($max -lt 4) { return "" }
    if ($value.Length -le $max) { return $value }
    return $value.Substring(0, $max - 1) + "…"
}

function UI-Wrap($text, $color = "White", $indent = 2) {
    $max = [Math]::Max(20, (Get-UIWidth) - $indent - 2)
    $words = ([string]$text) -split "\s+"
    $line = ""
    foreach ($word in $words) {
        if (($line.Length + $word.Length + 1) -gt $max -and $line) {
            Write-Host ((" " * $indent) + $line) -ForegroundColor $color
            $line = $word
        } else { $line = if ($line) { $line + " " + $word } else { $word } }
    }
    if ($line) { Write-Host ((" " * $indent) + $line) -ForegroundColor $color }
}

function UI-Badge($text, $color = "White") {
    Write-Host (" " + $text + " ") -NoNewline -ForegroundColor Black -BackgroundColor $color
}

function UI-Choice($key, $title, $detail, $color = "White") {
    Write-Host ("  │  [" + $key + "] ") -NoNewline -ForegroundColor $color
    Write-Host $title -NoNewline -ForegroundColor $color
    Write-Host ("  " + (UI-Fit $detail ((Get-UIWidth) - $title.Length - $key.Length - 12))) -ForegroundColor DarkGray
}

function UI-Notice($kind, $title, $detail = "") {
    $symbol = "•"; $color = "Cyan"
    if ($kind -eq "success") { $symbol = "✓"; $color = "Green" }
    elseif ($kind -eq "warn") { $symbol = "!"; $color = "Yellow" }
    elseif ($kind -eq "error") { $symbol = "×"; $color = "Red" }
    elseif ($kind -eq "progress") { $symbol = "◌"; $color = "Magenta" }
    Write-Host ("  " + $symbol + " ") -NoNewline -ForegroundColor $color
    Write-Host $title -ForegroundColor $color
    if ($detail) { UI-Wrap $detail "DarkGray" 4 }
}

function UI-EmptyState($title, $detail) {
    Write-Host "  ╭─ ◇ " -NoNewline -ForegroundColor DarkMagenta
    Write-Host $title -ForegroundColor White
    UI-Wrap $detail "DarkGray"  5
    Write-Host "  ╰────────────────" -ForegroundColor DarkGray
}

function UI-StatusBanner($status, $name) {
    $label = UI-StatusLabel $status; $color = UI-StatusColor $status
    Write-Host "  " -NoNewline
    UI-Badge $label $color
    Write-Host ("  " + $name) -ForegroundColor White
}

function UI-WorkspaceCard($number, $session) {
    $width = Get-UIWidth
    $name = UI-Fit (Get-RepoLabel $session.repo) 24
    $title = UI-Fit ([string]$session.title) 20
    Write-Host ("  ╭─ [" + $number + "] ") -NoNewline -ForegroundColor Cyan
    Write-Host $name -NoNewline -ForegroundColor White
    Write-Host "  " -NoNewline
    Write-Host (UI-StatusLabel $session.status) -NoNewline -ForegroundColor (UI-StatusColor $session.status)
    if ($title -and $title -ne "-") { Write-Host ("  " + $title) -ForegroundColor DarkGray } else { Write-Host "" }
    Write-Host ("  │  " + (UI-Fit ([string]$session.branch) ($width - 8))) -ForegroundColor Cyan
    Write-Host ("  ╰─ " + $session.mode + "  ·  " + (Get-ProviderLabel $session.tunnelProvider) + "  ·  " + (Get-AiClientLabel $session.aiClient)) -ForegroundColor DarkGray
}

function Get-MissionActionText($action) {
    switch ($action) {
        "stop_and_report_branch_mismatch" { return L "Stop: branch mismatch detected" "Стоп: обнаружено несовпадение ветки" }
        "wait_for_or_start_real_task" { return L "Send or start the real task" "Отправьте или запустите реальное ТЗ" }
        "inspect_existing_changes" { return L "Review the existing diff before editing" "Проверьте существующий diff до изменений" }
        "inspect_project_context_then_execute_task" { return L "Inspect project context, then execute the task" "Изучите контекст проекта, затем выполняйте задачу" }
        default { return [string]$action }
    }
}

function UI-Line($char = "─", $width = 0, $color = "DarkGray") {
    if ($width -le 0) { $width = (Get-UIWidth) - 4 }
    if ($width -lt 8) { $width = 8 }
    Write-Host (($char * $width)) -ForegroundColor $color
}

function UI-Section($title) {
    $width = Get-UIWidth
    $label = " " + ([string]$title).ToUpperInvariant() + " "
    $fill = [Math]::Max(2, $width - $label.Length - 5)
    Write-Host ""
    Write-Host "  ┌─" -NoNewline -ForegroundColor DarkMagenta
    Write-Host $label -NoNewline -ForegroundColor Magenta
    Write-Host ("─" * $fill) -ForegroundColor DarkGray
}

function UI-KeyValue($key, $value, $color = "White") {
    $label = [string]$key
    $text = [string]$value
    $width = Get-UIWidth
    if (($label.Length + $text.Length + 21) -le $width) {
        Write-Host ("  │  {0,-16}" -f $label) -NoNewline -ForegroundColor DarkGray
        Write-Host $text -ForegroundColor $color
    } else {
        Write-Host ("  │  " + $label) -ForegroundColor DarkGray
        Write-Host ("  │    " + (UI-Fit $text ($width - 8))) -ForegroundColor $color
    }
}

function UI-StatusLabel($status) {
    switch ([string]$status) {
        "running" { return "● LIVE" }
        "partial" { return "◐ DEGRADED" }
        default   { return "○ OFFLINE" }
    }
}

function UI-StatusColor($status) {
    switch ([string]$status) {
        "running" { return "Green" }
        "partial" { return "Yellow" }
        default   { return "DarkGray" }
    }
}

function Header($subtitle = "") {
    Clear-Host
    $width = Get-UIWidth
    Write-Host ""
    Write-Host "  ◆ " -NoNewline -ForegroundColor Magenta
    Write-Host "KAROX" -NoNewline -ForegroundColor White
    if ($KaroXVersion) { Write-Host (" v" + $KaroXVersion) -NoNewline -ForegroundColor DarkMagenta }
    Write-Host "  /  PROJECT FLIGHT DECK" -ForegroundColor DarkGray
    Write-Host ("  " + ("━" * ($width - 2))) -ForegroundColor DarkMagenta
    if ($subtitle) {
        Write-Host ("  " + (UI-Fit ([string]$subtitle) ($width - 2))) -ForegroundColor White
        Write-Host ("  " + (L "local code · explicit control · safe AI handoff" "локальный код · явный контроль · безопасная передача AI")) -ForegroundColor DarkGray
    }
    Write-Host ""
}

function Get-RepoLabel($repo) {
    try { return Split-Path -Leaf $repo } catch { return $repo }
}

function Normalize-ExistingPath($path) {
    try { return (Resolve-Path -LiteralPath $path).Path.ToLowerInvariant() } catch { return ([string]$path).ToLowerInvariant() }
}

function Get-RunningSessionsForRepo($repo) {
    $target = Normalize-ExistingPath $repo
    return @(Get-Sessions | Where-Object {
        $_.status -eq "running" -and (Normalize-ExistingPath $_.repo) -eq $target
    })
}

function Save-SessionJson($sessionDir, $session) {
    $session | ConvertTo-Json -Depth 20 | Set-Content -Path (Join-Path $sessionDir "session.json") -Encoding UTF8
}

function Load-SessionJson($sessionDir) {
    $path = Join-Path $sessionDir "session.json"
    if (!(Test-Path -LiteralPath $path)) { return $null }
    try { return Get-Content -Raw -LiteralPath $path | ConvertFrom-Json } catch { return $null }
}

function Write-SessionFiles($sessionDir, $connectPrompt, $taskPrompt, $apiKey, $providerId, $sessionInfo) {
    $connectPrompt | Set-Content -Path (Join-Path $sessionDir "connect-prompt.txt") -Encoding UTF8
    $taskPrompt | Set-Content -Path (Join-Path $sessionDir "task-prompt.txt") -Encoding UTF8
    $apiKey | Set-Content -Path (Join-Path $sessionDir "api-key.txt") -Encoding UTF8
    $providerId | Set-Content -Path (Join-Path $sessionDir "provider-id.txt") -Encoding UTF8
    $sessionInfo | Set-Content -Path (Join-Path $sessionDir "session-info.txt") -Encoding UTF8

@"
ПОДСКАЗКА ПОДКЛЮЧЕНИЯ:
$connectPrompt

ШАБЛОН ЗАДАЧИ:
$taskPrompt

X-API-KEY:
$apiKey

PROVIDER ID:
$providerId
"@ | Set-Content -Path (Join-Path $sessionDir "all.txt") -Encoding UTF8
}

function Get-Sessions {
    $items = @()
    if (!(Test-Path -LiteralPath $SessionsDir)) { return @() }
    foreach ($dir in Get-ChildItem -LiteralPath $SessionsDir -Directory -ErrorAction SilentlyContinue) {
        $session = Load-SessionJson $dir.FullName
        if (!$session) { continue }

        $serverAlive = Test-ProcessAlive $session.serverPid
        $tunnelAlive = Test-ProcessAlive $session.tunnelPid
        $status = if ($serverAlive -and $tunnelAlive) { "running" } elseif ($serverAlive -or $tunnelAlive) { "partial" } else { "stopped" }
        $sessionDir = [string]$session.sessionDir
        if (!$sessionDir) { $sessionDir = $dir.FullName }

        $items += [pscustomobject]@{
            id = [string]$session.id
            repo = [string]$session.repo
            mode = [string]$session.mode
            branch = [string]$session.branch
            title = [string]$session.title
            startedAt = [string]$session.startedAt
            localPort = [int]$session.localPort
            tunnelProvider = (Normalize-TunnelProvider $session.tunnelProvider)
            tunnelUrl = [string]$session.tunnelUrl
            providerId = [string]$session.providerId
            apiKey = [string]$session.apiKey
            commitAllowed = [bool]$session.commitAllowed
            sessionDir = $sessionDir
            serverPid = $session.serverPid
            tunnelPid = $session.tunnelPid
            serverAlive = $serverAlive
            tunnelAlive = $tunnelAlive
            status = $status
            serverOutLog = [string]$session.serverOutLog
            serverErrLog = [string]$session.serverErrLog
            tunnelOutLog = [string]$session.tunnelOutLog
            tunnelErrLog = [string]$session.tunnelErrLog
        }
    }
    return @($items | Sort-Object startedAt -Descending)
}


function Select-Mode {
    Header (L "Create workspace session" "Создание рабочей сессии")
    UI-Notice "info" (L "Choose the smallest access profile that can finish the task." "Выберите минимальный профиль, достаточный для задачи.") (L "You can open a new session later with more access." "Позже можно открыть новую сессию с большим доступом.")
    UI-Section (L "Access profiles" "Профили доступа")
    UI-Choice "1" "OBSERVE" (L "Read-only exploration and repository context" "Анализ и контекст репозитория без изменений") "Cyan"
    UI-Choice "2" "BUILD" (L "Isolated branch, edits, checks and safe commit" "Отдельная ветка, правки, проверки и безопасный commit") "Magenta"
    UI-Choice "3" "RESUME" (L "Continue the current workspace branch" "Продолжить текущую рабочую ветку") "Green"
    UI-Choice "4" "ADVANCED" (L "Extended commands inside this repository" "Расширенные команды внутри репозитория") "Yellow"
    Write-Host ""
    $choice = ([string](Read-Host ("  › " + (L "Profile" "Профиль")))).Trim()
    if ($choice -notmatch "^[1-4]$") { throw (L "Choose a profile from 1 to 4." "Выберите профиль от 1 до 4.") }
    return $choice
}

function Select-AiClient {
    Header (L "Connection target" "Куда подключить")
    UI-Notice "info" (L "Choose where this local workspace should appear." "Выберите AI-клиент для рабочего пространства.") ""
    UI-Section (L "AI targets" "AI-клиенты")
    UI-Choice "1" "PROMPTQL" (L "Native shared AI workspace · recommended" "Нативная командная AI-среда · рекомендуется") "Magenta"
    UI-Choice "2" (L "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ") (L "Generic OpenAPI connection" "Универсальное OpenAPI-подключение") "Cyan"
    UI-Choice "3" "LETAIDO.COM" (L "Compatibility mode" "Режим совместимости") "DarkGray"
    Write-Host ""
    $choice = ([string](Read-Host ("  › " + (L "Target" "Клиент")))).Trim()
    if ($choice -eq "1") { return "promptql" }
    if ($choice -eq "2") { return "other" }
    if ($choice -eq "3") { return "letaido" }
    throw (L "Choose an option from 1 to 3." "Выберите вариант от 1 до 3.")
}

function Ensure-AiClient {
    # Спрашиваем только если файла настроек нет или в нём нет поля aiClient.
    if (!(Test-Path $SettingsFile)) {
        $picked = Select-AiClient
        $settings = [pscustomobject]@{ tunnelProvider = "cloudflare"; aiClient = $picked }
        Save-Settings $settings
        return
    }
    try {
        $settings = Get-Content -Raw -LiteralPath $SettingsFile | ConvertFrom-Json
        if (!$settings -or ($settings.PSObject.Properties.Name -notcontains "aiClient")) {
            $picked = Select-AiClient
            if (!$settings) { $settings = [pscustomobject]@{ tunnelProvider = "cloudflare" } }
            if ($settings.PSObject.Properties.Name -notcontains "aiClient") {
                $settings | Add-Member -NotePropertyName "aiClient" -NotePropertyValue $picked -Force
            } else {
                $settings.aiClient = $picked
            }
            Save-Settings $settings
        }
    } catch {
        $picked = Select-AiClient
        $settings = [pscustomobject]@{ tunnelProvider = "cloudflare"; aiClient = $picked }
        Save-Settings $settings
    }
}


function Select-Repo {
    $repos = @(Load-Repos)
    Header (L "Choose repository" "Выберите репозиторий")
    UI-Notice "info" (L "Choose a recent repository or paste a full Git path." "Выберите недавний репозиторий или вставьте полный Git-путь.") (L "KaroX validates the repository before creating a session." "KaroX проверит репозиторий до создания сессии.")
    UI-Section (L "Recent repositories" "Недавние репозитории")
    if ($repos.Count -eq 0) {
        UI-EmptyState (L "No pinned repositories" "Нет закреплённых репозиториев") (L "Paste a full path below to add the first one." "Вставьте полный путь ниже, чтобы добавить первый.")
    } else {
        for ($i=0; $i -lt $repos.Count; $i++) {
            $label = Get-RepoLabel $repos[$i]
            UI-Choice ([string]($i + 1)) $label (UI-Fit ([string]$repos[$i]) 42) "Cyan"
        }
    }
    UI-Section (L "Connect" "Подключить")
    UI-Choice "N" (L "NEW PATH" "НОВЫЙ ПУТЬ") (L "Enter a full path to a local Git repository" "Введите полный путь к локальному Git-репозиторию") "Green"
    Write-Host ("  │  " + (L "Tip: you may paste the full path directly at the prompt." "Совет: полный путь можно вставить прямо в строку выбора.")) -ForegroundColor DarkGray
    Write-Host ""
    $repoChoice = ([string](Read-Host ("  › " + (L "Repository" "Репозиторий")))).Trim()
    $typedPath = $repoChoice.Trim([char]34)
    if ($repoChoice -match "^[Nn]$") {
        $repo = ([string](Read-Host ("  › " + (L "Full path" "Полный путь")))).Trim().Trim([char]34)
        $repo = Ensure-GitRepo $repo; Save-Repo $repo; return $repo
    }
    if ($repoChoice -match "^\d+$") {
        $idx = [int]$repoChoice - 1
        if ($idx -lt 0 -or $idx -ge $repos.Count) { throw (L "Repository number not found." "Репозиторий с таким номером не найден.") }
        $repo = [string]$repos[$idx]
        if (!(Test-Path -LiteralPath $repo)) { throw ((L "Path no longer exists: " "Путь больше не существует: ") + $repo) }
        Save-Repo $repo; return $repo
    }
    if ($typedPath -and (Test-Path -LiteralPath $typedPath)) {
        $repo = Ensure-GitRepo $typedPath; Save-Repo $repo; return $repo
    }
    throw (L "Enter a number, N, or a full repository path." "Введите номер, N или полный путь к репозиторию.")
}

function Test-TunnelProviderStatus($provider) {
    $provider = Normalize-TunnelProvider $provider
    if ($provider -eq "tailscale") {
        try {
            $ts = Find-Tailscale
            if (Test-TailscaleReady $ts) {
                return [pscustomobject]@{ ok = $true; text = (L "Tailscale was found and responds to status." "Tailscale найден и отвечает на status.") }
            }
            return [pscustomobject]@{ ok = $false; text = (L "Tailscale was found but is not ready. Sign in and check tailscale status." "Tailscale найден, но не готов. Войдите в аккаунт и проверьте tailscale status.") }
        } catch {
            return [pscustomobject]@{ ok = $false; text = $_.Exception.Message }
        }
    }

    try {
        $cf = Find-Cloudflared
        return [pscustomobject]@{ ok = $true; text = ((L "cloudflared found: " "cloudflared найден: ") + $cf) }
    } catch {
        return [pscustomobject]@{ ok = $false; text = $_.Exception.Message }
    }
}

function Install-TailscaleFromSettings {
    try {
        $existing = Find-Tailscale
        Write-Host "Tailscale уже установлен: $existing" -ForegroundColor Green
        return $true
    } catch {}

    Write-Host ""
    Write-Host "RepoPilot может установить Tailscale через winget." -ForegroundColor Cyan
    Write-Host "После установки Tailscale всё равно нужно войти в аккаунт и включить Funnel в tailnet." -ForegroundColor Yellow
    if (!(Ask-Yes "Установить Tailscale сейчас?" $true)) {
        return $false
    }

    $installed = Install-WingetPackage "Tailscale.Tailscale" "Tailscale"
    if (!$installed) {
        Write-Host "winget не смог установить Tailscale. Попробуйте ещё раз из настроек или установите вручную." -ForegroundColor Red
        return $false
    }

    try {
        $ts = Find-Tailscale
        Write-Host "Tailscale установлен: $ts" -ForegroundColor Green
        Write-Host "Откройте Tailscale, войдите в аккаунт, затем вернитесь в RepoPilot и нажмите R для проверки." -ForegroundColor Yellow
        return $true
    } catch {
        Write-Host "Установка завершилась, но tailscale.exe пока не найден в PATH. Перезапустите PowerShell или нажмите R позже." -ForegroundColor Yellow
        return $false
    }
}


function Show-Settings {
    while ($true) {
        $settings = Load-Settings
        $provider = Normalize-TunnelProvider $settings.tunnelProvider
        $aiClient = Normalize-AiClient $settings.aiClient
        $status = Test-TunnelProviderStatus $provider
        Header (L "Workspace settings" "Настройки рабочего пространства")
        UI-Section (L "Current configuration" "Текущая конфигурация")
        UI-KeyValue (L "Language" "Язык") $(if ($settings.language -eq "ru") { "Русский" } else { "English" }) "White"
        UI-KeyValue (L "AI target" "AI-клиент") (Get-AiClientLabel $aiClient) "Magenta"
        UI-KeyValue (L "Secure tunnel" "Безопасный туннель") (Get-ProviderLabel $provider) "Cyan"
        UI-KeyValue (L "Connection" "Подключение") $(if ($status.ok) { L "READY" "ГОТОВО" } else { L "NEEDS ATTENTION" "ТРЕБУЕТ ВНИМАНИЯ" }) $(if ($status.ok) { "Green" } else { "Yellow" })
        if (-not $status.ok) { UI-Notice "warn" (L "Tunnel provider needs attention" "Туннель требует внимания") $status.text }
        UI-Section (L "Preferences" "Параметры")
        UI-Choice "1" "CLOUDFLARE" (L "Quick ephemeral public tunnel" "Быстрый временный публичный туннель") "Cyan"
        UI-Choice "2" "TAILSCALE" (L "Private identity-aware funnel" "Приватный туннель с проверкой личности") "Magenta"
        UI-Choice "A" (L "AI TARGET" "AI-КЛИЕНТ") (L "Change the connection destination" "Изменить место подключения") "White"
        UI-Choice "L" (L "LANGUAGE" "ЯЗЫК") (L "Switch English / Русский" "Переключить English / Русский") "White"
        if ($provider -eq "tailscale") { UI-Choice "T" (L "TAILSCALE LOGIN" "ВХОД TAILSCALE") (L "Login or start the CLI" "Войти или запустить CLI") "Yellow" }
        if ($provider -eq "tailscale" -and !$status.ok) { UI-Choice "I" (L "INSTALL" "УСТАНОВИТЬ") "Tailscale" "Yellow" }
        Write-Host ("  │  [R] " + (L "Refresh health" "Обновить состояние") + "   [B] " + (L "Back" "Назад")) -ForegroundColor DarkGray
        Write-Host ""
        $x = ([string](Read-Host ("  › " + (L "Action" "Действие")))).Trim()
        if ($x -eq "1") { $settings.tunnelProvider = "cloudflare"; Save-Settings $settings }
        elseif ($x -eq "2") { $settings.tunnelProvider = "tailscale"; Save-Settings $settings; try { Find-Tailscale | Out-Null } catch { Install-TailscaleFromSettings | Out-Null } }
        elseif ($x -match "^[Aa]$") { $settings.aiClient = Select-AiClient; Save-Settings $settings }
        elseif ($x -match "^[Ll]$") { $settings.language = Select-Language; Save-Settings $settings }
        elseif ($x -match "^[Ii]$" -and $provider -eq "tailscale" -and !$status.ok) { Install-TailscaleFromSettings | Out-Null; Read-Host (L "  Enter to continue" "  Enter — продолжить") | Out-Null }
        elseif ($x -match "^[Tt]$" -and $provider -eq "tailscale") { try { Invoke-TailscaleUp | Out-Null } catch { UI-Notice "error" (L "Tailscale failed" "Ошибка Tailscale") $_.Exception.Message }; Read-Host (L "  Enter to continue" "  Enter — продолжить") | Out-Null }
        elseif ($x -match "^[Rr]$") { continue }
        elseif ($x -match "^[Bb]$") { return }
    }
}

function Build-Prompts($providerId, $tunnelUrl, $mode, $branch, $sessionTitle, $commitAllowed, $tunnelProvider, $aiClient) {
    $providerLabel = Get-ProviderLabel $tunnelProvider
    $aiClient = Normalize-AiClient $aiClient
    $language = Get-SelectedLanguage

    if ($language -eq "ru") {
        $sessionBlock = @"
Сессия:
- repoRoot: будет подтверждён через /session
- режим: $mode
- ветка: $branch
- название сессии: $sessionTitle
- provider id: $providerId
- commitAllowed: $commitAllowed
- pushAllowed: false

Название сессии — только метка истории, не задача. После подключения дождись отдельного реального ТЗ пользователя.
"@
        $preflight = @"
Перед любой работой выполни preflight:
1. GET /session
2. GET /health
3. GET /git/status
4. Сверь repoRoot, branch, mode, commitAllowed и pushAllowed с этой карточкой.
Если что-либо не совпадает — остановись и сообщи пользователю.
"@
        $rules = @"
Правила:
- большой вывод сохраняй через capture=file;
- не создавай helper-скрипты для commit/push/check;
- перед commit вызывай /git/cleanup-generated;
- commit выполняй только через /git/commit;
- никогда не выполняй git push;
- ключ X-API-Key не проси отправлять в чат.
"@
        if ($aiClient -eq "promptql") {
            $intro = @"
Я запустил Star For KaroX и хочу подключить эту локальную сессию к PromptQL.

Создай отдельную personal custom API integration:
- provider id: $providerId
- protocol: api
- name: KaroX
- base_url: $tunnelUrl
- api_docs_url: $tunnelUrl/openapi.json
- credential type: api_key
- credential header: X-API-Key
- prefix: пустой
- туннель: $providerLabel

Не переиспользуй интеграцию другой активной сессии. Покажи защищённую карточку подключения; в неё пользователь вставит ключ, скопированный клавишей K.
"@
        } elseif ($aiClient -eq "letaido") {
            $intro = @"
Я запустил Star For KaroX и хочу подключить локальную сессию к letaido.com.
Разреши домен $tunnelUrl и запроси защищённый header secret X-API-Key для этого домена. Затем обращайся напрямую к $tunnelUrl.
"@
        } else {
            $intro = @"
Я запустил Star For KaroX и хочу подключить локальную сессию к AI-клиенту.
- base_url: $tunnelUrl
- OpenAPI: $tunnelUrl/openapi.json
- auth: header X-API-Key
- туннель: $providerLabel
Ключ пользователь вводит только через защищённое хранилище клиента.
"@
        }
        $connectPrompt = "$intro`n`n$sessionBlock`n$preflight`n$rules"
        $taskPrompt = @"
Реальное ТЗ для KaroX:

<ВСТАВЬТЕ СЮДА ЗАДАЧУ>

Контекст:
- ветка: $branch
- режим: $mode
- commitAllowed: $commitAllowed
- pushAllowed: false

Сначала выполни /session, /health и /git/status и проверь совпадение сессии.
Работай автономно только по реальному ТЗ. Перед commit вызови /git/cleanup-generated, commit делай только через /git/commit. Никогда не делай push.
В конце покажи проверки, commit hash (если создан), git status и краткий отчёт.
"@
    } else {
        $sessionBlock = @"
Session:
- repoRoot: must be confirmed via /session
- mode: $mode
- branch: $branch
- session label: $sessionTitle
- provider id: $providerId
- commitAllowed: $commitAllowed
- pushAllowed: false

The session label is history metadata, not a task. After connecting, wait for a separate real user instruction.
"@
        $preflight = @"
Before any repository work, run this preflight:
1. GET /session
2. GET /health
3. GET /git/status
4. Match repoRoot, branch, mode, commitAllowed, and pushAllowed against this card.
Stop and report the mismatch if any value differs.
"@
        $rules = @"
Rules:
- use capture=file for large output;
- do not create helper scripts for commit/push/check orchestration;
- call /git/cleanup-generated before committing;
- commit only through /git/commit;
- never run git push;
- never ask the user to paste X-API-Key into chat.
"@
        if ($aiClient -eq "promptql") {
            $intro = @"
I started Star For KaroX and want to connect this local session to PromptQL.

Create a separate personal custom API integration:
- provider id: $providerId
- protocol: api
- name: KaroX
- base_url: $tunnelUrl
- api_docs_url: $tunnelUrl/openapi.json
- credential type: api_key
- credential header: X-API-Key
- prefix: empty
- tunnel: $providerLabel

Do not reuse another active session's integration. Show a protected connection card where the user can paste the key copied with K.
"@
        } elseif ($aiClient -eq "letaido") {
            $intro = @"
I started Star For KaroX and want to connect this local session to letaido.com.
Allow the domain $tunnelUrl and request a protected X-API-Key header secret for it. Then call $tunnelUrl directly.
"@
        } else {
            $intro = @"
I started Star For KaroX and want to connect this local session to an AI client.
- base_url: $tunnelUrl
- OpenAPI: $tunnelUrl/openapi.json
- auth: X-API-Key header
- tunnel: $providerLabel
The user must enter the key only through the client's protected credential store.
"@
        }
        $connectPrompt = "$intro`n`n$sessionBlock`n$preflight`n$rules"
        $taskPrompt = @"
Real task for KaroX:

<INSERT THE USER'S TASK HERE>

Context:
- branch: $branch
- mode: $mode
- commitAllowed: $commitAllowed
- pushAllowed: false

First call /session, /health, and /git/status and verify the exact session.
Work autonomously only on the real task. Before committing call /git/cleanup-generated; commit only through /git/commit. Never push.
At the end report checks, commit hash (if created), git status, and a concise task summary.
"@
    }

    return [pscustomobject]@{ connect = $connectPrompt; task = $taskPrompt }
}

function Start-NewSession {
    $modeChoice = Select-Mode
    $repo = Select-Repo

    $sessionTitle = Read-Host (L "Session name (history label, not an AI task)" "Название сессии (метка истории, не ТЗ для AI)")
    if (!$sessionTitle) { $sessionTitle = (L "KaroX session" "Сессия KaroX") }

    $mode = "read_only"
    $commitAllowed = "false"
    $branch = Get-Branch $repo
    $branchPrefix = ""

    switch ($modeChoice) {
        "1" { $mode = "read_only"; $commitAllowed = "false" }
        "2" { $mode = "autopilot"; $commitAllowed = "true"; $branchPrefix = "autopilot" }
        "3" {
            $mode = "autopilot"; $commitAllowed = "true"
            if (-not $branch.StartsWith("promptql/")) {
                Write-Host ((L "Current branch is not promptql/*: " "Текущая ветка не promptql/*: ") + $branch) -ForegroundColor Yellow
                if (!(Ask-Yes (L "Continue on this branch?" "Продолжить на этой ветке?") $false)) { return $null }
            }
        }
        "4" { $mode = "full"; $commitAllowed = "true"; $branchPrefix = "full" }
    }

    $sameRepoSessions = @(Get-RunningSessionsForRepo $repo)
    if ($sameRepoSessions.Count -gt 0 -and $mode -ne "read_only") {
        Write-Host ""
        Write-Host (L "This project path is already open in another active session:" "Этот путь проекта уже открыт в другой активной сессии:") -ForegroundColor Yellow
        foreach ($s in $sameRepoSessions) {
            Write-Host " - $($s.id) | $($s.mode) | $($s.branch) | $($s.tunnelUrl)" -ForegroundColor Yellow
        }
        Write-Host (L "Parallel tasks in one project require a separate clone/worktree; otherwise sessions modify the same branch and files." "Для двух параллельных задач в одном проекте нужен отдельный clone/worktree. Иначе сессии будут менять одну и ту же ветку и файлы.") -ForegroundColor Yellow
        if (!(Ask-Yes (L "Continue on the same path anyway?" "Всё равно продолжить на этом же пути?") $false)) { return $null }
    }

    if ($branchPrefix) {
        $branch = New-Branch $repo $branchPrefix
    }

    $apiKey = (([guid]::NewGuid().ToString("N")) + ([guid]::NewGuid().ToString("N")))
    $tunnelProvider = Get-SelectedTunnelProvider
    $aiClient = Get-SelectedAiClient
    $tunnelProviderLabel = Get-ProviderLabel $tunnelProvider
    $tunnelLogStem = Get-ProviderLogStem $tunnelProvider
    $sessionId = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + ([guid]::NewGuid().ToString("N").Substring(0, 6))
    $sessionDir = Join-Path $SessionsDir $sessionId
    $sessionLogsDir = Join-Path $sessionDir "logs"
    $runsDir = Join-Path $sessionDir "runs"
    $serverOutLog = Join-Path $sessionLogsDir "uvicorn.out.log"
    $serverErrLog = Join-Path $sessionLogsDir "uvicorn.err.log"
    $serverLog = Join-Path $sessionLogsDir "repo-tools.jsonl"
    $tunnelOutLog = Join-Path $sessionLogsDir "$tunnelLogStem.out.log"
    $tunnelErrLog = Join-Path $sessionLogsDir "$tunnelLogStem.err.log"
    $localPort = Get-FreeLocalPort

    New-Item -ItemType Directory -Force -Path $sessionDir | Out-Null
    New-Item -ItemType Directory -Force -Path $sessionLogsDir | Out-Null
    New-Item -ItemType Directory -Force -Path $runsDir | Out-Null

    foreach ($p in @($serverOutLog, $serverErrLog, $serverLog, $tunnelOutLog, $tunnelErrLog)) {
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    }

    $env:REPO_ROOT = $repo
    $env:REPO_ROOT_B64 = ConvertTo-Base64Utf8 $repo
    $env:REPO_TOOLS_API_KEY = $apiKey
    $env:REPO_TOOLS_MODE = $mode
    $env:REPO_TOOLS_BRANCH = $branch
    $env:REPO_TOOLS_SESSION_TITLE = $sessionTitle
    $env:REPO_TOOLS_SESSION_TITLE_B64 = ConvertTo-Base64Utf8 $sessionTitle
    $env:REPO_TOOLS_INITIAL_TASK = ""
    $env:REPO_TOOLS_INITIAL_TASK_B64 = ConvertTo-Base64Utf8 ""
    $env:REPO_TOOLS_COMMIT_ALLOWED = $commitAllowed
    $env:REPO_TOOLS_HOME = $RuntimeDir
    $env:REPO_TOOLS_LOG_FILE = $serverLog
    $env:REPO_TOOLS_RUNS_DIR = $runsDir

    Header (L "Provisioning local workspace" "Запуск локального рабочего пространства")
    Write-Host ((L "Repository: " "Репозиторий: ") + $repo)
    Write-Host ((L "Mode      : " "Режим     : ") + $mode)
    Write-Host ((L "Tunnel    : " "Туннель   : ") + $tunnelProviderLabel)
    Write-Host ((L "Port      : " "Порт      : ") + $localPort)
    Write-Host ""

    Write-Host "  ◌ Starting secure local API..." -ForegroundColor Magenta
    $serverLogPaths = @($serverErrLog, $serverOutLog)
    $SupervisorScript = Join-Path $Root "scripts\karox_supervisor.py"
    $serverPidFile = Join-Path $sessionDir "server.pid"
    $watchdogEnabled = ($env:KAROX_NO_WATCHDOG -ne "1") -and (Test-Path -LiteralPath $SupervisorScript)
    if ($watchdogEnabled) {
        Write-Host ("  ◌ " + (L "Watchdog: auto-restart guard is on" "Watchdog: авто-рестарт при сбое включён")) -ForegroundColor DarkMagenta
    }
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = if ($previousPythonPath) { "$ServerDir$([System.IO.Path]::PathSeparator)$previousPythonPath" } else { $ServerDir }
        $uvicornArgs = @("-m", "uvicorn", "--app-dir", $ServerDir, "repo_tools:app", "--host", "127.0.0.1", "--port", "$localPort")
        $serverArgs = if ($watchdogEnabled) {
            @($SupervisorScript, "--port", "$localPort", "--pid-file", $serverPidFile, "--log", (Join-Path $sessionLogsDir "supervisor.jsonl"), "--", $PythonExe) + $uvicornArgs
        } else { $uvicornArgs }
        $serverProc = Start-Process -FilePath $PythonExe `
            -ArgumentList $serverArgs `
            -WorkingDirectory $ServerDir `
            -RedirectStandardOutput $serverOutLog `
            -RedirectStandardError $serverErrLog `
            -WindowStyle Hidden `
            -PassThru
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
    $serverPid = $serverProc.Id

    if (!(Wait-LocalApiProcess $apiKey $localPort $serverPid $null $serverLogPaths)) {
        Stop-Pid $serverPid
        $tail = Get-LogTailText $serverLogPaths
        if (!$tail) { $tail = L "Logs are empty." "Логи пустые." }
        throw ((L "Local API did not respond within 20 seconds." "Локальный API не ответил за 20 секунд.") + "`n" + $tail)
    }

    $isolation = Assert-SessionIsolation $apiKey $localPort $repo $branch $mode
    $tunnelStatus = Test-TunnelProviderStatus $tunnelProvider
    if (!$tunnelStatus.ok -and $tunnelProvider -eq "tailscale") {
        Write-Host ""
        Write-Host ($tunnelProviderLabel + (L " is not ready: " " не готов: ") + $tunnelStatus.text) -ForegroundColor Yellow
        if (Ask-Yes (L "Run tailscale up now?" "Запустить tailscale up сейчас?") $true) {
            Invoke-TailscaleUp | Out-Null
            $tunnelStatus = Test-TunnelProviderStatus $tunnelProvider
        }
    }
    if (!$tunnelStatus.ok -and $tunnelProvider -ne "tailscale") {
        Write-Host ""
        Write-Host ($tunnelProviderLabel + (L " is not ready: " " не готов: ") + $tunnelStatus.text) -ForegroundColor Yellow
        if (Ask-Yes (L "Download cloudflared automatically from github.com/cloudflare/cloudflared?" "Скачать cloudflared автоматически с github.com/cloudflare/cloudflared?") $true) {
            Install-Cloudflared | Out-Null
            $tunnelStatus = Test-TunnelProviderStatus $tunnelProvider
        }
    }
    if (!$tunnelStatus.ok) {
        Stop-Pid $serverPid
        throw ($tunnelProviderLabel + (L " is not ready: " " не готов: ") + $tunnelStatus.text)
    }

    Write-Host "  ◌ Opening $tunnelProviderLabel..." -ForegroundColor Cyan
    $tunnelProc = Start-Tunnel $tunnelProvider $localPort $tunnelOutLog $tunnelErrLog

    $tunnelUrl = Wait-TunnelUrl $tunnelOutLog $tunnelErrLog $tunnelProvider
    if (!$tunnelUrl) {
        Stop-Pid $tunnelProc.Id
        Stop-Pid $serverPid
        $tail = Get-LogTailText @($tunnelOutLog, $tunnelErrLog)
        if (!$tail) { $tail = L "Logs are empty." "Логи пустые." }
        if ($tunnelProvider -eq "tailscale") {
            $enableUrl = Get-TailscaleFunnelEnableUrlFromLogs @($tunnelOutLog, $tunnelErrLog)
            if ($enableUrl) {
                Invoke-TailscaleFunnelEnableFlow $enableUrl
                $tail += "`n`n" + (L "Tailscale Funnel is not enabled for this tailnet. KaroX copied the enable URL and tried to open it in your browser: " "Tailscale Funnel не включён в вашем tailnet. KaroX скопировал ссылку включения и попытался открыть её в браузере: ") + $enableUrl + "`n" + (L "Approve Funnel in Tailscale and start the session again." "Подтвердите Funnel в Tailscale и запустите сессию ещё раз.")
            } else {
                $tail += "`n`n" + (L "Tip: open G = Settings, choose Tailscale Funnel, and press L to run tailscale up. If Funnel needs approval, approve it in Tailscale and start the session again." "Подсказка: откройте G = настройки, выберите Tailscale Funnel и нажмите L, чтобы выполнить tailscale up. Если Funnel требует разрешения, подтвердите его в Tailscale и запустите сессию ещё раз.")
            }
        }
        throw ((L "Could not obtain the tunnel URL for " "Не удалось получить URL туннеля ") + $tunnelProviderLabel + (L ". Logs: " ". Логи: ") + "$tunnelOutLog / $tunnelErrLog`n$tail")
    }

    $providerId = Get-ProviderIdFromUrl $tunnelUrl $tunnelProvider $sessionId
    $prompts = Build-Prompts $providerId $tunnelUrl $mode $branch $sessionTitle $commitAllowed $tunnelProvider $aiClient

    $aiClientLabel = Get-AiClientLabel $aiClient
    $sessionInfo = if ((Get-SelectedLanguage) -eq "ru") {
@"
Репозиторий  : $repo
Режим        : $mode
Ветка        : $branch
Сессия       : $sessionTitle
Session ID   : $sessionId
Локальный API: http://127.0.0.1:$localPort
Туннель      : $tunnelProviderLabel
URL туннеля  : $tunnelUrl
AI-клиент    : $aiClientLabel
Provider ID  : $providerId
"@
    } else {
@"
Repository   : $repo
Mode         : $mode
Branch       : $branch
Session      : $sessionTitle
Session ID   : $sessionId
Local API    : http://127.0.0.1:$localPort
Tunnel       : $tunnelProviderLabel
Tunnel URL   : $tunnelUrl
AI client    : $aiClientLabel
Provider ID  : $providerId
"@
    }
    Write-SessionFiles $sessionDir $prompts.connect $prompts.task $apiKey $providerId $sessionInfo

    $session = [ordered]@{
        id = $sessionId
        repo = $repo
        mode = $mode
        branch = $branch
        title = $sessionTitle
        startedAt = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
        localPort = $localPort
        tunnelProvider = $tunnelProvider
        aiClient = $aiClient
        tunnelUrl = $tunnelUrl
        providerId = $providerId
        apiKey = $apiKey
        commitAllowed = ($commitAllowed -eq "true")
        watchdog = $watchdogEnabled
        verifiedRepoRoot = $isolation.repoRoot
        verifiedBranch = $isolation.branch
        verifiedMode = $isolation.mode
        sessionDir = $sessionDir
        serverPid = $serverPid
        tunnelPid = $tunnelProc.Id
        serverOutLog = $serverOutLog
        serverErrLog = $serverErrLog
        tunnelOutLog = $tunnelOutLog
        tunnelErrLog = $tunnelErrLog
    }
    Save-SessionJson $sessionDir $session

    Write-Host ""
    Write-Host "  ● Workspace is live" -ForegroundColor Green
    Write-Host ((L "Tunnel URL    : " "URL туннеля : ") + $tunnelUrl)
    Write-Host ("Provider ID   : " + $providerId)
    Write-Host ((L "Session folder: " "Папка сессии: ") + $sessionDir)
    return (Load-SessionJson $sessionDir)
}

function Copy-SessionFile($session, $fileName, $message) {
    $path = Join-Path $session.sessionDir $fileName
    if (!(Test-Path -LiteralPath $path)) {
        Write-Host ((L "File not found: " "Файл не найден: ") + $path) -ForegroundColor Yellow
        return
    }
    Get-Content -Raw -LiteralPath $path | Set-Clipboard
    Write-Host $message -ForegroundColor Green
}

function Show-LogTail($session) {
    Header (L "Session logs" "Логи сессии")
    $paths = @($session.serverOutLog, $session.serverErrLog)
    if ($session.serverRunnerOutLog) { $paths += $session.serverRunnerOutLog }
    if ($session.serverRunnerErrLog) { $paths += $session.serverRunnerErrLog }
    $paths += @($session.tunnelOutLog, $session.tunnelErrLog)
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) {
            Write-Host "---- $path" -ForegroundColor DarkCyan
            Get-Content -LiteralPath $path -Tail 20 -ErrorAction SilentlyContinue
            Write-Host ""
        }
    }
    Read-Host (L "Enter to return" "Enter для возврата") | Out-Null
}

function Stop-ServerChild($session) {
    if (!$session -or !$session.sessionDir) { return }
    $pidFile = Join-Path ([string]$session.sessionDir) "server.pid"
    if (!(Test-Path -LiteralPath $pidFile)) { return }
    $raw = ""
    try { $raw = (Get-Content -Raw -LiteralPath $pidFile -ErrorAction Stop).Trim() } catch {}
    if ($raw -match "^\d+$") {
        & taskkill /PID $raw /T /F 2>$null | Out-Null
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

function Stop-Session($session) {
    Stop-Pid $session.tunnelPid
    Stop-Pid $session.serverPid
    Stop-Pid $session.serverRunnerPid
    Stop-ServerChild $session
    Write-Host ((L "Session stopped: " "Сессия остановлена: ") + $session.id) -ForegroundColor Yellow
}

function Resolve-SessionDirForDelete($session) {
    if (!$session -or !$session.sessionDir) { return $null }
    if (!(Test-Path -LiteralPath $session.sessionDir)) { return $null }

    $root = (Resolve-Path -LiteralPath $SessionsDir).Path.TrimEnd(@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar))
    $target = (Resolve-Path -LiteralPath $session.sessionDir).Path
    $rootPrefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if ($target -eq $root -or !$target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ((L "Session folder is outside the RepoPilot sessions directory: " "Папка сессии вне каталога RepoPilot sessions: ") + $target)
    }
    return $target
}

function Remove-SessionHistory($session) {
    $serverAlive = Test-ProcessAlive $session.serverPid
    $tunnelAlive = Test-ProcessAlive $session.tunnelPid
    if ($serverAlive -or $tunnelAlive) {
        return [pscustomobject]@{
            ok = $false
            id = [string]$session.id
            deleted = $false
            reason = (L "The session is still running or partially active" "Сессия ещё запущена или частично активна")
        }
    }

    try {
        $target = Resolve-SessionDirForDelete $session
        if (!$target) {
            return [pscustomobject]@{
                ok = $true
                id = [string]$session.id
                deleted = $false
                reason = (L "History folder is already absent" "Папка истории уже отсутствует")
            }
        }

        Remove-Item -LiteralPath $target -Recurse -Force
        return [pscustomobject]@{
            ok = $true
            id = [string]$session.id
            deleted = $true
            reason = ""
        }
    } catch {
        return [pscustomobject]@{
            ok = $false
            id = [string]$session.id
            deleted = $false
            reason = $_.Exception.Message
        }
    }
}

function Remove-StoppedSessionHistories {
    $results = @()
    foreach ($session in @(Get-Sessions | Where-Object { $_.status -eq "stopped" })) {
        $results += Remove-SessionHistory $session
    }
    return @($results)
}

function Stop-AllSessions {
    foreach ($session in @(Get-Sessions)) {
        Stop-Pid $session.tunnelPid
        Stop-Pid $session.serverPid
        Stop-Pid $session.serverRunnerPid
        Stop-ServerChild $session
    }
    Stop-Old
}



function Show-MissionBrief($session) {
    Header (L "Mission Control" "Центр управления")
    UI-Notice "progress" (L "Building a live context brief" "Формирую актуальный контекст") (L "Read-only · secret-free · no repository changes" "Только чтение · без секретов · без изменений")
    $result = Invoke-SessionApi $session.apiKey $session.localPort "/context/brief"
    if (-not $result.ok) {
        UI-Notice "error" (L "Mission context unavailable" "Контекст миссии недоступен") $result.error
        Write-Host ""; Read-Host (L "  Enter to return" "  Enter — назад") | Out-Null
        return $false
    }
    $brief = $result.value
    $identity = $brief.identity; $task = $brief.task; $git = $brief.git; $permissions = $brief.permissions
    UI-Section (L "Current mission" "Текущая миссия")
    $taskStatus = switch ([string]$task.status) {
        "running" { L "RUNNING" "ВЫПОЛНЯЕТСЯ" }
        "completed" { L "COMPLETED" "ЗАВЕРШЕНА" }
        "finished" { L "FINISHED" "ЗАВЕРШЕНА" }
        default { L "WAITING FOR TASK" "ОЖИДАЕТ ЗАДАЧУ" }
    }
    UI-KeyValue (L "Task" "Задача") $taskStatus $(if ($task.status -eq "running") { "Green" } else { "Yellow" })
    UI-KeyValue (L "Repository" "Репозиторий") (Get-RepoLabel $identity.repoRoot)
    UI-KeyValue (L "Branch" "Ветка") $identity.branch "Cyan"
    $accessMode = switch ([string]$identity.mode) {
        "read_only" { L "OBSERVE · read only" "НАБЛЮДЕНИЕ · только чтение" }
        "autopilot" { L "BUILD · isolated branch" "СБОРКА · отдельная ветка" }
        "worktree_write" { L "RESUME · current branch" "ПРОДОЛЖЕНИЕ · текущая ветка" }
        "full" { L "ADVANCED · full repository access" "РАСШИРЕННЫЙ · полный доступ" }
        default { [string]$identity.mode }
    }
    UI-KeyValue (L "Access" "Доступ") $accessMode
    $workingTree = if ($git.clean) {
        if ((Get-SelectedLanguage) -eq "ru") { "ЧИСТО" } else { "CLEAN" }
    } else {
        if ((Get-SelectedLanguage) -eq "ru") { "ПРОВЕРИТЬ $($git.changedCount) изменений" } else { "REVIEW $($git.changedCount) changed paths" }
    }
    UI-KeyValue (L "Working tree" "Рабочее дерево") $workingTree $(if ($git.clean) { "Green" } else { "Yellow" })
    UI-KeyValue (L "Commit" "Commit") $(if ($permissions.commitAllowed) { L "allowed via /git/commit" "разрешён через /git/commit" } else { L "blocked" "заблокирован" })
    UI-KeyValue (L "Push" "Push") $(if ($permissions.pushAllowed) { L "allowed" "разрешён" } else { L "blocked by policy" "заблокирован политикой" }) $(if ($permissions.pushAllowed) { "Yellow" } else { "Green" })
    UI-Section (L "Next move" "Следующий шаг")
    $next = Get-MissionActionText ([string]$brief.recommendedNextAction)
    UI-Notice $(if ($brief.recommendedNextAction -eq "stop_and_report_branch_mismatch") { "error" } elseif (@($brief.warnings).Count -gt 0) { "warn" } else { "success" }) $next $brief.recommendedNextAction
    if (@($brief.warnings).Count -gt 0) {
        UI-Section (L "Guardrails" "Ограничения")
        foreach ($warning in @($brief.warnings)) { UI-Wrap ("! " + $warning) "Yellow" 5 }
    }
    UI-Section (L "Context routes" "Маршруты контекста")
    UI-Wrap "/project/info  /tree/dir  /files/search  /files/read" "DarkGray" 4
    UI-Wrap "/git/diff/stat  /git/diff/file  /audit  /session/report" "DarkGray" 4
    Write-Host ""
    Write-Host ("  ✓ " + (L "AI can refresh this snapshot with GET /context/brief." "AI может обновить снимок через GET /context/brief.")) -ForegroundColor Green
    Write-Host ""; Read-Host (L "  Enter to return" "  Enter — назад") | Out-Null
    return $true
}

function Show-AiReadiness($session) {
    Header (L "AI readiness" "Готовность для AI")
    UI-Notice "progress" (L "Running four local preflight checks" "Выполняю четыре локальные проверки") (L "Nothing is sent to the AI yet." "Данные ещё не передаются AI.")
    $checks = @(
        [pscustomobject]@{ name="/session"; result=(Invoke-SessionApi $session.apiKey $session.localPort "/session") },
        [pscustomobject]@{ name="/health"; result=(Invoke-SessionApi $session.apiKey $session.localPort "/health") },
        [pscustomobject]@{ name="/git/status"; result=(Invoke-SessionApi $session.apiKey $session.localPort "/git/status") },
        [pscustomobject]@{ name="/context/brief"; result=(Invoke-SessionApi $session.apiKey $session.localPort "/context/brief") }
    )
    $allOk = $true
    UI-Section (L "Preflight" "Предварительная проверка")
    foreach ($check in $checks) {
        if ($check.result.ok) {
            Write-Host "  │  ✓ " -NoNewline -ForegroundColor Green; Write-Host $check.name
        } else {
            $allOk = $false
            Write-Host "  │  ✗ " -NoNewline -ForegroundColor Red; Write-Host ($check.name + " — " + $check.result.error)
        }
    }
    if ($allOk) {
        $actual = $checks[0].result.value
        $matches = ([string]$actual.repoRoot -eq [string]$session.repo) -and ([string]$actual.branch -eq [string]$session.branch)
        UI-Section (L "Verdict" "Результат")
        if ($matches) {
            UI-Notice "success" (L "READY FOR AI HANDOFF" "ГОТОВО К ПЕРЕДАЧЕ AI") (L "Repository and branch match this session card." "Репозиторий и ветка совпадают с карточкой.")
        } else {
            UI-Notice "error" (L "HANDOFF BLOCKED" "ПЕРЕДАЧА ЗАБЛОКИРОВАНА") (L "Repository or branch mismatch. Stop and verify the session." "Репозиторий или ветка не совпадают. Остановитесь и проверьте сессию.")
            $allOk = $false
        }
        UI-KeyValue (L "Repository" "Репозиторий") (Get-RepoLabel $actual.repoRoot)
        UI-KeyValue (L "Branch" "Ветка") $actual.branch "Cyan"
        UI-KeyValue (L "Mode" "Режим") $actual.mode
        UI-KeyValue (L "Commit" "Commit") $actual.commitAllowed
        UI-KeyValue (L "Push" "Push") $actual.pushAllowed $(if ($actual.pushAllowed) { "Yellow" } else { "Green" })
    }
    Write-Host ""; Read-Host (L "  Enter to return" "  Enter — назад") | Out-Null
    return $allOk
}

function Session-Menu($session) {
    while ($true) {
        $fresh = Load-SessionJson $session.sessionDir; if ($fresh) { $session = $fresh }
        $serverAlive = Test-ProcessAlive $session.serverPid
        $tunnelAlive = Test-ProcessAlive $session.tunnelPid
        $status = if ($serverAlive -and $tunnelAlive) { "running" } elseif ($serverAlive -or $tunnelAlive) { "partial" } else { "stopped" }
        Header ((L "Workspace / " "Рабочее пространство / ") + $session.title)
        UI-StatusBanner $status (Get-RepoLabel $session.repo)
        UI-Section (L "Flight data" "Параметры полёта")
        UI-KeyValue (L "Repository" "Репозиторий") (Get-RepoLabel $session.repo)
        UI-KeyValue (L "Branch" "Ветка") $session.branch "Cyan"
        UI-KeyValue (L "Access" "Доступ") $session.mode
        UI-KeyValue (L "AI target" "AI-клиент") (Get-AiClientLabel $session.aiClient) "Magenta"
        UI-Section (L "Connection" "Подключение")
        UI-KeyValue (L "Public URL" "Публичный URL") $session.tunnelUrl "Cyan"
        UI-KeyValue (L "Tunnel" "Туннель") (Get-ProviderLabel $session.tunnelProvider)
        UI-KeyValue "Provider ID" $session.providerId
        UI-KeyValue (L "Local API" "Локальный API") "127.0.0.1:$($session.localPort)" "DarkGray"
        UI-Section (L "AI launch sequence" "Запуск AI")
        UI-Choice "V" (L "VERIFY" "ПРОВЕРИТЬ") (L "Run exact-session readiness checks" "Проверить точную сессию и готовность") "Green"
        UI-Choice "M" (L "MISSION" "МИССИЯ") (L "Inspect live context, warnings and next action" "Открыть контекст, предупреждения и следующий шаг") "Cyan"
        UI-Choice "A" (L "HANDOFF" "ПЕРЕДАТЬ") (L "Copy the complete connection package" "Скопировать полный пакет подключения") "Magenta"
        Write-Host ("  │  [C] " + (L "Connect" "Подключение") + "   [T] " + (L "Task" "Задача") + "   [K] " + (L "Key" "Ключ") + "   [P] Provider ID") -ForegroundColor DarkGray
        UI-Section (L "Controls" "Управление")
        Write-Host ("  │  [L] " + (L "Logs" "Логи") + "   [S] " + (L "Stop session" "Остановить") + "   [B] " + (L "Back" "Назад"))
        if ($status -eq "stopped") { Write-Host ("  │  [D] " + (L "Delete stopped history" "Удалить остановленную историю")) -ForegroundColor Red }
        Write-Host ""
        $x = ([string](Read-Host ("  › " + (L "Action" "Действие")))).Trim()
        if ($x -match "^[Vv]$") { Show-AiReadiness $session | Out-Null }
        elseif ($x -match "^[Mm]$") { Show-MissionBrief $session | Out-Null }
        elseif ($x -match "^[Cc]$") { Copy-SessionFile $session "connect-prompt.txt" (L "Connection prompt copied." "Prompt подключения скопирован.") }
        elseif ($x -match "^[Tt]$") { Copy-SessionFile $session "task-prompt.txt" (L "Task template copied." "Шаблон задачи скопирован.") }
        elseif ($x -match "^[Kk]$") { Copy-SessionFile $session "api-key.txt" (L "Secure key copied." "Секретный ключ скопирован.") }
        elseif ($x -match "^[Pp]$") { Copy-SessionFile $session "provider-id.txt" "Provider ID copied." }
        elseif ($x -match "^[Aa]$") { Copy-SessionFile $session "all.txt" (L "Complete handoff copied." "Полный handoff скопирован.") }
        elseif ($x -match "^[Ll]$") { Show-LogTail $session }
        elseif ($x -match "^[Ss]$") { if (Ask-Yes (L "Stop this session?" "Остановить сессию?") $false) { Stop-Session $session; return } }
        elseif ($x -match "^[Dd]$" -and $status -eq "stopped") { if (Ask-Yes (L "Delete session history?" "Удалить историю сессии?") $false) { Remove-SessionHistory $session | Out-Null; return } }
        elseif ($x -match "^[Bb]$") { return }
    }
}

function Show-Manager {
    while ($true) {
        $sessions = @(Get-Sessions)
        $liveCount = @($sessions | Where-Object { $_.status -eq "running" }).Count
        $attentionCount = @($sessions | Where-Object { $_.status -eq "partial" }).Count
        Header (L "Repository sessions" "Сессии репозиториев")
        Write-Host "  " -NoNewline
        UI-Badge (Get-AiClientLabel (Get-SelectedAiClient)) "Magenta"
        Write-Host "  " -NoNewline
        UI-Badge (Get-ProviderLabel (Get-SelectedTunnelProvider)) "Cyan"
        Write-Host "  " -NoNewline
        UI-Badge ((L "LIVE " "АКТИВНЫХ ") + $liveCount) $(if ($liveCount -gt 0) { "Green" } else { "DarkGray" })
        if ($attentionCount -gt 0) { Write-Host "  " -NoNewline; UI-Badge ((L "ATTENTION " "ВНИМАНИЕ ") + $attentionCount) "Yellow" }
        Write-Host ""
        UI-Section (L "Workspaces" "Рабочие пространства")
        if ($sessions.Count -eq 0) {
            UI-EmptyState (L "Your flight deck is clear" "Панель пока пуста") (L "Press N to connect the first Git repository." "Нажмите N, чтобы подключить первый Git-репозиторий.")
        } else {
            for ($i=0; $i -lt $sessions.Count; $i++) {
                UI-WorkspaceCard ($i + 1) $sessions[$i]
            }
        }
        UI-Section (L "Command bar" "Панель команд")
        UI-Choice "N" (L "NEW SESSION" "НОВАЯ СЕССИЯ") (L "Connect a repository and choose access" "Подключить репозиторий и выбрать доступ") "Magenta"
        Write-Host ("  │  [number] " + (L "Open workspace" "Открыть") + "   [R] " + (L "Refresh" "Обновить") + "   [G] " + (L "Settings" "Настройки"))
        Write-Host ("  │  [D] " + (L "Diagnostics" "Диагностика") + "   [U] " + (L "Clear history" "Очистить историю") + "   [X] " + (L "Stop all" "Остановить все"))
        Write-Host ("  │  [Q] " + (L "Close manager — LIVE sessions keep running" "Закрыть менеджер — LIVE-сессии продолжат работу")) -ForegroundColor DarkGray
        Write-Host ""
        $x = ([string](Read-Host ("  › " + (L "Action" "Действие")))).Trim()
        if ($x -match "^[Nn]$") {
            try { $created = Start-NewSession; if ($created) { Session-Menu $created } }
            catch { UI-Notice "error" (L "Could not create session" "Не удалось создать сессию") $_.Exception.Message; Read-Host (L "  Enter to continue" "  Enter — продолжить") | Out-Null }
        }
        elseif ($x -match "^[Rr]$") { continue }
        elseif ($x -match "^[Gg]$") { Show-Settings }
        elseif ($x -match "^[Dd]$") { powershell -ExecutionPolicy Bypass -File (Join-Path $Root "doctor.ps1") -NoPause; Read-Host (L "  Enter to continue" "  Enter — продолжить") | Out-Null }
        elseif ($x -match "^[Uu]$") { Remove-StoppedSessionHistories | Out-Null }
        elseif ($x -match "^[Xx]$") { if (Ask-Yes (L "Stop all local workspace sessions?" "Остановить все локальные сессии?") $false) { Stop-AllSessions } }
        elseif ($x -match "^[Qq]$") { return }
        elseif ($x -match "^\d+$") { $idx=[int]$x-1; if ($idx -ge 0 -and $idx -lt $sessions.Count) { Session-Menu $sessions[$idx] } }
    }
}

Ensure-Installed
Ensure-Language
Show-KaroXIntro
Ensure-AiClient
Show-Manager
