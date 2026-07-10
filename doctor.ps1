param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $Root "server"
$PythonExe = Join-Path $env:LOCALAPPDATA "RepoPilotBridge\.venv\Scripts\python.exe"
# Выделяем свободный порт, чтобы doctor никогда не подключился к живой
# пользовательской KaroX-сессии вместо своего временного репозитория.
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
try {
    $listener.Start()
    $Port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
} finally {
    $listener.Stop()
}
$Base = "http://127.0.0.1:$Port"
$Key = "doctor-" + ([guid]::NewGuid().ToString("N"))
$TmpRepo = Join-Path $env:TEMP ("repopilot-doctor-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$LogDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge\doctor"
$Report = Join-Path $LogDir ("doctor-report-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$script:failures = 0

function Line($x = "") {
    $x | Tee-Object -FilePath $Report -Append | Out-Null
}

function Ok($x) {
    Write-Host "[OK] $x" -ForegroundColor Green
    Line "[OK] $x"
}

function Fail($x) {
    $script:failures++
    Write-Host "[FAIL] $x" -ForegroundColor Red
    Line "[FAIL] $x"
}

function Info($x) {
    Write-Host "[INFO] $x" -ForegroundColor Cyan
    Line "[INFO] $x"
}

function JsonOrNull($text) {
    try { return $text | ConvertFrom-Json } catch { return $null }
}

function Call-Api($Method, $Url, $ApiKey = "", $Body = $null, $TimeoutSec = 180, $ExtraHeaders = $null) {
    $headers = @{}
    if ($ApiKey) {
        $headers["X-API-Key"] = $ApiKey
    }
    if ($ExtraHeaders) {
        foreach ($key in $ExtraHeaders.Keys) {
            $headers[$key] = $ExtraHeaders[$key]
        }
    }

    try {
        $params = @{
            Uri = $Url
            Method = $Method
            Headers = $headers
            UseBasicParsing = $true
            TimeoutSec = $TimeoutSec
        }

        if ($null -ne $Body) {
            $params["ContentType"] = "application/json; charset=utf-8"
            $params["Body"] = ($Body | ConvertTo-Json -Depth 30)
        }

        $resp = Invoke-WebRequest @params

        return [pscustomobject]@{
            Status = [int]$resp.StatusCode
            Text = [string]$resp.Content
            Json = (JsonOrNull ([string]$resp.Content))
            Error = $null
        }
    }
    catch {
        $status = 0
        $text = ""
        $err = $_.Exception.Message

        # Windows PowerShell часто уже читает response stream и оставляет
        # серверный JSON только в ErrorDetails.Message.
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $text = [string]$_.ErrorDetails.Message
        }

        try {
            if ($_.Exception.Response) {
                $status = [int]$_.Exception.Response.StatusCode
                if (!$text) {
                    $stream = $_.Exception.Response.GetResponseStream()
                    if ($stream) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $text = $reader.ReadToEnd()
                    }
                }
            }
        } catch {}

        return [pscustomobject]@{
            Status = $status
            Text = $text
            Json = (JsonOrNull $text)
            Error = $err
        }
    }
}

function Expect($name, $res, [int[]]$codes) {
    if ($codes -contains $res.Status) {
        Ok "$name -> HTTP $($res.Status)"
        return $true
    }

    $body = ""
    if ($res.Text) {
        $body = $res.Text.Substring(0, [Math]::Min(500, $res.Text.Length))
    }

    Fail "$name -> HTTP $($res.Status), ожидалось $($codes -join ', '). Ошибка: $($res.Error). Ответ: $body"
    return $false
}

