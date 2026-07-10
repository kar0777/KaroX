#!/usr/bin/env bash
# RepoPilot Bridge Doctor — диагностический harness для проверки сервера и безопасности.
# Порт doctor.ps1 под POSIX (macOS / Linux). Запускает сервер во всех режимах
# (read_only / autopilot / full) на временном git-репозитории и проверяет эндпоинты,
# блокировки опасных команд, изоляцию ветки и защиту секретов.
#
# Совместим с bash 3.2 (macOS по умолчанию). Не использует bashisms 4+.

set -u

# --- Определение путей -------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/server"
if [ ! -f "$SERVER_DIR/repo_tools.py" ]; then
    # doctor.sh может лежать рядом с server/ или быть установленным в app/
    for cand in "$SCRIPT_DIR/server" "$SCRIPT_DIR/../server" "$SCRIPT_DIR"; do
        if [ -f "$cand/repo_tools.py" ]; then
            SERVER_DIR="$(cd "$cand" && pwd)"
            break
        fi
    done
fi

# Python: venv (если установлен) либо системный python3. Можно переопределить
# через REPOPILOT_PYTHON (используется smoke-test контейнером).
RUNTIME_BASE="${REPOPILOT_RUNTIME:-$HOME/.local/share/RepoPilotBridge}"
VENV_PYTHON="$RUNTIME_BASE/.venv/bin/python"
if [ -n "${REPOPILOT_PYTHON:-}" ]; then
    PYTHON_EXE="$REPOPILOT_PYTHON"
elif [ -x "$VENV_PYTHON" ]; then
    PYTHON_EXE="$VENV_PYTHON"
else
    PYTHON_EXE="$(command -v python3 || command -v python || true)"
fi

PORT="${REPOPILOT_DOCTOR_PORT:-8797}"
BASE="http://127.0.0.1:$PORT"
# API-ключ: 64 hex-символа, как делает start (uuid+uuid без дефисов).
KEY="doctor-$(python3 -c 'import uuid; print(uuid.uuid4().hex + uuid.uuid4().hex)' 2>/dev/null || echo "$(date +%s)$$fallbackkey0000000000000000000000000000000000000000000000")"
TMP_REPO="$(mktemp -d -t repopilot-doctor-XXXXXX 2>/dev/null || mktemp -d)"
LOG_DIR="$RUNTIME_BASE/doctor"
REPORT="$LOG_DIR/doctor-report-$(date +%Y%m%d-%H%M%S).txt"

mkdir -p "$LOG_DIR"

FAILURES=0

# --- Хелперы вывода (параллельно в терминал и файл) --------------------------
line() { printf '%s\n' "$1" | tee -a "$REPORT" >/dev/null; }
ok()   { printf '\033[32m[OK]\033[0m   %s\n' "$1"; line "[OK]   $1"; }
fail() { FAILURES=$((FAILURES + 1)); printf '\033[31m[FAIL]\033[0m %s\n' "$1"; line "[FAIL] $1"; }
info() { printf '\033[36m[INFO]\033[0m %s\n' "$1"; line "[INFO] $1"; }

# JSON-парсинг через надёжный интерпретатор (тот же, что запускает сервер).
# Принимает JSON через stdin, чтобы избежать проблем с переносом строк/кавычками
# в аргументах. json_get '<dotted.path>' читает JSON из stdin.
json_get() {
    "$PYTHON_EXE" -c '
import json, sys
try:
    obj = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
cur = obj
for part in sys.argv[1].split("."):
    if isinstance(cur, list):
        try: cur = cur[int(part)]
        except Exception: print(""); sys.exit(0)
    elif isinstance(cur, dict):
        cur = cur.get(part)
    else:
        print(""); sys.exit(0)
    if cur is None:
        print(""); sys.exit(0)
print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur, ensure_ascii=False))
' "$1"
}

