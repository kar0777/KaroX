param(
    [string]$Repository = "kar0777/KaroX",
    [string]$Branch = "",
    [ValidateSet("stable", "preview")]
    [string]$Channel = "stable",
    [switch]$Clean,
    [switch]$ResolveOnly
)

$ErrorActionPreference = "Stop"
try { chcp.com 65001 > $null; [Console]::InputEncoding = [Text.Encoding]::UTF8; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# Streaming downloads: fixed HTTPS hosts, explicit redirects, a wall-clock
# cancellation budget, and size limits even when Content-Length is absent.
function Get-KaroXDownload {
    param([string]$Uri, [string]$OutFile, [long]$MaxBytes, [switch]$AllowMissing)
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $cancel = New-Object System.Threading.CancellationTokenSource
    $cancel.CancelAfter(120000)
    $response = $null
    try {
        for ($redirect = 0; $redirect -le 5; $redirect++) {
            $target = [Uri]$Uri
            if ($target.Scheme -ne 'https' -or $target.Port -ne 443 -or $target.UserInfo -or
                $target.Host -notin @('github.com', 'raw.githubusercontent.com', 'codeload.github.com', 'release-assets.githubusercontent.com')) {
                throw 'KaroX download refused: source/redirect is outside trusted HTTPS hosts.'
            }
            $request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $target)
            $request.Headers.UserAgent.ParseAdd('KaroX-bootstrap')
            try {
                $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
            } finally { $request.Dispose() }
            $status = [int]$response.StatusCode
            if ($status -in @(301, 302, 303, 307, 308)) {
                if (!$response.Headers.Location) { throw 'Download redirect has no location.' }
                $Uri = ([Uri]::new($target, $response.Headers.Location)).AbsoluteUri
                $response.Dispose(); $response = $null
                continue
            }
            if ($status -eq 404 -and $AllowMissing) { return $false }
            if ($status -ne 200) { throw "KaroX download failed (HTTP $status); retry or check GitHub access." }
            $length = $response.Content.Headers.ContentLength
            if ($null -ne $length -and ($length -le 0 -or $length -gt $MaxBytes)) {
                throw 'KaroX download has an invalid or oversized Content-Length.'
            }
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = [IO.File]::Open($OutFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            try {
                $buffer = New-Object byte[] 65536
                [long]$total = 0
                while (($count = $inputStream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token).GetAwaiter().GetResult()) -gt 0) {
                    $total += $count
                    if ($total -gt $MaxBytes) { throw 'KaroX download exceeds its size limit.' }
                    $outputStream.Write($buffer, 0, $count)
                }
                if ($total -eq 0 -or ($null -ne $length -and $total -ne $length)) { throw 'KaroX download was empty or truncated.' }
                $outputStream.Flush($true)
            } finally { $inputStream.Dispose(); $outputStream.Dispose() }
            return $true
        }
        throw 'KaroX download exceeded the redirect limit.'
    } catch {
        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
        # Do not print proxy credentials or signed redirect query strings.
        $detail = if ($_.Exception.Message -match '^(KaroX download|Download redirect)') { $_.Exception.Message } else { 'Network/TLS, timeout, or local I/O failure; check connectivity, proxy settings and disk space.' }
        throw "Download refused: $detail No unchecked fallback will be executed."
    } finally {
        if ($response) { $response.Dispose() }
        $cancel.Dispose(); $client.Dispose(); $handler.Dispose()
    }
}