function Stop-DoctorServers {
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*uvicorn*repo_tools:app*--port*$Port*"
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-Server($mode, $commitAllowed) {
    Stop-DoctorServers

    $env:REPO_ROOT = $TmpRepo
    $env:REPO_ROOT_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($TmpRepo))
    # Не наследуем текстовые поля живой пользовательской сессии:
    # сервер отдаёт предпочтение *_B64 перед обычными переменными.
    Remove-Item Env:REPO_TOOLS_SESSION_TITLE_B64 -ErrorAction SilentlyContinue
    Remove-Item Env:REPO_TOOLS_INITIAL_TASK_B64 -ErrorAction SilentlyContinue
    $env:REPO_TOOLS_API_KEY = $Key
    $env:REPO_TOOLS_MODE = $mode
    $env:REPO_TOOLS_BRANCH = "promptql/doctor"
    $env:REPO_TOOLS_TASK = "doctor"
    $env:REPO_TOOLS_COMMIT_ALLOWED = $commitAllowed
    $env:REPO_TOOLS_HOME = (Join-Path $env:LOCALAPPDATA "RepoPilotBridge")
    $env:REPO_TOOLS_LOG_FILE = (Join-Path $LogDir "repo-tools-$mode.jsonl")
    $env:REPO_TOOLS_RUNS_DIR = (Join-Path $TmpRepo ".promptql\runs")

    $stdout = Join-Path $LogDir "uvicorn-$mode.out.log"
    $stderr = Join-Path $LogDir "uvicorn-$mode.err.log"

    $p = Start-Process -FilePath $PythonExe `
        -ArgumentList @("-m", "uvicorn", "repo_tools:app", "--host", "127.0.0.1", "--port", "$Port") `
        -WorkingDirectory $ServerDir `
        -PassThru `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr

    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500

        $r = Call-Api "GET" "$Base/openapi.json" "" $null 10
        if ($r.Status -eq 200) {
            $sessionCheck = Call-Api "GET" "$Base/session" $Key $null 10
            if ($sessionCheck.Status -eq 200 -and $sessionCheck.Json.repoRoot -eq $TmpRepo -and $sessionCheck.Json.mode -eq $mode) {
                Ok "сервер запущен и изолирован, режим=$mode, порт=$Port, pid=$($p.Id)"
                return $p
            }
            $actualRepo = if ($sessionCheck.Json) { [string]$sessionCheck.Json.repoRoot } else { "<no-json>" }
            $actualMode = if ($sessionCheck.Json) { [string]$sessionCheck.Json.mode } else { "<no-json>" }
            $procArgs = ""
            try {
                $procArgs = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)").CommandLine
            } catch {}
            StopP $p
            throw "doctor isolation failed: base=$Base expectedRepo=$TmpRepo actualRepo=$actualRepo expectedMode=$mode actualMode=$actualMode pid=$($p.Id) args=$procArgs response=$($sessionCheck.Text)"
        }

        if ($p.HasExited) {
            Fail "сервер завершился, режим=$mode. Проверьте лог: $stderr"
            return $p
        }
    }

    Fail "сервер не запустился, режим=$mode"
    return $p
}

function StopP($p) {
    if ($p -and -not $p.HasExited) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
}

function Run-GitQuiet([string[]]$ArgsList) {
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & git @ArgsList *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "git $($ArgsList -join ' ') завершился с кодом $LASTEXITCODE"
        }
    } finally {
        $ErrorActionPreference = $oldPreference
    }
}

Line "============================================================"
Line "Star For KaroX Doctor"
Line "Запущено: $(Get-Date)"
Line "============================================================"