# --- HTTP-вызовы через curl --------------------------------------------------
# call_api <METHOD> <URL> [API_KEY] [JSON_BODY] [EXTRA_HEADER]
# Выводит "<status>\t<body>". Коды ошибок сети = status 0.
call_api() {
    local method="$1" url="$2" api_key="${3:-}" body="${4:-}" extra="${5:-}"
    local tmp_file body_file=""
    tmp_file="$(mktemp -t rpapi-XXXXXX 2>/dev/null || mktemp)"
    local code
    local -a hdrs=(-H "Content-Type: application/json; charset=utf-8")
    [ -n "$api_key" ] && hdrs+=(-H "X-API-Key: $api_key")
    [ -n "$extra" ] && hdrs+=(-H "$extra")
    # Тело передаём через временный файл и --data-binary, чтобы сохранить
    # UTF-8 без искажений (curl --data ломает не-ASCII на некоторых платформах).
    local -a data_args=()
    if [ -n "$body" ]; then
        body_file="$(mktemp -t rpbody-XXXXXX 2>/dev/null || mktemp)"
        printf '%s' "$body" > "$body_file"
        data_args+=(--data-binary "@$body_file")
    fi
    code="$(curl -s -o "$tmp_file" -w "%{http_code}" \
        --max-time 240 \
        -X "$method" \
        "${hdrs[@]}" \
        ${data_args[@]+"${data_args[@]}"} \
        "$url" 2>/dev/null || echo 0)"
    local resp
    resp="$(cat "$tmp_file" 2>/dev/null || true)"
    rm -f "$tmp_file" 2>/dev/null
    [ -n "$body_file" ] && rm -f "$body_file" 2>/dev/null
    printf '%s\t%s' "$code" "$resp"
}

# expect <name> <status> <expected_codes...>
expect() {
    local name="$1" status="$2"; shift 2
    local matched=0
    for want in "$@"; do
        [ "$status" = "$want" ] && matched=1
    done
    if [ "$matched" = 1 ]; then
        ok "$name -> HTTP $status"
        return 0
    fi
    fail "$name -> HTTP $status, ожидалось $*. Ошибка сети/сервера."
    return 1
}

# --- Управление сервером -----------------------------------------------------
# PID запущенного doctor-сервера (для надёжной остановки через $!).
DOCTOR_PID=""

# Освобождаем порт $PORT от любых процессов uvicorn. Используем несколько
# стратегий: сохранённый PID, pgrep по командной строке, и lsof/fuser по порту
# (macOS/Linux) или taskkill (Windows Git Bash).
stop_doctor_servers() {
    # 1. Сохранённый PID (самый надёжный путь).
    if [ -n "${DOCTOR_PID:-}" ]; then
        kill_tree "$DOCTOR_PID" 2>/dev/null || true
    fi
    # 2. pgrep по командной строке (macOS/Linux; есть не везде).
    if command -v pgrep >/dev/null 2>&1; then
        local pids
        pids="$(pgrep -f "uvicorn.*repo_tools:app.*--port.*$PORT" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            # shellcheck disable=SC2086
            kill_tree $pids 2>/dev/null || true
        fi
    fi
    # 3. По порту: lsof (macOS/многие Linux) или fuser (другие Linux).
    if command -v lsof >/dev/null 2>&1; then
        local port_pids
        port_pids="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
        if [ -n "$port_pids" ]; then
            # shellcheck disable=SC2086
            kill_tree $port_pids 2>/dev/null || true
        fi
    elif command -v fuser >/dev/null 2>&1; then
        fuser -k "${PORT}/tcp" 2>/dev/null || true
    fi
    # 4. Windows Git Bash: taskkill по оконечному порту через netstat.
    if command -v taskkill >/dev/null 2>&1; then
        local win_pids
        win_pids="$(netstat -ano 2>/dev/null | grep -E "[:.]$PORT\s" | awk '{print $5}' | sort -u || true)"
        if [ -n "$win_pids" ]; then
            for wp in $win_pids; do
                taskkill //PID "$wp" //T //F >/dev/null 2>&1 || true
            done
        fi
    fi
    DOCTOR_PID=""
    # Даём сокету время освободиться (TIME_WAIT и т.п.).
    sleep 1
}

# kill_tree <pid...> — убивает процесс и его потомков. На Windows Git Bash
# kill -9 работает для PID MSYS, но для процессов Python (нативные Win PID)
# нужен taskkill. Пробуем оба пути.
kill_tree() {
    local pid
    for pid in "$@"; do
        [ -n "$pid" ] || continue
        kill -9 "$pid" 2>/dev/null || true
        if command -v taskkill >/dev/null 2>&1; then
            taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
        fi
    done
}