function Expand-KaroXSafeZip {
    param([string]$Archive, [string]$Destination, [string]$PortableRoot = '')
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($zip.Entries.Count -gt 20000) { throw 'Archive has too many entries.' }
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        [long]$total = 0
        $prefix = $null
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            $parts = $name.TrimEnd('/').Split('/')
            $kind = ($entry.ExternalAttributes -shr 16) -band 61440
            if (!$name -or $name.StartsWith('/') -or $name.Contains('\') -or $kind -notin @(0, 16384, 32768)) {
                throw 'Archive contains an unsafe path, link or device.'
            }
            foreach ($part in $parts) {
                if (!$part -or $part -in @('.', '..') -or $part -match '[\x00-\x1f:<>"|?*]' -or
                    $part -match '[. ]$' -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)') {
                    throw 'Archive contains an unsafe Windows path.'
                }
            }
            if ($null -eq $prefix) { $prefix = $parts[0] }
            if ($parts[0] -cne $prefix -or ($PortableRoot -and ($prefix -cne $PortableRoot -or $parts.Count -gt 2))) {
                throw 'Archive has an unexpected layout.'
            }
            if (!$seen.Add($name.TrimEnd('/'))) { throw 'Archive contains duplicate paths.' }
            $total += $entry.Length
            if ($entry.Length -gt 268435456 -or $total -gt 1073741824) { throw 'Archive exceeds extraction size limits.' }
        }
        [IO.Directory]::CreateDirectory($Destination) | Out-Null
        foreach ($entry in $zip.Entries) {
            $target = Join-Path $Destination $entry.FullName
            if ($entry.FullName.EndsWith('/')) { [IO.Directory]::CreateDirectory($target) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
            $source = $entry.Open()
            $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            try {
                $buffer = New-Object byte[] 65536
                [long]$written = 0
                while (($count = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $written += $count
                    if ($written -gt $entry.Length) { throw 'Archive entry exceeds its declared size.' }
                    $output.Write($buffer, 0, $count)
                }
                if ($written -ne $entry.Length) { throw 'Archive entry is truncated.' }
            } finally { $source.Dispose(); $output.Dispose() }
        }
    } finally { $zip.Dispose() }
}

function Enable-KaroXPath {
    # IEX keeps this change in the calling PowerShell session; child-process
    # installs cannot change their parent's environment, so print the fix too.
    if ($env:Path.Split(';') -notcontains $AppRoot) { $env:Path = "$AppRoot;$env:Path" }
    try {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (!$userPath -or $userPath.Split(';') -notcontains $AppRoot) {
            [Environment]::SetEnvironmentVariable('Path', "$AppRoot;$userPath", 'User')
        }
    } catch { Write-Warning 'Could not persist the user PATH; use the command below in each new terminal.' }
    $quotedRoot = $AppRoot.Replace("'", "''")
    Write-Host 'Ready now: karox'
    Write-Host "If this was run in a child PowerShell, fix PATH in the original window:"
    Write-Host "`$env:Path = '$quotedRoot;' + `$env:Path"
    Write-Host "Or launch directly: & '$quotedRoot\KaroX.cmd'"
}

if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'Invalid GitHub repository.' }
if (!$PSBoundParameters.ContainsKey('Channel') -and $env:KAROX_BOOTSTRAP_CHANNEL) {
    $Channel = $env:KAROX_BOOTSTRAP_CHANNEL
    if ($Channel -notin @('stable', 'preview')) { throw 'Channel must be stable or preview.' }
}
if (-not $Branch -and $env:KAROX_BOOTSTRAP_REF) { $Branch = $env:KAROX_BOOTSTRAP_REF }
if (-not $Branch) {
    try {
        $manifest = if ($Channel -eq "preview") { "PREVIEW.json" } else { "RELEASE.json" }
        $manifestPath = Join-Path $env:TEMP ("karox-manifest-" + [Guid]::NewGuid().ToString('N') + '.json')
        try {
            $null = Get-KaroXDownload -Uri "https://raw.githubusercontent.com/$Repository/main/$manifest" -OutFile $manifestPath -MaxBytes 65536
            $releaseStatus = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        } finally { Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue }
        if ($releaseStatus.tag) { $Branch = [string]$releaseStatus.tag }
        elseif ($releaseStatus.version) { $Branch = "v" + [string]$releaseStatus.version }
    } catch {
        throw "Could not resolve the $Channel release; no unverified branch fallback. Retry or set KAROX_BOOTSTRAP_REF. $($_.Exception.Message)"
    }
}
if (-not $Branch) { throw 'Release manifest has no tag or version.' }
if ($Branch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or $Branch.Contains('..')) { throw 'Invalid bootstrap ref.' }

if ($ResolveOnly) {
    Write-Output $Branch
    exit 0
}

$AppRoot = if ($env:KAROX_INSTALL_ROOT) { $env:KAROX_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "KaroX" }
$ConfigDir = Join-Path $env:APPDATA "KaroX"
$SourceDir = Join-Path $AppRoot "source"
$ZipPath = Join-Path $env:TEMP ("karox-" + [guid]::NewGuid().ToString("N") + ".zip")
$ExtractDir = Join-Path $env:TEMP ("karox-" + [guid]::NewGuid().ToString("N"))
$ZipRefKind = if ($Branch -match "^v?\d+\.\d+\.\d+") { "tags" } else { "heads" }
$ZipUrl = "https://codeload.github.com/$Repository/zip/refs/$ZipRefKind/$Branch"
if ($Branch -match '^[0-9a-fA-F]{40}$') { $ZipUrl = "https://codeload.github.com/$Repository/zip/$Branch" }