try {
    if (!(Test-Path $PythonExe)) {
        throw "Runtime Python не найден. Сначала запустите install.ps1: $PythonExe"
    }

    if (!(Test-Path (Join-Path $ServerDir "repo_tools.py"))) {
        throw "server\repo_tools.py не найден"
    }

    & $PythonExe -m py_compile (Join-Path $ServerDir "repo_tools.py")
    Ok "синтаксис repo_tools.py корректен"

    New-Item -ItemType Directory -Force -Path $TmpRepo | Out-Null
    Set-Content (Join-Path $TmpRepo "README.md") "# doctor`n" -Encoding UTF8
    New-Item -ItemType Directory -Force -Path (Join-Path $TmpRepo "src") | Out-Null
    Set-Content (Join-Path $TmpRepo "src\a.txt") "a`n" -Encoding UTF8

    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "init")
    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "config", "core.autocrlf", "false")
    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "config", "user.email", "doctor@example.local")
    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "config", "user.name", "KaroX Doctor")
    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "add", ".")
    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "commit", "-m", "initial")
    Run-GitQuiet -ArgsList @("-C", $TmpRepo, "switch", "-c", "promptql/doctor")

    Ok "временный git-репозиторий готов"

    Info "READ ONLY / безопасный просмотр"
    $p = Start-Server "read_only" "false"
    try {
        $openapi = Call-Api "GET" "$Base/openapi.json" ""
        if (Expect "openapi schema" $openapi @(200)) {
            $scheme = $openapi.Json.components.securitySchemes.RepoPilotApiKey
            if ($scheme -and $scheme.type -eq "apiKey" -and $scheme.in -eq "header" -and $scheme.name -eq "X-API-Key") {
                Ok "openapi: RepoPilotApiKey описан как header X-API-Key"
            } else {
                Fail "openapi: RepoPilotApiKey security scheme отсутствует или некорректен"
            }

            $healthGet = $openapi.Json.paths."/health".get
            $hasSecurity = $false
            foreach ($entry in @($healthGet.security)) {
                if ($null -ne $entry.RepoPilotApiKey) { $hasSecurity = $true }
            }
            if ($hasSecurity) {
                Ok "openapi: /health требует RepoPilotApiKey"
            } else {
                Fail "openapi: /health не содержит security requirement RepoPilotApiKey"
            }

            $hasHeaderParam = $false
            foreach ($param in @($healthGet.parameters)) {
                if ($param.in -eq "header" -and $param.name -eq "X-API-Key") { $hasHeaderParam = $true }
            }
            if (!$hasHeaderParam) {
                Ok "openapi: X-API-Key не продублирован как обычный параметр"
            } else {
                Fail "openapi: X-API-Key продублирован как header parameter"
            }
        }
        Expect "read_only: health" (Call-Api "GET" "$Base/health" $Key) @(200) | Out-Null
        Expect "read_only: health через Authorization Bearer" (Call-Api "GET" "$Base/health" "" $null 180 @{ Authorization = "Bearer $Key" }) @(200) | Out-Null
        Expect "read_only: health с пробелами вокруг ключа" (Call-Api "GET" "$Base/health" "  $Key  ") @(200) | Out-Null
        Expect "read_only: статус задачи" (Call-Api "GET" "$Base/task/status" $Key) @(200) | Out-Null
        $readOnlyBrief = Call-Api "GET" "$Base/context/brief" $Key
        if (Expect "read_only: context brief" $readOnlyBrief @(200)) {
            if ($readOnlyBrief.Json.identity.mode -eq "read_only" -and
                $readOnlyBrief.Json.permissions.pushAllowed -eq $false -and
                $readOnlyBrief.Json.permissions.hardBlocksRemainActive -eq $true -and
                @($readOnlyBrief.Json.warnings).Count -gt 0) {
                Ok "read_only: context brief содержит режим, guardrails и предупреждения"
            } else {
                Fail "read_only: context brief неполон или небезопасен"
            }
        }
        Expect "read_only: запись заблокирована" (Call-Api "POST" "$Base/file" $Key @{path="x.txt";content="x"}) @(403) | Out-Null
        Expect "read_only: запуск команд заблокирован" (Call-Api "POST" "$Base/run" $Key @{cmd="git status"}) @(403) | Out-Null
    } finally {
        StopP $p
        Start-Sleep -Seconds 1
    }

    Info "AUTOPILOT / автопилот"
    $p = Start-Server "autopilot" "true"
    try {
        Expect "autopilot: сессия" (Call-Api "GET" "$Base/session" $Key) @(200) | Out-Null

        Expect "запуск задачи" (Call-Api "POST" "$Base/task/start" $Key @{
            task="doctor task"
            mode="autopilot"
            commitAllowed=$true
        }) @(200) | Out-Null

        $activeBrief = Call-Api "GET" "$Base/context/brief" $Key
        if (Expect "autopilot: active context brief" $activeBrief @(200)) {
            if ($activeBrief.Json.task.status -eq "running" -and
                $activeBrief.Json.task.instruction -eq "doctor task" -and
                $activeBrief.Json.identity.branch -eq "promptql/doctor" -and
                $activeBrief.Json.git.clean -eq $true -and
                $activeBrief.Json.recommendedNextAction -eq "inspect_project_context_then_execute_task") {
                Ok "autopilot: context brief отражает задачу, ветку и чистое дерево"
            } else {
                Fail "autopilot: context brief не отражает активную задачу или Git-контекст"
            }
            $briefText = $activeBrief.Text
            if ($briefText -and $briefText -notmatch [regex]::Escape($Key)) {
                Ok "context brief не раскрывает API key"
            } else {
                Fail "context brief пуст или раскрывает API key"
            }
        }

        $unicodeText = "doctor " + [char]0x043A + [char]0x0438 + [char]0x0440
        Expect "autopilot: запись Unicode" (Call-Api "POST" "$Base/file" $Key @{
            path="src\a.txt"
            content=$unicodeText
        }) @(200) | Out-Null

        $afterWrite = Call-Api "GET" "$Base/git/diff/file?path=src/a.txt" $Key
        if (Expect "autopilot: diff существует после записи" $afterWrite @(200)) {
            if ($afterWrite.Json.stdout) {
                Ok "autopilot: записанный файл присутствует в diff"
            } else {
                Fail "autopilot: после успешной записи diff файла пуст"
            }
        }

        Expect "секретный путь заблокирован" (Call-Api "GET" "$Base/file?path=.env" $Key) @(403) | Out-Null
        Expect "выход за пределы репозитория заблокирован" (Call-Api "GET" "$Base/file?path=../outside.txt" $Key) @(400,403) | Out-Null
        Expect "сырой git push заблокирован" (Call-Api "POST" "$Base/run" $Key @{cmd="git push"}) @(403) | Out-Null
        Expect "сырой git commit заблокирован" (Call-Api "POST" "$Base/run" $Key @{cmd="git commit -m nope"}) @(403) | Out-Null
        Expect "helper commit2.py заблокирован при записи" (Call-Api "POST" "$Base/file" $Key @{
            path="commit2.py"
            content="print('nope')"
        }) @(403) | Out-Null
        Expect "helper push_and_check.py заблокирован при запуске" (Call-Api "POST" "$Base/run" $Key @{
            cmd="python push_and_check.py"
        }) @(403) | Out-Null

        $hugeCmd = 'python -c "print(''x''*500000)"'
        $huge = Call-Api "POST" "$Base/run" $Key @{
            cmd=$hugeCmd
            capture="file"
            outputFile=".promptql/runs/huge.txt"
            tail=2000
        } 240

        if (Expect "большой вывод пишется в файл" $huge @(200)) {
            if ($huge.Json.outputFile) {
                Ok "outputFile вернулся"
            } else {
                Fail "outputFile отсутствует"
            }
        }

        New-Item -ItemType Directory -Force -Path (Join-Path $TmpRepo ".gradle\cache") | Out-Null
        Set-Content (Join-Path $TmpRepo ".gradle\cache\junk.lock") "junk" -Encoding UTF8

        Expect "diff stat" (Call-Api "GET" "$Base/git/diff/stat" $Key) @(200) | Out-Null
        Expect "diff name-only" (Call-Api "GET" "$Base/git/diff/name-only" $Key) @(200) | Out-Null
        Expect "diff файла" (Call-Api "GET" "$Base/git/diff/file?path=src/a.txt" $Key) @(200) | Out-Null
        Expect "изменённые файлы" (Call-Api "GET" "$Base/git/changed-files" $Key) @(200) | Out-Null
        $cleanup = Call-Api "POST" "$Base/git/cleanup-generated" $Key
        Expect "очистка generated-файлов" $cleanup @(200) | Out-Null

        $afterCleanup = Call-Api "GET" "$Base/git/diff/file?path=src/a.txt" $Key
        if (Expect "source diff сохранён после cleanup" $afterCleanup @(200)) {
            if ($afterCleanup.Json.stdout) {
                Ok "cleanup не затронул source-файл"
            } else {
                Fail "cleanup ошибочно удалил diff source-файла. Ответ cleanup: $($cleanup.Text)"
            }
        }

        $commit = Call-Api "POST" "$Base/git/commit" $Key @{
            message="doctor commit"
            include=@("src/a.txt")
            cleanupGenerated=$true
            runPreCommitChecks=$false
        } 240

        if (Expect "endpoint commit" $commit @(200)) {
            if ($commit.Json.hash) {
                Ok "commit hash вернулся: $($commit.Json.hash)"
            } else {
                Fail "commit hash отсутствует"
            }
        }

        Expect "отчёт задачи" (Call-Api "GET" "$Base/task/report" $Key) @(200) | Out-Null
        Expect "отчёт сессии" (Call-Api "GET" "$Base/session/report" $Key) @(200) | Out-Null
        Expect "audit json" (Call-Api "GET" "$Base/audit?tail=20" $Key) @(200) | Out-Null
        Expect "завершение задачи" (Call-Api "POST" "$Base/task/finish" $Key @{status="finished"}) @(200) | Out-Null
    } finally {
        StopP $p
        Start-Sleep -Seconds 1
    }

    Info "FULL / полный режим"
    $p = Start-Server "full" "true"
    try {
        Expect "full: произвольная команда echo" (Call-Api "POST" "$Base/run" $Key @{cmd="echo full-ok"}) @(200) | Out-Null
        Expect "full: git push всё равно заблокирован" (Call-Api "POST" "$Base/run" $Key @{cmd="git push"}) @(403) | Out-Null
    } finally {
        StopP $p
        Start-Sleep -Seconds 1
    }
}
catch {
    Fail "doctor завершился с ошибкой: $($_.Exception.Message)"
}
finally {
    try { Stop-DoctorServers } catch {}
    try {
        if (Test-Path $TmpRepo) {
            Remove-Item $TmpRepo -Recurse -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

Line ""
Line "============================================================"
Line "RESULT"
Line "Ошибок: $script:failures"
Line "Отчёт: $Report"
Line "============================================================"

if ($script:failures -eq 0) {
    Write-Host ""
    Write-Host "KaroX doctor прошёл успешно." -ForegroundColor Green
    Write-Host "Отчёт: $Report"
} else {
    Write-Host ""
    Write-Host "KaroX doctor нашёл ошибки: $script:failures" -ForegroundColor Red
    Write-Host "Отчёт: $Report"
}

if (!$NoPause) {
    Read-Host "Нажмите Enter для выхода" | Out-Null
}