# start_server <mode> <commit_allowed> -> фоновый сервер запущен, DOCTOR_PID установлен
start_server() {
    local mode="$1" commit_allowed="$2"
    stop_doctor_servers

    export REPO_ROOT="$TMP_REPO"
    export REPO_TOOLS_API_KEY="$KEY"
    export REPO_TOOLS_MODE="$mode"
    export REPO_TOOLS_BRANCH="promptql/doctor"
    export REPO_TOOLS_TASK="doctor"
    export REPO_TOOLS_COMMIT_ALLOWED="$commit_allowed"
    export REPO_TOOLS_HOME="$RUNTIME_BASE"
    export REPO_TOOLS_LOG_FILE="$LOG_DIR/repo-tools-$mode.jsonl"
    export REPO_TOOLS_RUNS_DIR="$TMP_REPO/.promptql/runs"

    local out_log="$LOG_DIR/uvicorn-$mode.out.log"
    local err_log="$LOG_DIR/uvicorn-$mode.err.log"
    : > "$out_log" 2>/dev/null || true
    : > "$err_log" 2>/dev/null || true

    # Запускаем uvicorn в фоне и сразу ловим его PID через $!.
    # Subshell с nohup отсоединяет от терминала; PID — это PID самой nohup/bash,
    # но kill_tree через taskkill /T достанет и дочерний python.
    (cd "$SERVER_DIR" && nohup "$PYTHON_EXE" -m uvicorn repo_tools:app \
        --host 127.0.0.1 --port "$PORT" >"$out_log" 2>"$err_log" & echo $! > "$LOG_DIR/doctor.pid") >/dev/null 2>&1
    DOCTOR_PID="$(cat "$LOG_DIR/doctor.pid" 2>/dev/null || true)"

    local i
    for i in $(seq 1 60); do
        sleep 0.5
        local code
        code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$BASE/openapi.json" 2>/dev/null || echo 0)"
        if [ "$code" = "200" ]; then
            ok "сервер запущен, режим=$mode (PID=${DOCTOR_PID:-?})"
            return 0
        fi
        # Проверим, жив ли процесс (если он умер — нет смысла ждать).
        if [ -n "${DOCTOR_PID:-}" ] && ! kill -0 "$DOCTOR_PID" 2>/dev/null; then
            # На Windows Git Bash kill -0 может ложно срабатывать; проверим лог.
            if [ -s "$err_log" ] && grep -qiE "error|traceback|bind" "$err_log" 2>/dev/null; then
                fail "сервер завершился с ошибкой, режим=$mode. Лог: $err_log"
                return 1
            fi
        fi
    done
    fail "сервер не запустился за 30с, режим=$mode. Лог: $err_log"
    return 1
}

stop_pid() {
    stop_doctor_servers
}

# git тихо, ошибки — в /dev/null
gitq() { git "$@" >/dev/null 2>&1; }

# --- Основная проверка -------------------------------------------------------
line "============================================================"
line "RepoPilot Bridge Doctor (POSIX)"
line "Запущено: $(date)"
line "Python: $PYTHON_EXE"
line "Сервер: $SERVER_DIR"
line "============================================================"

cleanup() {
    stop_doctor_servers 2>/dev/null || true
    rm -rf "$TMP_REPO" 2>/dev/null || true
}
trap cleanup EXIT

if [ -z "$PYTHON_EXE" ] || [ ! -x "$PYTHON_EXE" ]; then
    fail "Python не найден. Установите через install.sh или задайте REPOPILOT_PYTHON."
    exit 1
fi

if [ ! -f "$SERVER_DIR/repo_tools.py" ]; then
    fail "server/repo_tools.py не найден в $SERVER_DIR"
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    fail "curl не найден. Установите: brew install curl (macOS) / apt install curl (Linux)."
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    fail "git не найден. Установите: brew install git (macOS) / apt install git (Linux)."
    exit 1
fi

"$PYTHON_EXE" -m py_compile "$SERVER_DIR/repo_tools.py" && ok "синтаксис repo_tools.py корректен" \
    || fail "синтаксис repo_tools.py битый"