function Remove-PathIfExists($path) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
}

function Install-PortableIfAvailable {
    if ($Branch -notmatch '^v5\.\d+\.\d+(?:(?:a|b|rc)\d+)?$') { return $false }
    # Windows PowerShell 5.1 on older .NET lacks RuntimeInformation.
    try { $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }
    catch { $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE } }
    if ($arch -eq 'AMD64') { $arch = 'X64' }
    if ($arch -eq "X64") { $platform = "windows-x64" }
    elseif ($arch -eq "Arm64") { $platform = "windows-arm64" }
    else { return $false }

    $asset = "KaroX-$Branch-$platform-portable.zip"
    $checksumAsset = "KaroX-$Branch-SHA256SUMS.txt"
    $releaseBase = "https://github.com/$Repository/releases/download/$Branch"
    $archive = Join-Path $env:TEMP ("karox-portable-" + [Guid]::NewGuid().ToString("N") + ".zip")
    $checksums = Join-Path $env:TEMP ("karox-checksums-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $extractRoot = Join-Path $AppRoot (".portable-new-" + [Guid]::NewGuid().ToString("N"))
    try {
        if (!(Get-KaroXDownload -Uri "$releaseBase/$asset" -OutFile $archive -MaxBytes 268435456 -AllowMissing)) { return $false }
        $null = Get-KaroXDownload -Uri "$releaseBase/$checksumAsset" -OutFile $checksums -MaxBytes 1048576

        $line = Get-Content -LiteralPath $checksums | Where-Object { $_ -match ('^[0-9a-fA-F]{64}\s+\*?' + [regex]::Escape($asset) + '$') } | Select-Object -First 1
        if (!$line) { throw "Portable KaroX checksum entry is missing." }
        $expected = (($line -split '\s+')[0]).Trim().ToLowerInvariant()
        if ($expected -notmatch '^[0-9a-f]{64}$') { throw "Portable KaroX checksum entry is invalid." }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "Portable KaroX archive checksum mismatch; refusing to install." }

        Expand-KaroXSafeZip -Archive $archive -Destination $extractRoot -PortableRoot $asset.Substring(0, $asset.Length - 4)
        $bundleDirs = @(Get-ChildItem -LiteralPath $extractRoot -Directory)
        if ($bundleDirs.Count -ne 1) { throw "Portable KaroX archive has an unexpected layout." }
        $candidateDir = $bundleDirs[0].FullName
        $candidateLauncher = Join-Path $candidateDir "karox.cmd"
        $candidateUv = Join-Path $candidateDir "uv.exe"
        if (!(Test-Path -LiteralPath $candidateLauncher -PathType Leaf) -or !(Test-Path -LiteralPath $candidateUv -PathType Leaf)) {
            throw "Portable KaroX launcher or uv runtime is missing."
        }

        $portableDir = Join-Path $AppRoot "portable"
        $backupDir = Join-Path $AppRoot (".portable-old-" + [Guid]::NewGuid().ToString("N"))
        $launcher = Join-Path $AppRoot "KaroX.cmd"
        if (Test-Path -LiteralPath $portableDir) { Move-Item -LiteralPath $portableDir -Destination $backupDir }
        try {
            Move-Item -LiteralPath $candidateDir -Destination $portableDir
            $portableLauncher = Join-Path $portableDir "karox.cmd"
            # Relative ASCII launcher also works for non-ASCII install roots.
            Set-Content -LiteralPath ($launcher + '.new') -Encoding ASCII -Value '@echo off', 'setlocal DisableDelayedExpansion', '"%~dp0portable\karox.cmd" %*'
            Move-Item -LiteralPath ($launcher + '.new') -Destination $launcher -Force
        } catch {
            Remove-PathIfExists $portableDir
            if (Test-Path -LiteralPath $backupDir) { Move-Item -LiteralPath $backupDir -Destination $portableDir }
            throw
        }
        Remove-PathIfExists $backupDir
        $portableLauncher = Join-Path $portableDir "karox.cmd"
        try {
            $desktop = [Environment]::GetFolderPath("Desktop")
            if ($desktop) {
                $shell = New-Object -ComObject WScript.Shell
                $shortcut = $shell.CreateShortcut((Join-Path $desktop "KaroX.lnk"))
                $shortcut.TargetPath = $launcher
                $shortcut.WorkingDirectory = $portableDir
                $shortcut.Save()
            }
        } catch {}
        Write-Host "Portable KaroX installed: $portableDir"
        Write-Host "Launcher: $launcher"
        Enable-KaroXPath
        return $true
    }
    finally {
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $checksums -Force -ErrorAction SilentlyContinue
        Remove-PathIfExists $extractRoot
    }
}