# Готовим временный git-репозиторий.
mkdir -p "$TMP_REPO/src"
printf '# doctor\n' > "$TMP_REPO/README.md"
printf 'a\n' > "$TMP_REPO/src/a.txt"
gitq -C "$TMP_REPO" init
gitq -C "$TMP_REPO" config core.autocrlf false
gitq -C "$TMP_REPO" config user.email "doctor@example.local"
gitq -C "$TMP_REPO" config user.name "RepoPilot Doctor"
gitq -C "$TMP_REPO" add .
gitq -C "$TMP_REPO" commit -m "initial"
gitq -C "$TMP_REPO" switch -c "promptql/doctor"
ok "временный git-репозиторий готов: $TMP_REPO"

# ===== READ ONLY =============================================================
info "READ ONLY / безопасный просмотр"
if start_server "read_only" "false"; then
    openapi_resp="$(call_api GET "$BASE/openapi.json")"
    openapi_code="${openapi_resp%%$'\t'*}"
    openapi_json="${openapi_resp#*$'\t'}"
    if expect "openapi schema" "$openapi_code" 200; then
        scheme="$(printf '%s' "$openapi_json" | json_get "components.securitySchemes.RepoPilotApiKey")"
        stype="$(printf '%s' "$scheme" | json_get "type")"
        sin="$(printf '%s' "$scheme" | json_get "in")"
        sname="$(printf '%s' "$scheme" | json_get "name")"
        if [ "$stype" = "apiKey" ] && [ "$sin" = "header" ] && [ "$sname" = "X-API-Key" ]; then
            ok "openapi: RepoPilotApiKey описан как header X-API-Key"
        else
            fail "openapi: RepoPilotApiKey security scheme некорректен (type=$stype in=$sin name=$sname)"
        fi
    fi

    r="$(call_api GET "$BASE/health" "$KEY")"; expect "read_only: health" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/health" "" "" "Authorization: Bearer $KEY")"; expect "read_only: health через Bearer" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/health" "  $KEY  ")"; expect "read_only: health с пробелами" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/task/status" "$KEY")"; expect "read_only: статус задачи" "${r%%$'\t'*}" 200
    r="$(call_api POST "$BASE/file" "$KEY" '{"path":"x.txt","content":"x"}')"; expect "read_only: запись заблокирована" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"git status"}')"; expect "read_only: запуск команд заблокирован" "${r%%$'\t'*}" 403
    stop_pid
fi

# ===== AUTOPILOT =============================================================
info "AUTOPILOT / автопилот"
if start_server "autopilot" "true"; then
    r="$(call_api GET "$BASE/session" "$KEY")"; expect "autopilot: сессия" "${r%%$'\t'*}" 200

    r="$(call_api POST "$BASE/task/start" "$KEY" '{"task":"doctor task","mode":"autopilot","commitAllowed":true}')"
    expect "запуск задачи" "${r%%$'\t'*}" 200

    # Unicode-запись: кириллица "кир" в UTF-8.
    r="$(call_api POST "$BASE/file" "$KEY" '{"path":"src/a.txt","content":"doctor кир"}')"
    expect "autopilot: запись Unicode" "${r%%$'\t'*}" 200

    r="$(call_api GET "$BASE/file?path=.env" "$KEY")"; expect "секретный путь заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api GET "$BASE/file?path=../outside.txt" "$KEY")"; expect "выход за пределы репо заблокирован" "${r%%$'\t'*}" 400 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"git push"}')"; expect "сырой git push заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"git commit -m nope"}')"; expect "сырой git commit заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/file" "$KEY" '{"path":"commit2.py","content":"print(1)"}')"; expect "helper commit2.py заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"python push_and_check.py"}')"; expect "helper push_and_check.py заблокирован" "${r%%$'\t'*}" 403

    # --- POSIX-блокировки (новое для macOS/Linux порта) ---
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"sudo rm -rf /"}')"; expect "sudo заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"dd if=/dev/zero of=/dev/sda"}')"; expect "dd of=/dev заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"launchctl bootout system/x"}')"; expect "launchctl bootout заблокирован" "${r%%$'\t'*}" 403
    # Метасимволы POSIX sh в autopilot: npm .. && rm — должно блокироваться.
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"npm run build && rm -rf x"}')"; expect "autopilot: && метасимвол заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"ls; cat /etc/passwd"}')"; expect "autopilot: ; метасимвол заблокирован" "${r%%$'\t'*}" 403

    # Разрешённая команда проходит (ls — в allowlist, без метасимволов).
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"ls -la"}')"; expect "autopilot: ls разрешён" "${r%%$'\t'*}" 200

    # Большой вывод в файл. Команда должна выполняться через /bin/sh -c (POSIX)
    # или cmd.exe (Windows). Используем python без 3-суффикса — на Windows есть
    # только python, на POSIX оба варианта работают через PATH в venv.
    mkdir -p "$TMP_REPO/.promptql/runs"
    huge_cmd_body="$("$PYTHON_EXE" -c 'import json; print(json.dumps("python -c \"print(" + chr(39) + "x" + chr(39) + "*500000)\""))')"
    r="$(call_api POST "$BASE/run" "$KEY" "{\"cmd\":$huge_cmd_body,\"capture\":\"file\",\"outputFile\":\".promptql/runs/huge.txt\",\"tail\":2000}")"
    if expect "большой вывод пишется в файл" "${r%%$'\t'*}" 200; then
        out_file="$(printf '%s' "${r#*$'\t'}" | json_get "outputFile")"
        if [ -n "$out_file" ]; then ok "outputFile вернулся: $out_file"; else fail "outputFile отсутствует"; fi
    fi

    # diff / cleanup / commit.
    mkdir -p "$TMP_REPO/.gradle/cache"
    printf 'junk' > "$TMP_REPO/.gradle/cache/junk.lock"
    r="$(call_api GET "$BASE/git/diff/stat" "$KEY")"; expect "diff stat" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/git/diff/name-only" "$KEY")"; expect "diff name-only" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/git/diff/file?path=src/a.txt" "$KEY")"; expect "diff файла" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/git/changed-files" "$KEY")"; expect "изменённые файлы" "${r%%$'\t'*}" 200
    r="$(call_api POST "$BASE/git/cleanup-generated" "$KEY" '{}')"; expect "очистка generated" "${r%%$'\t'*}" 200

    # Готовим тело /git/commit через python, чтобы получить корректный JSON
    # с UTF-8 (кириллица в сообщении коммита). Включаем src/a.txt и b.txt,
    # созданные на предыдущих шагах.
    commit_body="$("$PYTHON_EXE" -c '
import json
print(json.dumps({"message":"doctor commit","include":["src/a.txt"],"cleanupGenerated":True,"runPreCommitChecks":False}, ensure_ascii=False))
')"
    commit_resp="$(call_api POST "$BASE/git/commit" "$KEY" "$commit_body")"
    if expect "endpoint commit" "${commit_resp%%$'\t'*}" 200; then
        chash="$(printf '%s' "${commit_resp#*$'\t'}" | json_get "hash")"
        if [ -n "$chash" ]; then ok "commit hash вернулся: $chash"; else fail "commit hash отсутствует"; fi
    fi

    r="$(call_api GET "$BASE/task/report" "$KEY")"; expect "отчёт задачи" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/session/report" "$KEY")"; expect "отчёт сессии" "${r%%$'\t'*}" 200
    r="$(call_api GET "$BASE/audit?tail=20" "$KEY")"; expect "audit json" "${r%%$'\t'*}" 200
    r="$(call_api POST "$BASE/task/finish" "$KEY" '{"status":"finished"}')"; expect "завершение задачи" "${r%%$'\t'*}" 200
    stop_pid
fi

# ===== FULL ==================================================================
info "FULL / полный режим"
if start_server "full" "true"; then
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"echo full-ok"}')"; expect "full: echo проходит" "${r%%$'\t'*}" 200
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"git push"}')"; expect "full: git push заблокирован" "${r%%$'\t'*}" 403
    r="$(call_api POST "$BASE/run" "$KEY" '{"cmd":"sudo ls"}')"; expect "full: sudo заблокирован" "${r%%$'\t'*}" 403
    stop_pid
fi

# --- Итог --------------------------------------------------------------------
line ""
line "============================================================"
line "RESULT"
line "Ошибок: $FAILURES"
line "Отчёт: $REPORT"
line "============================================================"

if [ "$FAILURES" = 0 ]; then
    printf '\n\033[32mRepoPilot Bridge doctor прошёл успешно.\033[0m\n'
    printf 'Отчёт: %s\n' "$REPORT"
    exit 0
else
    printf '\n\033[31mRepoPilot Bridge doctor нашёл ошибки: %s\033[0m\n' "$FAILURES"
    printf 'Отчёт: %s\n' "$REPORT"
    exit 1
fi