Write-Host ""
Write-Host "KaroX $Channel installer" -ForegroundColor Cyan
Write-Host "----------------------------------------" -ForegroundColor DarkCyan
Write-Host "Repository : https://github.com/$Repository"
Write-Host "Release/ref: $Branch"
Write-Host ""

if ($Clean) {
    Remove-PathIfExists $ConfigDir
    Remove-PathIfExists $AppRoot
}
New-Item -ItemType Directory -Force -Path $AppRoot | Out-Null

if (Install-PortableIfAvailable) {
    $launcher = Join-Path $AppRoot "KaroX.cmd"
    if ($env:KAROX_NO_START -eq "1") { exit 0 }
    & $launcher
    exit $LASTEXITCODE
}
if ($Branch -match '^v5\.\d+\.\d+(?:(?:a|b|rc)\d+)?$') {
    Write-Warning "Portable asset was not available; falling back to the source installer."
}

try {
    if ($PSScriptRoot -and
        (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'install.karox.ps1') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'scripts/install_guard.ps1') -PathType Leaf)) {
        # Match the POSIX bootstrap: an existing local checkout needs no download.
        $SourceDir = $PSScriptRoot
    } else {
        # Source archives have no official release checksum manifest; this is an
        # explicit developer fallback only, never an unchecked network fallback.
        if ($env:KAROX_SOURCE_SHA256 -notmatch '^[0-9a-fA-F]{64}$') {
            throw 'No portable bundle for this ref/platform. Use a source checkout installer, or set KAROX_SOURCE_SHA256 to the trusted source archive digest and retry.'
        }
        # Bounded replacement for Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl.
        $null = Get-KaroXDownload -Uri $ZipUrl -OutFile $ZipPath -MaxBytes 268435456
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash -ne $env:KAROX_SOURCE_SHA256) {
            throw 'Source archive checksum mismatch; refusing to install.'
        }
        Expand-KaroXSafeZip -Archive $ZipPath -Destination $ExtractDir
        $repoDir = Get-ChildItem -LiteralPath $ExtractDir -Directory | Select-Object -First 1
        if (!$repoDir) { throw "GitHub archive did not contain a project directory." }

        if (!(Test-Path -LiteralPath (Join-Path $repoDir.FullName 'install.karox.ps1') -PathType Leaf) -or
            !(Test-Path -LiteralPath (Join-Path $repoDir.FullName 'scripts/install_guard.ps1') -PathType Leaf)) {
            throw 'Source installer or guard missing; preserving previous installation.'
        }
        # Stage on the install filesystem before replacing the previous checkout.
        $sourceStage = Join-Path $AppRoot ('.source-new-' + [Guid]::NewGuid().ToString('N'))
        $sourceBackup = $sourceStage + '.previous'
        try {
            Copy-Item -LiteralPath $repoDir.FullName -Destination $sourceStage -Recurse
            if (Test-Path -LiteralPath $SourceDir) { Move-Item -LiteralPath $SourceDir -Destination $sourceBackup }
            try { Move-Item -LiteralPath $sourceStage -Destination $SourceDir }
            catch {
                if (Test-Path -LiteralPath $sourceBackup) { Move-Item -LiteralPath $sourceBackup -Destination $SourceDir }
                throw
            }
            Remove-PathIfExists $sourceBackup
        } finally { Remove-PathIfExists $sourceStage }

    }

    $installer = Join-Path $SourceDir "install.karox.ps1"
    $guard = Join-Path $SourceDir "scripts\install_guard.ps1"
    if (!(Test-Path -LiteralPath $installer)) { throw "install.karox.ps1 was not found after extraction." }
    if (!(Test-Path -LiteralPath $guard)) { throw "scripts\install_guard.ps1 was not found after extraction." }
    $guardArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $guard, "-Installer", $installer)
    if ($env:KAROX_NO_START -ne "1") { $guardArgs += "-Start" }
    & powershell @guardArgs
    $installExitCode = $LASTEXITCODE
    if ($installExitCode -eq 0) { Enable-KaroXPath }
    exit $installExitCode
}
finally {
    Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-PathIfExists $ExtractDir
}
