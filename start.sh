#!/usr/bin/env bash
# Star For KaroX — менеджер сессий для macOS / Linux (POSIX).
# Порт start.ps1. Интерактивный TUI: выбор проекта, режим, запуск uvicorn + туннеля,
# генерация промптов, управление сессиями. Совместим с bash 3.2 (macOS по умолчанию).

set -u

# --- Определение путей -------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
    Darwin)
        CONFIG_DIR="$HOME/Library/Application Support/RepoPilotBridge"
        RUNTIME_DIR="$HOME/.local/share/RepoPilotBridge"
        ;;
    Linux|MINGW*|MSYS*|CYGWIN*)
        CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/RepoPilotBridge"
        RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/RepoPilotBridge"
        ;;
    *)
        CONFIG_DIR="$HOME/.repopilot-bridge"
        RUNTIME_DIR="$HOME/.repopilot-bridge-data"
        ;;
esac

REPOS_FILE="$CONFIG_DIR/repos.json"
SETTINGS_FILE="$CONFIG_DIR/settings.json"
LOGS_DIR="$RUNTIME_DIR/logs"
SESSIONS_DIR="$RUNTIME_DIR/sessions"
VENV_PYTHON="$RUNTIME_DIR/.venv/bin/python"

# --- Цвета и вывод -----------------------------------------------------------
C_RESET=$'\033[0m'; C_CYAN=$'\033[36m'; C_MAGENTA=$'\033[35m'; C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_DARK=$'\033[2m'
C_BOLD=$'\033[1m'

log_info()    { printf '%s%s%s\n' "$C_CYAN" "$1" "$C_RESET"; }
log_success() { printf '%s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
log_warn()    { printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
log_error()   { printf '%s%s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }

has_command() { command -v "$1" >/dev/null 2>&1; }

ask_yes() {
    local text="$1" default_yes="${2:-1}" suffix
    if [ "$default_yes" = 1 ]; then suffix="Д/н"; else suffix="д/Н"; fi
    printf '%s [%s] ' "$text" "$suffix" >&2
    local answer
    read -r answer
    [ -z "$answer" ] && return "$default_yes"
    case "$answer" in
        [ДдYy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Поиск Python и server dir ----------------------------------------------
resolve_server_dir() {
    local root="$1"
    for cand in "$root/server" "$root/../server" "$root"; do
        if [ -f "$cand/repo_tools.py" ]; then
            (cd "$cand" && pwd)
            return 0
        fi
    done
    return 1
}

SERVER_DIR="$(resolve_server_dir "$SCRIPT_DIR" || true)"
PYTHON_EXE=""
find_python_runtime() {
    # 1. venv в runtime (создаётся install.sh).
    if [ -x "$VENV_PYTHON" ]; then
        PYTHON_EXE="$VENV_PYTHON"
        return 0
    fi
    # 2. REPOPILOT_PYTHON (smoke-test контейнер / override).
    if [ -n "${REPOPILOT_PYTHON:-}" ] && [ -x "$REPOPILOT_PYTHON" ]; then
        PYTHON_EXE="$REPOPILOT_PYTHON"
        return 0
    fi
    # 3. Системный python3.
    if has_command python3; then
        PYTHON_EXE="$(command -v python3)"
        return 0
    fi
    return 1
}

ensure_installed() {
    local missing=()
    if ! find_python_runtime >/dev/null 2>&1; then
        missing+=("Python runtime / venv RepoPilot ($VENV_PYTHON)")
    fi
    if ! has_command git; then
        missing+=("Git")
    fi
    if ! has_command curl; then
        missing+=("curl (нужен для проверки готовности сервера и doctor)")
    fi
    if [ "${#missing[@]}" -gt 0 ]; then
        log_warn "Не хватает компонентов:"
        for m in "${missing[@]}"; do echo " - $m"; done
        if ask_yes "Запустить install.sh сейчас?" 1; then
            bash "$SCRIPT_DIR/install.sh"
            find_python_runtime || { log_error "Python всё ещё не найден после install.sh"; exit 1; }
            has_command curl || { log_error "curl не найден. Установите: brew install curl (macOS) / apt install curl (Linux)."; exit 1; }
        else
            log_error "Настройка отменена."
            exit 1
        fi
    fi
}

# --- JSON-хелперы (через python3, который гарантированно есть) ----------------
json_read_file() {
    # json_read_file <file> <default_json> -> печатает содержимое или default
    local file="$1" default="$2"
    if [ ! -f "$file" ]; then printf '%s' "$default"; return 0; fi
    cat "$file" 2>/dev/null || printf '%s' "$default"
}

# json_py <python-expression> — выполняет python-код с stdin=содержимое файла.
# Используется для чтения/записи JSON в repos.json/settings.json/session.json.
json_py() {
    "$PYTHON_EXE" -c "$1" 2>/dev/null
}

# --- Поиск туннельных провайдеров -------------------------------------------
find_cloudflared() {
    local -a paths=(/opt/homebrew/bin/cloudflared /usr/local/bin/cloudflared)
    paths+=("$(command -v cloudflared 2>/dev/null || true)")
    paths+=("$RUNTIME_DIR/bin/cloudflared")
    for p in "${paths[@]}"; do
        [ -n "$p" ] && [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    return 1
}

find_tailscale() {
    # macOS: CLI внутри .app; Linux: /usr/bin/tailscale или /usr/local/bin.
    local -a paths=(
        /Applications/Tailscale.app/Contents/MacOS/Tailscale
        /usr/bin/tailscale
        /usr/local/bin/tailscale
        /opt/homebrew/bin/tailscale
    )
    paths+=("$(command -v tailscale 2>/dev/null || true)")
    for p in "${paths[@]}"; do
        [ -n "$p" ] && [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    return 1
}

# --- Настройки ---------------------------------------------------------------
normalize_tunnel_provider() {
    local v="${1:-cloudflare}"
    case "$v" in
        tailscale|ts) printf 'tailscale' ;;
        *) printf 'cloudflare' ;;
    esac
}

provider_label() {
    local p; p="$(normalize_tunnel_provider "$1")"
    if [ "$p" = tailscale ]; then printf 'Tailscale Funnel'; else printf 'Cloudflare Tunnel'; fi
}

normalize_ai_client() {
    local v="${1:-other}"
    case "$v" in
        letaido) printf 'letaido' ;;
        promptql|prompt\.ql|prompt_ql) printf 'promptql' ;;
        *) printf 'promptql' ;;
    esac
}

ai_client_label() {
    local c; c="$(normalize_ai_client "$1")"
    case "$c" in
        letaido) printf 'letaido.com' ;;
        promptql) printf 'prompt.ql.app' ;;
        *) printf 'Сторонний сервис' ;;
    esac
}


normalize_language() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        ru|russian|русский) printf 'ru' ;;
        *) printf 'en' ;;
    esac
}

get_selected_language() {
    load_settings | json_py "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('language','en'))" 2>/dev/null || printf 'en'
}

select_language() {
    case "${KAROX_LANGUAGE:-}" in
        en|ru) printf '%s' "$KAROX_LANGUAGE"; return 0 ;;
    esac
    clear_screen
    printf '\n%s                         ★%s\n' "$C_YELLOW" "$C_RESET"
    printf '%s%s                 STAR FOR KAROX%s\n\n' "$C_MAGENTA" "$C_BOLD" "$C_RESET"
    echo "  Choose your language / Выберите язык"
    echo ""
    printf '%s  [1] English%s\n' "$C_CYAN" "$C_RESET"
    printf '%s  [2] Русский%s\n\n' "$C_CYAN" "$C_RESET"
    printf '  Language / Язык: ' >&2
    local choice; read -r choice
    choice="$(printf '%s' "$choice" | tr -d '[:space:]')"
    case "$choice" in 2|ru|RU) printf 'ru' ;; 1|en|EN) printf 'en' ;; *) return 1 ;; esac
}

ensure_language() {
    local has=0
    if [ -f "$SETTINGS_FILE" ]; then
        json_py "import json,sys; print(1 if 'language' in json.loads(sys.stdin.read() or '{}') else 0)" < "$SETTINGS_FILE" 2>/dev/null | grep -q 1 && has=1
    fi
    if [ "$has" = 0 ]; then
        local picked; picked="$(select_language)" || picked=en
        save_settings "" "" "$picked"
    fi
}

l() {
    local en="$1" ru="$2"
    [ "$(get_selected_language)" = ru ] && printf '%s' "$ru" || printf '%s' "$en"
}


load_settings() {
    json_read_file "$SETTINGS_FILE" '{"tunnelProvider":"cloudflare","aiClient":"promptql","language":"en"}'
}


save_settings() {
    # save_settings [tunnel] [ai_client] [language]; omitted values are preserved.
    local tunnel="${1:-}" ai_client="${2:-}" language="${3:-}" current
    current="$(load_settings)"
    [ -n "$tunnel" ] || tunnel="$(json_py "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('tunnelProvider','cloudflare'))" <<< "$current" 2>/dev/null || echo cloudflare)"
    [ -n "$ai_client" ] || ai_client="$(json_py "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('aiClient','promptql'))" <<< "$current" 2>/dev/null || echo promptql)"
    [ -n "$language" ] || language="$(json_py "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('language','en'))" <<< "$current" 2>/dev/null || echo en)"
    mkdir -p "$CONFIG_DIR"
    printf '{"tunnelProvider":"%s","aiClient":"%s","language":"%s"}' \
        "$(normalize_tunnel_provider "$tunnel")" "$(normalize_ai_client "$ai_client")" "$(normalize_language "$language")" > "$SETTINGS_FILE"
}

get_selected_tunnel_provider() {
    local s; s="$(load_settings)"
    printf '%s' "$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('tunnelProvider','cloudflare'))" <<< "$s" 2>/dev/null || echo cloudflare)"
}

get_selected_ai_client() {
    local s; s="$(load_settings)"
    printf '%s' "$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('aiClient','other'))" <<< "$s" 2>/dev/null || echo other)"
}

# --- Репозитории -------------------------------------------------------------
load_repos() {
    local raw; raw="$(json_read_file "$REPOS_FILE" '[]')"
    printf '%s' "$raw"
}

save_repo() {
    local path="$1"
    [ -d "$path" ] || return 1
    path="$(cd "$path" && pwd)"
    mkdir -p "$CONFIG_DIR"
    local raw; raw="$(load_repos)"
    # Добавляем путь, если его ещё нет, и фильтруем несуществующие.
    json_py "
import json, sys, os
path = sys.argv[1]
try:
    repos = json.loads(sys.stdin.read() or '[]')
except Exception:
    repos = []
# Оставляем только существующие пути.
repos = [r for r in repos if isinstance(r, str) and os.path.isdir(r)]
if path not in repos:
    repos.append(path)
print(json.dumps(repos, ensure_ascii=False, indent=2))
" "$path" <<< "$raw" > "$REPOS_FILE"
}

ensure_git_repo() {
    local path="$1"
    path="${path//\"/}"
    path="${path//\'/}"
    if [ ! -d "$path" ]; then
        log_error "Папка не найдена: $path"
        return 1
    fi
    path="$(cd "$path" && pwd)"
    if [ -d "$path/.git" ]; then
        printf '%s' "$path"
        return 0
    fi
    echo ""
    log_warn "В этой папке нет Git-репозитория: $path"
    echo "KaroX использует Git для diff, веток и безопасного commit."
    if ! ask_yes "Инициализировать Git в этой папке сейчас?" 1; then
        return 1
    fi
    git -C "$path" init >/dev/null 2>&1 || { log_error "git init failed"; return 1; }
    git -C "$path" config user.email "repopilot@example.local" >/dev/null 2>&1
    git -C "$path" config user.name "Star For KaroX" >/dev/null 2>&1
    git -C "$path" commit --allow-empty -m "chore: initialize repository for RepoPilot" >/dev/null 2>&1 || { log_error "empty commit failed"; return 1; }
    log_success "Git-репозиторий инициализирован."
    printf '%s' "$path"
}

get_branch() {
    git -C "$1" branch --show-current 2>/dev/null | tr -d '[:space:]'
}

# --- Управление процессами (кросс-платформенное) -----------------------------
# На Windows Git Bash kill -9 работает для MSYS PID, но uvicorn — нативный Win
# процесс, поэтому дополнительно используем taskkill /T /F для дерева.
kill_tree() {
    local pid
    for pid in "$@"; do
        [ -n "$pid" ] || continue
        [ "$pid" = "0" ] && continue
        kill -9 "$pid" 2>/dev/null || true
        if has_command taskkill; then
            taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
        fi
    done
}

proc_alive() {
    local pid="$1"
    [ -z "$pid" ] && return 1
    [ "$pid" = "0" ] && return 1
    if kill -0 "$pid" 2>/dev/null; then return 0; fi
    # На Windows Git Bash kill -0 может ложно падать для нативных PID —
    # taskkill с /T покажет, живо ли дерево.
    if has_command tasklist; then
        tasklist //FI "PID eq $pid" 2>/dev/null | grep -q "$pid" && return 0
    fi
    return 1
}

get_free_local_port() {
    # Python выделяет свободный порт через временный сокет — кросс-платформенно.
    "$PYTHON_EXE" -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"
}

# --- HTTP-вызовы к локальному API --------------------------------------------
api_get() {
    # api_get <port> <path> <api_key>
    curl -s --max-time 5 -H "X-API-Key: $3" "http://127.0.0.1:$1$2" 2>/dev/null || true
}

wait_local_api() {
    # wait_local_api <port> <api_key> <pid> <err_log>
    local port="$1" key="$2" pid="$3" err_log="$4" i
    for i in $(seq 1 40); do
        sleep 0.5
        if [ -n "$pid" ] && ! proc_alive "$pid"; then
            if [ -s "$err_log" ] && grep -qiE "error|traceback|bind|address" "$err_log" 2>/dev/null; then
                return 2
            fi
        fi
        local code
        code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 -H "X-API-Key: $key" "http://127.0.0.1:$port/health" 2>/dev/null || echo 0)"
        if [ "$code" = "200" ]; then return 0; fi
    done
    return 1
}

assert_session_isolation() {
    # assert_session_isolation <port> <key> <expected_repo> <expected_branch> <expected_mode>
    local port="$1" key="$2" expected_repo="$3" expected_branch="$4" expected_mode="$5"
    local health session
    health="$(api_get "$port" /health "$key")"
    [ -z "$health" ] && { log_error "Новая сессия не отвечает на /health"; return 1; }
    local actual_repo
    actual_repo="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('repoRoot',''))" <<< "$health" 2>/dev/null || true)"
    if [ -n "$actual_repo" ]; then
        local r1 r2
        r1="$(cd "$actual_repo" 2>/dev/null && pwd || echo "$actual_repo")"
        r2="$(cd "$expected_repo" 2>/dev/null && pwd || echo "$expected_repo")"
        if [ "$r1" != "$r2" ]; then
            log_error "Новая сессия отвечает не тем проектом. Ожидался: $expected_repo, получен: $actual_repo"
            return 1
        fi
    fi
    session="$(api_get "$port" /session "$key")"
    [ -z "$session" ] && { log_error "Новая сессия не отвечает на /session"; return 1; }
    local actual_branch actual_mode
    actual_branch="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('branch',''))" <<< "$session" 2>/dev/null || true)"
    actual_mode="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('mode',''))" <<< "$session" 2>/dev/null || true)"
    if [ -n "$actual_branch" ] && [ "$actual_branch" != "$expected_branch" ]; then
        log_error "Новая сессия отвечает не той веткой. Ожидалась: $expected_branch, получена: $actual_branch"
        return 1
    fi
    if [ -n "$actual_mode" ] && [ "$actual_mode" != "$expected_mode" ]; then
        log_error "Новая сессия отвечает не тем режимом. Ожидался: $expected_mode, получен: $actual_mode"
        return 1
    fi
    printf '{"repoRoot":"%s","branch":"%s","mode":"%s"}' "$actual_repo" "$actual_branch" "$actual_mode"
}

# --- Туннели -----------------------------------------------------------------
tunnel_url_pattern() {
    local p; p="$(normalize_tunnel_provider "$1")"
    if [ "$p" = tailscale ]; then
        printf 'https://[a-zA-Z0-9.-]+\.ts\.net(:[0-9]+)?'
    else
        printf 'https://[a-zA-Z0-9-]+\.trycloudflare\.com'
    fi
}

wait_tunnel_url() {
    # wait_tunnel_url <out_log> <err_log> <provider>
    local out_log="$1" err_log="$2" provider="$3" pattern max_wait i log
    pattern="$(tunnel_url_pattern "$provider")"
    provider="$(normalize_tunnel_provider "$provider")"
    if [ "$provider" = tailscale ]; then max_wait=35; else max_wait=80; fi
    for i in $(seq 1 "$max_wait"); do
        sleep 1
        log=""
        [ -f "$out_log" ] && log="$log$(cat "$out_log" 2>/dev/null || true)"
        [ -f "$err_log" ] && log="$log$(cat "$err_log" 2>/dev/null || true)"
        if [ -n "$log" ]; then
            local url
            url="$(printf '%s' "$log" | grep -oE "$pattern" | head -n1 || true)"
            if [ -n "$url" ]; then
                printf '%s' "$url"
                return 0
            fi
            # Tailscale: ссылка включения Funnel или "Funnel is not enabled" -> не ждём.
            if [ "$provider" = tailscale ]; then
                if printf '%s' "$log" | grep -q "https://login.tailscale.com/f/funnel"; then return 1; fi
                if printf '%s' "$log" | grep -q "Funnel is not enabled"; then return 1; fi
            fi
        fi
    done
    return 1
}

tailscale_ready() {
    local ts="$1"
    [ -x "$ts" ] || return 1
    "$ts" status --json 2>/dev/null | json_py "import json,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if d.get('BackendState')=='Running' else 1)" || return 1
}

tailscale_funnel_enable_url() {
    local text="$1"
    printf '%s' "$text" | grep -oE "https://login\.tailscale\.com/f/funnel\?[^\s\"']+" | head -n1 | tr -d '.,;)' || true
}

start_tunnel() {
    # start_tunnel <provider> <local_port> <out_log> <err_log> -> печатает PID
    local provider="$1" local_port="$2" out_log="$3" err_log="$4" exe
    provider="$(normalize_tunnel_provider "$provider")"
    if [ "$provider" = tailscale ]; then
        exe="$(find_tailscale)" || { log_error "tailscale не найден"; return 1; }
        nohup "$exe" funnel --yes "http://127.0.0.1:$local_port" >"$out_log" 2>"$err_log" &
        printf '%s' "$!"
    else
        exe="$(find_cloudflared)" || { log_error "cloudflared не найден"; return 1; }
        nohup "$exe" tunnel --url "http://localhost:$local_port" >"$out_log" 2>"$err_log" &
        printf '%s' "$!"
    fi
}

normalize_provider_id_part() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | tr -s '-' | sed 's/^-*//;s/-*$//'
}

get_provider_id_from_url() {
    local url="$1" provider="$2" session_id="$3" host provider_suffix session_suffix
    provider="$(normalize_tunnel_provider "$provider")"
    host="$(printf '%s' "$url" | sed -E 's#https?://([^/:]+).*#\1#')"
    if [ "$provider" = cloudflare ]; then
        host="${host%.trycloudflare.com}"
    fi
    provider_suffix="$(normalize_provider_id_part "$host")"
    if [ "$provider" = tailscale ] && [ -n "$session_id" ]; then
        session_suffix="$(normalize_provider_id_part "$session_id")"
        if [ -n "$session_suffix" ]; then
            provider_suffix="${provider_suffix}-${session_suffix}"
        fi
    fi
    printf 'repo-tools-%s' "$provider_suffix"
}

# --- Сессии ------------------------------------------------------------------
session_status() {
    # session_status <server_pid> <tunnel_pid>
    local server_alive tunnel_alive
    proc_alive "$1" 2>/dev/null && server_alive=1 || server_alive=0
    proc_alive "$2" 2>/dev/null && tunnel_alive=1 || tunnel_alive=0
    if [ "$server_alive" = 1 ] && [ "$tunnel_alive" = 1 ]; then printf 'running'
    elif [ "$server_alive" = 1 ] || [ "$tunnel_alive" = 1 ]; then printf 'partial'
    else printf 'stopped'; fi
}

# get_sessions — печатает JSON-массив сессий с актуальным статусом.
get_sessions() {
    [ -d "$SESSIONS_DIR" ] || { printf '[]'; return 0; }
    local dirs=()
    local d
    for d in "$SESSIONS_DIR"/*/; do
        [ -d "$d" ] || continue
        [ -f "$d/session.json" ] || continue
        dirs+=("${d%/}")
    done
    if [ "${#dirs[@]}" = 0 ]; then printf '[]'; return 0; fi
    # Передаём список директорий в python, он читает session.json и считает статус.
    {
        printf '%s\n' "${dirs[@]}" | "$PYTHON_EXE" -c '
import json, sys, os, subprocess

def proc_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        pass
    # Windows fallback
    if os.name == "nt":
        try:
            subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                            capture_output=True, text=True, timeout=5)
            # если процесс существует, tasklist вернёт его в выводе
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=5)
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    return False

items = []
for line in sys.stdin:
    d = line.strip()
    if not d:
        continue
    try:
        with open(os.path.join(d, "session.json"), "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        continue
    server_alive = proc_alive(s.get("serverPid"))
    tunnel_alive = proc_alive(s.get("tunnelPid"))
    if server_alive and tunnel_alive:
        status = "running"
    elif server_alive or tunnel_alive:
        status = "partial"
    else:
        status = "stopped"
    s["status"] = status
    s["sessionDir"] = s.get("sessionDir") or d
    items.append(s)
items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
print(json.dumps(items, ensure_ascii=False))
'
    }
}

save_session_json() {
    local session_dir="$1"
    shift
    mkdir -p "$session_dir"
    # Аргументы: ключ=значение пары.
    "$PYTHON_EXE" -c '
import json, sys
pairs = sys.argv[1:]
obj = {}
for p in pairs:
    if "=" in p:
        k, v = p.split("=", 1)
        # bool
        if v in ("true","false"):
            obj[k] = (v == "true")
        elif v.isdigit():
            obj[k] = int(v)
        else:
            obj[k] = v
print(json.dumps(obj, ensure_ascii=False, indent=2))
' "$@" > "$session_dir/session.json"
}

load_session_json() {
    [ -f "$1/session.json" ] || { printf '{}'; return 1; }
    cat "$1/session.json"
}

write_session_files() {
    local session_dir="$1" connect_prompt="$2" task_prompt="$3" api_key="$4" provider_id="$5" session_info="$6"
    printf '%s' "$connect_prompt" > "$session_dir/connect-prompt.txt"
    printf '%s' "$task_prompt" > "$session_dir/task-prompt.txt"
    printf '%s' "$api_key" > "$session_dir/api-key.txt"
    printf '%s' "$provider_id" > "$session_dir/provider-id.txt"
    printf '%s' "$session_info" > "$session_dir/session-info.txt"
    {
        printf 'ПОДСКАЗКА ПОДКЛЮЧЕНИЯ:\n%s\n\nШАБЛОН ЗАДАЧИ:\n%s\n\nX-API-KEY:\n%s\n\nPROVIDER ID:\n%s\n' \
            "$connect_prompt" "$task_prompt" "$api_key" "$provider_id"
    } > "$session_dir/all.txt"
}

# --- Буфер обмена / открытие браузера ---------------------------------------
copy_to_clipboard() {
    if has_command pbcopy; then pbcopy
    elif has_command clip.exe; then clip.exe
    elif has_command xclip; then xclip -selection clipboard
    elif has_command xsel; then xsel --clipboard --input
    else cat >/dev/null; return 1; fi
}

open_url() {
    if [ "$(uname -s)" = Darwin ]; then open "$1" 2>/dev/null || true
    elif has_command xdg-open; then xdg-open "$1" 2>/dev/null || true
    elif has_command start; then start "$1" 2>/dev/null || true
    fi
}

# --- UI: заголовки и меню ----------------------------------------------------
clear_screen() {
    # На Windows Git Bash clear работает; на macOS тоже.
    clear 2>/dev/null || true
}


show_karox_intro() {
    clear_screen
    if [ -t 1 ] && [ "${KAROX_NO_ANIMATION:-0}" != "1" ]; then
        local frame
        for frame in "  ★" "       ★" "            ★" "                 ★" "                      ★" "                           ★" "                                ★" "                                     ★" "                                          ★"; do
            printf '\r%s%-62s%s' "$C_YELLOW" "$frame" "$C_RESET"; sleep 0.055
        done
        printf '\n%s                                          │%s\n' "$C_DARK" "$C_RESET"; sleep 0.08
        printf '%s                                          ▼%s\n' "$C_YELLOW" "$C_RESET"; sleep 0.12
    fi
    printf '\n%s                         ★%s\n' "$C_YELLOW" "$C_RESET"
    printf '%s%s                 STAR FOR KAROX%s\n' "$C_MAGENTA" "$C_BOLD" "$C_RESET"
    printf '%s              %s%s\n\n' "$C_DARK" "$(l "local code, guided safely" "локальный код под безопасным управлением")" "$C_RESET"
    if [ -t 1 ] && [ "${KAROX_NO_ANIMATION:-0}" != "1" ]; then sleep 0.45; fi
}

ui_width() {
    local w=72
    if [ -t 1 ] && command -v tput >/dev/null 2>&1; then w="$(tput cols 2>/dev/null || echo 72)"; fi
    [ "$w" -ge 52 ] || w=52
    [ "$w" -le 88 ] || w=88
    printf '%s' "$w"
}

ui_repeat() {
    local char="$1" n="$2" out=""
    while [ "${#out}" -lt "$n" ]; do out="${out}${char}"; done
    printf '%s' "${out:0:$n}"
}

ui_fit() {
    local value="$1" max="$2"
    if [ "${#value}" -le "$max" ]; then printf '%s' "$value"
    elif [ "$max" -gt 1 ]; then printf '%s…' "${value:0:$((max-1))}"
    fi
}

ui_wrap() {
    local text="$1" color="${2:-$C_RESET}" indent="${3:-2}" max line="" word
    max=$(( $(ui_width) - indent - 2 )); [ "$max" -ge 20 ] || max=20
    for word in $text; do
        if [ -n "$line" ] && [ $(( ${#line}+${#word}+1 )) -gt "$max" ]; then
            printf '%s%*s%s%s\n' "$color" "$indent" "" "$line" "$C_RESET"; line="$word"
        else line="${line:+$line }$word"; fi
    done
    [ -z "$line" ] || printf '%s%*s%s%s\n' "$color" "$indent" "" "$line" "$C_RESET"
}

ui_badge() { printf '%s[%s]%s' "$2" "$1" "$C_RESET"; }

ui_choice() {
    local key="$1" title="$2" detail="$3" color="$4"
    printf '%s  │  [%s] %s%s%s  %s%s\n' "$color" "$key" "$title" "$C_RESET" "$C_DARK" "$(ui_fit "$detail" $(( $(ui_width)-${#title}-${#key}-12 )))" "$C_RESET"
}

ui_notice() {
    local kind="$1" title="$2" detail="$3" symbol="•" color="$C_CYAN"
    case "$kind" in success) symbol="✓"; color="$C_GREEN" ;; warn) symbol="!"; color="$C_YELLOW" ;; error) symbol="×"; color="$C_RED" ;; progress) symbol="◌"; color="$C_MAGENTA" ;; esac
    printf '%s  %s %s%s\n' "$color" "$symbol" "$title" "$C_RESET"
    [ -z "$detail" ] || ui_wrap "$detail" "$C_DARK" 4
}

ui_empty() {
    printf '%s  ╭─ ◇ %s%s\n' "$C_MAGENTA" "$2" "$C_RESET"
    ui_wrap "$3" "$C_DARK" 5
    printf '%s  ╰────────────────%s\n' "$C_DARK" "$C_RESET"
}

ui_line() {
    local char="${1:-─}" width="${2:-0}" line=""
    [ "$width" -gt 0 ] 2>/dev/null || width=$(( $(ui_width) - 4 ))
    [ "$width" -ge 8 ] || width=8
    while [ "${#line}" -lt "$width" ]; do line="${line}${char}"; done
    printf '%s%s%s\n' "$C_DARK" "${line:0:$width}" "$C_RESET"
}

ui_section() {
    local width label fill
    width="$(ui_width)"; label=" $(printf '%s' "$1" | tr '[:lower:]' '[:upper:]') "
    fill=$((width - ${#label} - 5)); [ "$fill" -ge 2 ] || fill=2
    echo ""
    printf '%s  ┌─%s%s%s' "$C_MAGENTA" "$C_RESET" "$label" "$C_DARK"
    ui_repeat "─" "$fill"
    printf '%s\n' "$C_RESET"
}

ui_kv() {
    local label="$1" value="$2" width
    width="$(ui_width)"
    if [ $(( ${#label} + ${#value} + 21 )) -le "$width" ]; then
        printf '%s  │  %-16s%s%s\n' "$C_DARK" "$label" "$C_RESET" "$value"
    else
        printf '%s  │  %s%s\n%s  │    %s%s\n' "$C_DARK" "$label" "$C_RESET" "$C_DARK" "$(ui_fit "$value" $((width-8)))" "$C_RESET"
    fi
}

status_label() {
    case "$1" in running) printf '● LIVE' ;; partial) printf '◐ DEGRADED' ;; *) printf '○ OFFLINE' ;; esac
}

header() {
    clear_screen
    local width; width="$(ui_width)"
    echo ""
    printf '%s  ◆ %s%sKAROX%s%s  /  PROJECT FLIGHT DECK%s\n' "$C_MAGENTA" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_DARK" "$C_RESET"
    printf '%s  ' "$C_MAGENTA"; ui_repeat "━" $((width-2)); printf '%s\n' "$C_RESET"
    if [ -n "${1:-}" ]; then
        printf '  %s\n%s  %s%s\n' "$(ui_fit "$1" $((width-2)))" "$C_DARK" "$(l "local code · explicit control · safe AI handoff" "локальный код · явный контроль · безопасная передача AI")" "$C_RESET"
    fi
    echo ""
}

repo_label() {
    printf '%s' "${1##*/}"
}

# --- Запуск новой сессии -----------------------------------------------------

select_mode() {
    header "$(l "Create workspace session" "Создание рабочей сессии")"
    ui_notice info "$(l "Choose the smallest access profile that can finish the task." "Выберите минимальный профиль, достаточный для задачи.")" "$(l "You can open a new session later with more access." "Позже можно открыть новую сессию с большим доступом.")"
    ui_section "$(l "Access profiles" "Профили доступа")"
    ui_choice 1 OBSERVE "$(l "Read-only exploration and repository context" "Анализ и контекст без изменений")" "$C_CYAN"
    ui_choice 2 BUILD "$(l "Isolated branch, edits, checks and safe commit" "Отдельная ветка, правки, проверки и безопасный commit")" "$C_MAGENTA"
    ui_choice 3 RESUME "$(l "Continue the current workspace branch" "Продолжить текущую рабочую ветку")" "$C_GREEN"
    ui_choice 4 ADVANCED "$(l "Extended repository commands" "Расширенные команды репозитория")" "$C_YELLOW"
    printf '\n  › %s: ' "$(l "Profile" "Профиль")" >&2
    local choice; read -r choice
    case "$choice" in 1) printf read_only ;; 2) printf autopilot ;; 3) printf autopilot-continue ;; 4) printf full ;; *) return 1 ;; esac
}

select_ai_client() {
    header "$(l "Connection target" "Куда подключить")"
    ui_notice info "$(l "Choose where this local workspace should appear." "Выберите AI-клиент для рабочего пространства.")" ""
    ui_section "$(l "AI targets" "AI-клиенты")"
    ui_choice 1 PROMPTQL "$(l "Native shared AI workspace · recommended" "Нативная командная AI-среда · рекомендуется")" "$C_MAGENTA"
    ui_choice 2 "$(l "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ")" "$(l "Generic OpenAPI connection" "Универсальное OpenAPI-подключение")" "$C_CYAN"
    ui_choice 3 LETAIDO.COM "$(l "Compatibility mode" "Режим совместимости")" "$C_DARK"
    printf '\n  › %s: ' "$(l "Target" "Клиент")" >&2
    local choice; read -r choice
    case "$choice" in 1) printf promptql ;; 2) printf other ;; 3) printf letaido ;; *) return 1 ;; esac
}

ensure_ai_client() {
    # Спрашиваем только если файла настроек нет или в нём нет поля aiClient.
    # Иначе — выбор уже сделан (даже если значение other).
    local has_field=0
    if [ -f "$SETTINGS_FILE" ]; then
        json_py "import json,sys
d=json.loads(sys.stdin.read() or '{}')
print(1 if 'aiClient' in d else 0)" < "$SETTINGS_FILE" 2>/dev/null | grep -q 1 && has_field=1
    fi
    if [ "$has_field" = 0 ]; then
        local picked
        picked="$(select_ai_client)" || return 1
        save_settings "" "$picked"
    fi
}

select_repo() {
    local repos_raw repos_count choice typed
    repos_raw="$(load_repos)"
    repos_count="$(json_py "import json,sys; print(len(json.loads(sys.stdin.read() or '[]')))" <<< "$repos_raw" 2>/dev/null || echo 0)"
    header "$(l "Choose repository" "Выберите репозиторий")"
    ui_notice info "$(l "Choose a recent repository or paste a full Git path." "Выберите недавний репозиторий или вставьте полный Git-путь.")" "$(l "KaroX validates it before creating a session." "KaroX проверит его до создания сессии.")"
    ui_section "$(l "Recent repositories" "Недавние репозитории")"
    if [ "$repos_count" -eq 0 ]; then
        ui_empty "$(l "No pinned repositories" "Нет закреплённых репозиториев")" "$(l "Paste a full path below to add the first one." "Вставьте полный путь ниже, чтобы добавить первый.")"
    else
        REPOS_JSON="$repos_raw" "$PYTHON_EXE" - <<'PY'
import json,os
for i,path in enumerate(json.loads(os.environ["REPOS_JSON"]),1):
    name=os.path.basename(path.rstrip("/\\")) or path
    print(f"  │  [{i}] {name}  {path}")
PY
    fi
    ui_section "$(l "Connect" "Подключить")"
    ui_choice N "$(l "NEW PATH" "НОВЫЙ ПУТЬ")" "$(l "Enter a full local Git repository path" "Введите полный путь к локальному Git-репозиторию")" "$C_GREEN"
    printf '%s  │  %s%s\n\n' "$C_DARK" "$(l "Tip: paste a full path directly at the prompt." "Совет: полный путь можно вставить прямо в строку выбора.")" "$C_RESET"
    printf '  › %s: ' "$(l "Repository" "Репозиторий")" >&2
    read -r choice; typed="${choice//\"/}"
    case "$choice" in
        [Nn])
            printf '  › %s: ' "$(l "Full path" "Полный путь")" >&2
            local path; read -r path
            path="${path//\"/}"; path="$(ensure_git_repo "$path")" || return 1
            save_repo "$path"; printf '%s' "$path"
            ;;
        *)
            if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$repos_count" ]; then
                local repo
                repo="$(json_py "import json,sys; x=json.loads(sys.stdin.read() or '[]'); print(x[$choice-1])" <<< "$repos_raw" 2>/dev/null || true)"
                [ -n "$repo" ] && [ -d "$repo" ] || return 1
                save_repo "$repo"; printf '%s' "$repo"; return 0
            fi
            if [ -n "$typed" ] && [ -d "$typed" ]; then
                local repo; repo="$(ensure_git_repo "$typed")" || return 1
                save_repo "$repo"; printf '%s' "$repo"; return 0
            fi
            log_error "$(l "Enter a number, N, or a full repository path." "Введите номер, N или полный путь к репозиторию.")"
            return 1
            ;;
    esac
}

new_branch() {
    local repo="$1" prefix="$2" stamp branch
    stamp="$(date +%Y%m%d-%H%M%S)"
    branch="promptql/${prefix}-${stamp}"
    git -C "$repo" switch -c "$branch" >/dev/null 2>&1 || { log_error "Не удалось создать ветку $branch"; return 1; }
    printf '%s' "$branch"
}


build_prompts() {
    local provider_id="$1" tunnel_url="$2" mode="$3" branch="$4"
    local session_title="$5" commit_allowed="$6" provider="$7" ai_client="$8"
    ai_client="$(normalize_ai_client "$ai_client")"
    local language provider_name intro session_block preflight rules connect_prompt task_prompt
    language="$(get_selected_language)"
    provider_name="$(provider_label "$provider")"

    if [ "$language" = ru ]; then
        session_block="Сессия:
- repoRoot: будет подтверждён через /session
- режим: $mode
- ветка: $branch
- название сессии: $session_title
- provider id: $provider_id
- commitAllowed: $commit_allowed
- pushAllowed: false

Название сессии — только метка истории, не задача. После подключения дождись отдельного реального ТЗ пользователя."
        preflight="Перед любой работой выполни preflight:
1. GET /session
2. GET /health
3. GET /git/status
4. GET /context/brief
5. Сверь repoRoot, branch, mode, commitAllowed и pushAllowed с этой карточкой.
Если что-либо не совпадает — остановись и сообщи пользователю."
        rules="Правила:
- большой вывод сохраняй через capture=file;
- не создавай helper-скрипты для commit/push/check;
- перед commit вызывай /git/cleanup-generated;
- commit выполняй только через /git/commit;
- никогда не выполняй git push;
- ключ X-API-Key не проси отправлять в чат."
        case "$ai_client" in
            promptql)
                intro="Я запустил Star For KaroX и хочу подключить эту локальную сессию к PromptQL.

Создай отдельную personal custom API integration:
- provider id: $provider_id
- protocol: api
- name: KaroX
- base_url: $tunnel_url
- api_docs_url: $tunnel_url/openapi.json
- credential type: api_key
- credential header: X-API-Key
- prefix: пустой
- туннель: $provider_name

Не переиспользуй интеграцию другой активной сессии. Покажи защищённую карточку подключения; в неё пользователь вставит ключ, скопированный клавишей K." ;;
            letaido)
                intro="Я запустил Star For KaroX для letaido.com. Разреши домен $tunnel_url и запроси защищённый header secret X-API-Key, затем обращайся к URL напрямую." ;;
            *)
                intro="Я запустил Star For KaroX.
- base_url: $tunnel_url
- OpenAPI: $tunnel_url/openapi.json
- auth: header X-API-Key
- туннель: $provider_name
Ключ вводится только через защищённое хранилище AI-клиента." ;;
        esac
        task_prompt="Реальное ТЗ для KaroX:

<ВСТАВЬТЕ СЮДА ЗАДАЧУ>

Контекст: branch=$branch; mode=$mode; commitAllowed=$commit_allowed; pushAllowed=false.
Сначала вызови /session, /health и /git/status. Перед commit вызови /git/cleanup-generated; commit делай только через /git/commit. Никогда не делай push.
В конце покажи проверки, commit hash, git status и краткий отчёт."
    else
        session_block="Session:
- repoRoot: must be confirmed via /session
- mode: $mode
- branch: $branch
- session label: $session_title
- provider id: $provider_id
- commitAllowed: $commit_allowed
- pushAllowed: false

The session label is history metadata, not a task. Wait for a separate real user instruction."
        preflight="Before repository work:
1. GET /session
2. GET /health
3. GET /git/status
4. GET /context/brief
5. Match repoRoot, branch, mode, commitAllowed, and pushAllowed.
Stop and report any mismatch."
        rules="Rules:
- use capture=file for large output;
- do not create helper scripts for commit/push/check;
- call /git/cleanup-generated before committing;
- commit only through /git/commit;
- never run git push;
- never ask for X-API-Key in chat."
        case "$ai_client" in
            promptql)
                intro="I started Star For KaroX and want to connect this local session to PromptQL.

Create a separate personal custom API integration:
- provider id: $provider_id
- protocol: api
- name: KaroX
- base_url: $tunnel_url
- api_docs_url: $tunnel_url/openapi.json
- credential type: api_key
- credential header: X-API-Key
- prefix: empty
- tunnel: $provider_name

Do not reuse another active session's integration. Show a protected connection card for the key copied with K." ;;
            letaido)
                intro="I started Star For KaroX for letaido.com. Allow $tunnel_url and request a protected X-API-Key header secret, then call the URL directly." ;;
            *)
                intro="I started Star For KaroX.
- base_url: $tunnel_url
- OpenAPI: $tunnel_url/openapi.json
- auth: X-API-Key header
- tunnel: $provider_name
Store the key only in the AI client's protected credential store." ;;
        esac
        task_prompt="Real task for KaroX:

<INSERT THE USER'S TASK HERE>

Context: branch=$branch; mode=$mode; commitAllowed=$commit_allowed; pushAllowed=false.
First call /session, /health, and /git/status. Before committing call /git/cleanup-generated; commit only through /git/commit. Never push.
At the end report checks, commit hash, git status, and a concise summary."
    fi

    connect_prompt="${intro}

${session_block}

${preflight}

${rules}"
    printf '%s\t%s' "$connect_prompt" "$task_prompt"
}

gen_api_key() {
    "$PYTHON_EXE" -c "import uuid; print(uuid.uuid4().hex + uuid.uuid4().hex)"
}

gen_session_id() {
    printf '%s-%s' "$(date +%Y%m%d-%H%M%S)" "$("$PYTHON_EXE" -c "import uuid; print(uuid.uuid4().hex[:6])")"
}

start_new_session() {
    local mode_choice repo session_title
    mode_choice="$(select_mode)" || { log_error "Неверный режим"; return 1; }
    repo="$(select_repo)" || return 1

    printf '%s: ' "$(l "Session name (history label, not an AI task)" "Название сессии (метка истории, не ТЗ для AI)")" >&2
    read -r session_title
    [ -n "$session_title" ] || session_title="$(l "KaroX session" "Сессия KaroX")"

    local mode="read_only" commit_allowed="false" branch branch_prefix=""
    branch="$(get_branch "$repo")"
    case "$mode_choice" in
        read_only) mode="read_only"; commit_allowed="false" ;;
        autopilot) mode="autopilot"; commit_allowed="true"; branch_prefix="autopilot" ;;
        autopilot-continue)
            mode="autopilot"; commit_allowed="true"
            case "$branch" in
                promptql/*) ;;
                *)
                    log_warn "Текущая ветка не promptql/*: $branch"
                    if ! ask_yes "Продолжить на этой ветке?" 0; then return 1; fi
                    ;;
            esac
            ;;
        full) mode="full"; commit_allowed="true"; branch_prefix="full" ;;
    esac

    # Проверка параллельных сессий на том же репо.
    if [ "$mode" != "read_only" ]; then
        local same_count
        same_count="$(get_sessions | "$PYTHON_EXE" -c "
import json,sys,os
items=json.loads(sys.stdin.read() or '[]')
repo=sys.argv[1]
target=os.path.realpath(repo)
n=sum(1 for s in items if s.get('status')=='running' and os.path.realpath(s.get('repo',''))==target)
print(n)" "$repo" 2>/dev/null || echo 0)"
        if [ "$same_count" -gt 0 ]; then
            echo ""
            log_warn "Этот путь проекта уже открыт в другой активной сессии."
            log_warn "Для двух параллельных задач в одном проекте нужен отдельный clone/worktree."
            if ! ask_yes "Всё равно продолжить на этом же пути?" 0; then return 1; fi
        fi
    fi

    if [ -n "$branch_prefix" ]; then
        branch="$(new_branch "$repo" "$branch_prefix")" || return 1
    fi

    local api_key tunnel_provider tunnel_log_stem session_id session_dir ai_client
    local session_logs_dir runs_dir server_out_log server_err_log server_log
    local tunnel_out_log tunnel_err_log local_port
    api_key="$(gen_api_key)"
    tunnel_provider="$(get_selected_tunnel_provider)"
    ai_client="$(get_selected_ai_client)"
    if [ "$tunnel_provider" = tailscale ]; then tunnel_log_stem="tailscale"; else tunnel_log_stem="cloudflared"; fi
    session_id="$(gen_session_id)"
    session_dir="$SESSIONS_DIR/$session_id"
    session_logs_dir="$session_dir/logs"
    runs_dir="$session_dir/runs"
    server_out_log="$session_logs_dir/uvicorn.out.log"
    server_err_log="$session_logs_dir/uvicorn.err.log"
    server_log="$session_logs_dir/repo-tools.jsonl"
    tunnel_out_log="$session_logs_dir/${tunnel_log_stem}.out.log"
    tunnel_err_log="$session_logs_dir/${tunnel_log_stem}.err.log"
    local_port="$(get_free_local_port)"

    mkdir -p "$session_dir" "$session_logs_dir" "$runs_dir"
    rm -f "$server_out_log" "$server_err_log" "$server_log" "$tunnel_out_log" "$tunnel_err_log" 2>/dev/null || true

    header "$(l "Provisioning local workspace" "Запуск локального рабочего пространства")"
    echo "Проект: $repo"
    echo "Режим : $mode"
    echo "Туннель: $(provider_label "$tunnel_provider")"
    echo "Порт  : $local_port"
    echo ""

    log_info "  ◌ Starting secure local API..."
    # Запускаем uvicorn в фоне через nohup; PID — это PID процесса python.
    export REPO_ROOT="$repo"
    export REPO_ROOT_B64="$("$PYTHON_EXE" -c "import base64; print(base64.b64encode('$repo'.encode('utf-8')).decode())")"
    export REPO_TOOLS_API_KEY="$api_key"
    export REPO_TOOLS_MODE="$mode"
    export REPO_TOOLS_BRANCH="$branch"
    export REPO_TOOLS_SESSION_TITLE="$session_title"
    export REPO_TOOLS_SESSION_TITLE_B64="$("$PYTHON_EXE" -c "import base64; print(base64.b64encode('$session_title'.encode('utf-8')).decode())")"
    export REPO_TOOLS_INITIAL_TASK=""
    export REPO_TOOLS_INITIAL_TASK_B64=""
    export REPO_TOOLS_COMMIT_ALLOWED="$commit_allowed"
    export REPO_TOOLS_HOME="$RUNTIME_DIR"
    export REPO_TOOLS_LOG_FILE="$server_log"
    export REPO_TOOLS_RUNS_DIR="$runs_dir"

    (cd "$SERVER_DIR" && nohup "$PYTHON_EXE" -m uvicorn --app-dir "$SERVER_DIR" \
        repo_tools:app --host 127.0.0.1 --port "$local_port" \
        >"$server_out_log" 2>"$server_err_log" & echo $! > "$session_dir/server.pid") >/dev/null 2>&1
    local server_pid
    server_pid="$(cat "$session_dir/server.pid" 2>/dev/null || true)"

    if ! wait_local_api "$local_port" "$api_key" "$server_pid" "$server_err_log"; then
        kill_tree "$server_pid" 2>/dev/null || true
        local tail_text=""
        [ -f "$server_err_log" ] && tail_text="$(tail -n 30 "$server_err_log" 2>/dev/null || true)"
        [ -f "$server_out_log" ] && tail_text="$tail_text$(tail -n 30 "$server_out_log" 2>/dev/null || true)"
        [ -z "$tail_text" ] && tail_text="Логи пустые."
        log_error "Локальный API не ответил за 20 секунд."
        echo "$tail_text"
        return 1
    fi

    local isolation
    isolation="$(assert_session_isolation "$local_port" "$api_key" "$repo" "$branch" "$mode")" || {
        kill_tree "$server_pid" 2>/dev/null || true
        return 1
    }

    # Проверка готовности туннеля.
    local tunnel_ok=0
    if [ "$tunnel_provider" = tailscale ]; then
        local ts_exe
        ts_exe="$(find_tailscale 2>/dev/null || true)"
        if [ -n "$ts_exe" ] && tailscale_ready "$ts_exe"; then
            tunnel_ok=1
        fi
        if [ "$tunnel_ok" = 0 ]; then
            echo ""
            log_warn "$(provider_label tailscale) не готов."
            if ask_yes "Запустить tailscale up сейчас?" 1; then
                "$ts_exe" up 2>&1 || true
                if [ -n "$ts_exe" ] && tailscale_ready "$ts_exe"; then tunnel_ok=1; fi
            fi
        fi
    else
        if find_cloudflared >/dev/null 2>&1; then tunnel_ok=1; fi
    fi
    if [ "$tunnel_ok" = 0 ]; then
        kill_tree "$server_pid" 2>/dev/null || true
        log_error "$(provider_label "$tunnel_provider") не готов."
        return 1
    fi

    log_info "  ◌ Opening $(provider_label "$tunnel_provider")..."
    local tunnel_pid
    tunnel_pid="$(start_tunnel "$tunnel_provider" "$local_port" "$tunnel_out_log" "$tunnel_err_log")" || {
        kill_tree "$server_pid" 2>/dev/null || true
        return 1
    }

    local tunnel_url
    tunnel_url="$(wait_tunnel_url "$tunnel_out_log" "$tunnel_err_log" "$tunnel_provider")" || {
        kill_tree "$tunnel_pid" "$server_pid" 2>/dev/null || true
        local tail_text=""
        [ -f "$tunnel_out_log" ] && tail_text="$(tail -n 30 "$tunnel_out_log" 2>/dev/null || true)"
        [ -f "$tunnel_err_log" ] && tail_text="$tail_text$(tail -n 30 "$tunnel_err_log" 2>/dev/null || true)"
        [ -z "$tail_text" ] && tail_text="Логи пустые."
        if [ "$tunnel_provider" = tailscale ]; then
            local enable_url
            enable_url="$(tailscale_funnel_enable_url "$tail_text")"
            if [ -n "$enable_url" ]; then
                echo ""
                log_warn "Tailscale просит включить Funnel для этого tailnet."
                log_info "Ссылка включения: $enable_url"
                printf '%s' "$enable_url" | copy_to_clipboard >/dev/null 2>&1 || true
                log_success "Ссылка скопирована в буфер обмена."
                open_url "$enable_url"
                log_warn "Подтвердите Funnel в Tailscale и запустите сессию ещё раз."
            else
                log_warn "Откройте G = настройки, выберите Tailscale Funnel и нажмите L для tailscale up."
            fi
        fi
        log_error "Не удалось получить URL туннеля. Логи: $tunnel_out_log / $tunnel_err_log"
        echo "$tail_text"
        return 1
    }

    local provider_id prompts
    provider_id="$(get_provider_id_from_url "$tunnel_url" "$tunnel_provider" "$session_id")"
    prompts="$(build_prompts "$provider_id" "$tunnel_url" "$mode" "$branch" "$session_title" "$commit_allowed" "$tunnel_provider" "$ai_client")"
    local connect_prompt task_prompt
    connect_prompt="${prompts%%$'\t'*}"
    task_prompt="${prompts#*$'\t'}"

    local session_info
    session_info="Репозиторий : $repo
Режим       : $mode
Ветка       : $branch
Сессия      : $session_title
Session ID  : $session_id
Локальный API: http://127.0.0.1:$local_port
Туннель     : $(provider_label "$tunnel_provider")
URL туннеля : $tunnel_url
AI-клиент   : $(ai_client_label "$ai_client")
Provider ID : $provider_id"
    write_session_files "$session_dir" "$connect_prompt" "$task_prompt" "$api_key" "$provider_id" "$session_info"

    save_session_json "$session_dir" \
        "id=$session_id" \
        "repo=$repo" \
        "mode=$mode" \
        "branch=$branch" \
        "title=$session_title" \
        "startedAt=$(date '+%Y-%m-%d %H:%M:%S')" \
        "localPort=$local_port" \
        "tunnelProvider=$tunnel_provider" \
        "aiClient=$ai_client" \
        "tunnelUrl=$tunnel_url" \
        "providerId=$provider_id" \
        "apiKey=$api_key" \
        "commitAllowed=$commit_allowed" \
        "sessionDir=$session_dir" \
        "serverPid=$server_pid" \
        "tunnelPid=$tunnel_pid" \
        "serverOutLog=$server_out_log" \
        "serverErrLog=$server_err_log" \
        "tunnelOutLog=$tunnel_out_log" \
        "tunnelErrLog=$tunnel_err_log"

    echo ""
    log_success "  ● Workspace is live"
    echo "URL туннеля : $tunnel_url"
    echo "Provider ID : $provider_id"
    echo "Папка сессии: $session_dir"
    # Возвращаем session_dir для вызывающего меню.
    printf '%s' "$session_dir"
}

# --- Меню сессии -------------------------------------------------------------
copy_session_file() {
    local session_dir="$1" file_name="$2" message="$3"
    local path="$session_dir/$file_name"
    if [ ! -f "$path" ]; then
        log_warn "Файл не найден: $path"
        return
    fi
    if cat "$path" | copy_to_clipboard >/dev/null 2>&1; then
        log_success "$message"
    else
        log_warn "Буфер обмена недоступен. Файл: $path"
        echo "---"
        cat "$path"
    fi
}

show_log_tail() {
    local session_json="$1"
    header "Логи сессии"
    local server_out server_err tunnel_out tunnel_err
    server_out="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('serverOutLog',''))" <<< "$session_json" 2>/dev/null || true)"
    server_err="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('serverErrLog',''))" <<< "$session_json" 2>/dev/null || true)"
    tunnel_out="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('tunnelOutLog',''))" <<< "$session_json" 2>/dev/null || true)"
    tunnel_err="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('tunnelErrLog',''))" <<< "$session_json" 2>/dev/null || true)"
    for p in "$server_out" "$server_err" "$tunnel_out" "$tunnel_err"; do
        if [ -f "$p" ]; then
            printf '%s---- %s%s\n' "$C_DARK" "$p" "$C_RESET"
            tail -n 20 "$p" 2>/dev/null || true
            echo ""
        fi
    done
    printf 'Enter для возврата: ' >&2
    read -r _
}

stop_session_by_json() {
    local session_json="$1"
    local server_pid tunnel_pid
    server_pid="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('serverPid',0))" <<< "$session_json" 2>/dev/null || echo 0)"
    tunnel_pid="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('tunnelPid',0))" <<< "$session_json" 2>/dev/null || echo 0)"
    kill_tree "$tunnel_pid" "$server_pid" 2>/dev/null || true
}

remove_session_history() {
    local session_json="$1"
    local session_dir server_pid tunnel_pid status
    session_dir="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('sessionDir',''))" <<< "$session_json" 2>/dev/null || true)"
    server_pid="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('serverPid',0))" <<< "$session_json" 2>/dev/null || echo 0)"
    tunnel_pid="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get('tunnelPid',0))" <<< "$session_json" 2>/dev/null || echo 0)"
    if proc_alive "$server_pid" || proc_alive "$tunnel_pid"; then
        log_warn "Сессия ещё запущена или частично активна."
        return 1
    fi
    # Безопасность: удаляем только внутри SESSIONS_DIR.
    local real_session real_root
    real_session="$(cd "$session_dir" 2>/dev/null && pwd || echo "")"
    real_root="$(cd "$SESSIONS_DIR" 2>/dev/null && pwd || echo "")"
    if [ -z "$real_session" ] || [ -z "$real_root" ]; then
        log_error "Папка сессии вне каталога RepoPilot sessions."
        return 1
    fi
    case "$real_session" in
        "$real_root"|"$real_root"/*) ;;
        *) log_error "Папка сессии вне каталога RepoPilot sessions: $real_session"; return 1 ;;
    esac
    rm -rf "$real_session" 2>/dev/null && log_success "История удалена." || log_error "Не удалось удалить историю."
}



show_mission_brief() {
    local session_json="$1" api_key port brief
    api_key="$(json_py "import json,sys; print(json.load(sys.stdin).get('apiKey',''))" <<< "$session_json")"
    port="$(json_py "import json,sys; print(json.load(sys.stdin).get('localPort',0))" <<< "$session_json")"
    header "$(l "Mission Control" "Центр управления")"
    ui_notice progress "$(l "Building a live context brief" "Формирую актуальный контекст")" "$(l "Read-only · secret-free · no repository changes" "Только чтение · без секретов · без изменений")"
    brief="$(api_get "$port" "$api_key" /context/brief 2>/dev/null)" || {
        ui_notice error "$(l "Mission context unavailable" "Контекст миссии недоступен")" "/context/brief"
        printf '\n  Enter: ' >&2; read -r _; return 1
    }
    BRIEF_JSON="$brief" KAROX_LANG="$(get_selected_language)" "$PYTHON_EXE" - <<'PY'
import json,os
b=json.loads(os.environ["BRIEF_JSON"]); ru=os.environ.get("KAROX_LANG")=="ru"
identity=b.get("identity",{}); task=b.get("task",{}); git=b.get("git",{}); p=b.get("permissions",{})
def l(en,ru_): return ru_ if ru else en
def section(s): print("\n  ┌─ "+s.upper()+" "+"─"*max(2,54-len(s)))
def kv(k,v): print(f"  │  {k:<16} {v}")
actions={
 "stop_and_report_branch_mismatch":l("Stop: branch mismatch detected","Стоп: обнаружено несовпадение ветки"),
 "wait_for_or_start_real_task":l("Send or start the real task","Отправьте или запустите реальное ТЗ"),
 "inspect_existing_changes":l("Review the existing diff before editing","Проверьте существующий diff до изменений"),
 "inspect_project_context_then_execute_task":l("Inspect context, then execute the task","Изучите контекст, затем выполняйте задачу"),
}
section(l("Current mission","Текущая миссия"))
kv(l("Task","Задача"),task.get("status"))
kv(l("Repository","Репозиторий"),os.path.basename(identity.get("repoRoot","").rstrip("/\\")))
kv(l("Branch","Ветка"),identity.get("branch"))
kv(l("Access","Доступ"),identity.get("mode"))
kv(l("Working tree","Рабочее дерево"),l("CLEAN","ЧИСТО") if git.get("clean") else f"{git.get('changedCount')} "+l("changed paths","изменений"))
kv("Commit",l("allowed via /git/commit","разрешён через /git/commit") if p.get("commitAllowed") else l("blocked","заблокирован"))
kv("Push",l("allowed","разрешён") if p.get("pushAllowed") else l("blocked by policy","заблокирован политикой"))
section(l("Next move","Следующий шаг"))
raw=str(b.get("recommendedNextAction",""))
print("  ✓ "+actions.get(raw,raw)); print("    "+raw)
warnings=b.get("warnings") or []
if warnings:
 section(l("Guardrails","Ограничения"))
 for warning in warnings: print("  ! "+str(warning))
section(l("Context routes","Маршруты контекста"))
print("  /project/info  /tree/dir  /files/search  /files/read")
print("  /git/diff/stat  /git/diff/file  /audit  /session/report")
print("\n  ✓ "+l("AI can refresh this snapshot with GET /context/brief.","AI может обновить снимок через GET /context/brief."))
PY
    echo ""; printf '  Enter: ' >&2; read -r _
}

show_ai_readiness() {
    local session_json="$1" api_key port repo branch ok=1
    api_key="$(json_py "import json,sys; print(json.load(sys.stdin).get('apiKey',''))" <<< "$session_json")"
    port="$(json_py "import json,sys; print(json.load(sys.stdin).get('localPort',0))" <<< "$session_json")"
    repo="$(json_py "import json,sys; print(json.load(sys.stdin).get('repo',''))" <<< "$session_json")"
    branch="$(json_py "import json,sys; print(json.load(sys.stdin).get('branch',''))" <<< "$session_json")"
    header "$(l "AI readiness" "Готовность для AI")"
    ui_notice progress "$(l "Running four local preflight checks" "Выполняю четыре локальные проверки")" "$(l "Nothing is sent to the AI yet." "Данные ещё не передаются AI.")"
    local session health gitstatus contextbrief
    session="$(api_get "$port" "$api_key" /session 2>/dev/null)" || ok=0
    health="$(api_get "$port" "$api_key" /health 2>/dev/null)" || ok=0
    gitstatus="$(api_get "$port" "$api_key" /git/status 2>/dev/null)" || ok=0
    contextbrief="$(api_get "$port" "$api_key" /context/brief 2>/dev/null)" || ok=0
    ui_section "$(l "Preflight" "Предварительная проверка")"
    [ -n "$session" ] && printf '%s  │  ✓%s /session\n' "$C_GREEN" "$C_RESET" || { printf '%s  │  ×%s /session\n' "$C_RED" "$C_RESET"; ok=0; }
    [ -n "$health" ] && printf '%s  │  ✓%s /health\n' "$C_GREEN" "$C_RESET" || { printf '%s  │  ×%s /health\n' "$C_RED" "$C_RESET"; ok=0; }
    [ -n "$gitstatus" ] && printf '%s  │  ✓%s /git/status\n' "$C_GREEN" "$C_RESET" || { printf '%s  │  ×%s /git/status\n' "$C_RED" "$C_RESET"; ok=0; }
    [ -n "$contextbrief" ] && printf '%s  │  ✓%s /context/brief\n' "$C_GREEN" "$C_RESET" || { printf '%s  │  ×%s /context/brief\n' "$C_RED" "$C_RESET"; ok=0; }
    if [ "$ok" = 1 ]; then
        ui_section "$(l "Verdict" "Результат")"
        if SESSION_JSON="$session" EXPECTED_REPO="$repo" EXPECTED_BRANCH="$branch" "$PYTHON_EXE" - <<'PY'
import json,os,sys
s=json.loads(os.environ["SESSION_JSON"])
same=os.path.realpath(s.get("repoRoot",""))==os.path.realpath(os.environ["EXPECTED_REPO"]) and s.get("branch")==os.environ["EXPECTED_BRANCH"]
sys.exit(0 if same else 2)
PY
        then
            ui_notice success "$(l "READY FOR AI HANDOFF" "ГОТОВО К ПЕРЕДАЧЕ AI")" "$(l "Repository and branch match this session." "Репозиторий и ветка совпадают с сессией.")"
        else
            ui_notice error "$(l "HANDOFF BLOCKED" "ПЕРЕДАЧА ЗАБЛОКИРОВАНА")" "$(l "Repository or branch mismatch." "Репозиторий или ветка не совпадают.")"; ok=0
        fi
    fi
    echo ""; printf '  Enter: ' >&2; read -r _
    [ "$ok" = 1 ]
}

session_menu() {
    local session_dir="$1"
    while true; do
        local session_json status server_pid tunnel_pid title repo mode branch port url pid tp ac
        session_json="$(load_session_json "$session_dir")" || return 1
        server_pid="$(json_py "import json,sys; print(json.load(sys.stdin).get('serverPid',0))" <<< "$session_json")"
        tunnel_pid="$(json_py "import json,sys; print(json.load(sys.stdin).get('tunnelPid',0))" <<< "$session_json")"
        status="$(session_status "$server_pid" "$tunnel_pid")"
        title="$(json_py "import json,sys; print(json.load(sys.stdin).get('title',''))" <<< "$session_json")"
        repo="$(json_py "import json,sys; print(json.load(sys.stdin).get('repo',''))" <<< "$session_json")"
        mode="$(json_py "import json,sys; print(json.load(sys.stdin).get('mode',''))" <<< "$session_json")"
        branch="$(json_py "import json,sys; print(json.load(sys.stdin).get('branch',''))" <<< "$session_json")"
        port="$(json_py "import json,sys; print(json.load(sys.stdin).get('localPort',0))" <<< "$session_json")"
        url="$(json_py "import json,sys; print(json.load(sys.stdin).get('tunnelUrl',''))" <<< "$session_json")"
        pid="$(json_py "import json,sys; print(json.load(sys.stdin).get('providerId',''))" <<< "$session_json")"
        tp="$(json_py "import json,sys; print(json.load(sys.stdin).get('tunnelProvider','cloudflare'))" <<< "$session_json")"
        ac="$(json_py "import json,sys; print(json.load(sys.stdin).get('aiClient','promptql'))" <<< "$session_json")"
        header "$(l "Workspace" "Рабочее пространство") / $title"
        printf '  '; ui_badge "$(status_label "$status")" "$([ "$status" = running ] && echo "$C_GREEN" || echo "$C_YELLOW")"; printf '  %s\n' "$(repo_label "$repo")"
        ui_section "$(l "Flight data" "Параметры полёта")"
        ui_kv "$(l "Repository" "Репозиторий")" "$(repo_label "$repo")"
        ui_kv "$(l "Branch" "Ветка")" "$branch"; ui_kv "$(l "Access" "Доступ")" "$mode"; ui_kv "$(l "AI target" "AI-клиент")" "$(ai_client_label "$ac")"
        ui_section "$(l "Connection" "Подключение")"
        ui_kv "$(l "Public URL" "Публичный URL")" "$url"; ui_kv "$(l "Tunnel" "Туннель")" "$(provider_label "$tp")"; ui_kv "Provider ID" "$pid"; ui_kv "$(l "Local API" "Локальный API")" "127.0.0.1:$port"
        ui_section "$(l "AI launch sequence" "Запуск AI")"
        ui_choice V "$(l "VERIFY" "ПРОВЕРИТЬ")" "$(l "Run exact-session readiness checks" "Проверить точную сессию и готовность")" "$C_GREEN"
        ui_choice M "$(l "MISSION" "МИССИЯ")" "$(l "Inspect context, warnings and next action" "Открыть контекст, предупреждения и следующий шаг")" "$C_CYAN"
        ui_choice A "$(l "HANDOFF" "ПЕРЕДАТЬ")" "$(l "Copy the complete connection package" "Скопировать полный пакет подключения")" "$C_MAGENTA"
        printf '%s  │  [C] %s   [T] %s   [K] %s   [P] Provider ID%s\n' "$C_DARK" "$(l "Connect" "Подключение")" "$(l "Task" "Задача")" "$(l "Key" "Ключ")" "$C_RESET"
        ui_section "$(l "Controls" "Управление")"
        echo "  │  [L] $(l "Logs" "Логи")   [S] $(l "Stop session" "Остановить")   [B] $(l "Back" "Назад")"
        [ "$status" = stopped ] && printf '%s  │  [D] %s%s\n' "$C_RED" "$(l "Delete stopped history" "Удалить остановленную историю")" "$C_RESET"
        printf '\n  › %s: ' "$(l "Action" "Действие")" >&2
        local x; read -r x
        case "$x" in
            [Vv]) show_ai_readiness "$session_json" || true ;;
            [Mm]) show_mission_brief "$session_json" || true ;;
            [Cc]) copy_session_file "$session_dir" connect-prompt.txt "$(l "Connection prompt copied." "Prompt подключения скопирован.")" ;;
            [Tt]) copy_session_file "$session_dir" task-prompt.txt "$(l "Task template copied." "Шаблон задачи скопирован.")" ;;
            [Kk]) copy_session_file "$session_dir" api-key.txt "$(l "Secure key copied." "Секретный ключ скопирован.")" ;;
            [Pp]) copy_session_file "$session_dir" provider-id.txt "Provider ID copied." ;;
            [Aa]) copy_session_file "$session_dir" all.txt "$(l "Complete handoff copied." "Полный handoff скопирован.")" ;;
            [Ll]) show_log_tail "$session_json" ;;
            [Ss]) ask_yes "$(l "Stop this session?" "Остановить сессию?")" 0 && { stop_session_by_json "$session_json"; return; } ;;
            [Dd]) [ "$status" = stopped ] && ask_yes "$(l "Delete session history?" "Удалить историю сессии?")" 0 && { remove_session_history "$session_dir"; return; } ;;
            [Bb]) return ;;
        esac
    done
}

show_settings() {
    while true; do
        local settings provider ac language ok=0 ts_exe
        settings="$(load_settings)"; provider="$(get_selected_tunnel_provider)"
        ac="$(get_selected_ai_client)"; language="$(get_selected_language)"
        header "$(l "Workspace settings" "Настройки рабочего пространства")"
        ui_section "$(l "Current configuration" "Текущая конфигурация")"
        ui_kv "$(l "Language" "Язык")" "$([ "$language" = ru ] && echo Русский || echo English)"
        ui_kv "$(l "AI target" "AI-клиент")" "$(ai_client_label "$ac")"
        ui_kv "$(l "Secure tunnel" "Безопасный туннель")" "$(provider_label "$provider")"
        if [ "$provider" = tailscale ]; then
            ts_exe="$(find_tailscale 2>/dev/null || true)"
            [ -n "$ts_exe" ] && tailscale_ready "$ts_exe" && ok=1
        else find_cloudflared >/dev/null 2>&1 && ok=1; fi
        ui_kv "$(l "Connection" "Подключение")" "$([ "$ok" = 1 ] && l READY ГОТОВО || l "NEEDS ATTENTION" "ТРЕБУЕТ ВНИМАНИЯ")"
        [ "$ok" = 1 ] || ui_notice warn "$(l "Tunnel provider needs attention" "Туннель требует внимания")" "$(l "Refresh diagnostics or choose another provider." "Обновите диагностику или выберите другой туннель.")"
        ui_section "$(l "Preferences" "Параметры")"
        ui_choice 1 CLOUDFLARE "$(l "Quick ephemeral public tunnel" "Быстрый временный публичный туннель")" "$C_CYAN"
        ui_choice 2 TAILSCALE "$(l "Private identity-aware funnel" "Приватный туннель с проверкой личности")" "$C_MAGENTA"
        ui_choice A "$(l "AI TARGET" "AI-КЛИЕНТ")" "$(l "Change connection destination" "Изменить место подключения")" "$C_RESET"
        ui_choice L "$(l "LANGUAGE" "ЯЗЫК")" "$(l "Switch English / Русский" "Переключить English / Русский")" "$C_RESET"
        [ "$provider" = tailscale ] && ui_choice T "$(l "TAILSCALE LOGIN" "ВХОД TAILSCALE")" "$(l "Login or start CLI" "Войти или запустить CLI")" "$C_YELLOW"
        printf '%s  │  [R] %s   [B] %s%s\n\n' "$C_DARK" "$(l "Refresh" "Обновить")" "$(l "Back" "Назад")" "$C_RESET"
        printf '  › %s: ' "$(l "Action" "Действие")" >&2
        local x; read -r x
        case "$x" in
            1) save_settings cloudflare ;;
            2) save_settings tailscale ;;
            [Aa]) local picked; picked="$(select_ai_client)" && save_settings "" "$picked" ;;
            [Ll]) local picked_lang; picked_lang="$(select_language)" && save_settings "" "" "$picked_lang" ;;
            [Tt]) [ "$provider" = tailscale ] && "$(find_tailscale)" up || true ;;
            [Rr]) continue ;;
            [Bb]) return ;;
        esac
    done
}

stop_all_sessions() {
    local sessions_raw
    sessions_raw="$(get_sessions)"
    echo "$sessions_raw" | "$PYTHON_EXE" -c "
import json, sys, os, signal, subprocess
items = json.loads(sys.stdin.read() or '[]')
for s in items:
    for k in ('tunnelPid','serverPid'):
        pid = s.get(k, 0)
        try: pid = int(pid)
        except: continue
        if pid <= 0: continue
        try: os.kill(pid, 9)
        except OSError: pass
        if os.name == 'nt':
            try: subprocess.run(['taskkill','/PID',str(pid),'/T','/F'], capture_output=True)
            except: pass
" 2>/dev/null || true
}

remove_stopped_histories() {
    local sessions_raw deleted=0 skipped=0
    sessions_raw="$(get_sessions)"
    # python фильтрует stopped и удаляет папки.
    echo "$sessions_raw" | "$PYTHON_EXE" -c "
import json, sys, os, shutil
items = json.loads(sys.stdin.read() or '[]')
deleted = 0
skipped = 0
root = sys.argv[1]
for s in items:
    if s.get('status') != 'stopped':
        continue
    d = s.get('sessionDir','')
    if not d or not os.path.isdir(d):
        skipped += 1
        continue
    real = os.path.realpath(d)
    realroot = os.path.realpath(root)
    if real == realroot or not real.startswith(realroot + os.sep):
        skipped += 1
        continue
    try:
        shutil.rmtree(real)
        deleted += 1
    except Exception:
        skipped += 1
print(f'{deleted} {skipped}')
" "$SESSIONS_DIR" 2>/dev/null | { read -r d s; echo "$d $s"; }
}

# --- Главное меню ------------------------------------------------------------

show_manager() {
    while true; do
        local sessions_raw count live attention
        sessions_raw="$(get_sessions)"
        count="$(json_py "import json,sys; print(len(json.loads(sys.stdin.read() or '[]')))" <<< "$sessions_raw" 2>/dev/null || echo 0)"
        live="$(json_py "import json,sys; print(sum(x.get('status')=='running' for x in json.loads(sys.stdin.read() or '[]')))" <<< "$sessions_raw" 2>/dev/null || echo 0)"
        attention="$(json_py "import json,sys; print(sum(x.get('status')=='partial' for x in json.loads(sys.stdin.read() or '[]')))" <<< "$sessions_raw" 2>/dev/null || echo 0)"
        header "$(l "Repository sessions" "Сессии репозиториев")"
        printf '  '; ui_badge "$(ai_client_label "$(get_selected_ai_client)")" "$C_MAGENTA"; printf '  '; ui_badge "$(provider_label "$(get_selected_tunnel_provider)")" "$C_CYAN"; printf '  '; ui_badge "$(l "LIVE " "АКТИВНЫХ ")$live" "$C_GREEN"; echo ""
        ui_section "$(l "Workspaces" "Рабочие пространства")"
        if [ "$count" = 0 ]; then
            ui_empty "$(l "Your flight deck is clear" "Панель пока пуста")" "$(l "Press N to connect the first Git repository." "Нажмите N, чтобы подключить первый Git-репозиторий.")"
        else
            SESSIONS_JSON="$sessions_raw" KAROX_LANG="$(get_selected_language)" "$PYTHON_EXE" - <<'PY'
import json,os
items=json.loads(os.environ["SESSIONS_JSON"])
for i,s in enumerate(items,1):
    repo=os.path.basename(s.get("repo","").rstrip("/\\"))[:24]
    label={"running":"● LIVE","partial":"◐ DEGRADED"}.get(s.get("status"),"○ OFFLINE")
    title=s.get("title","")
    title="" if title=="-" else title[:20]
    print(f"  ╭─ [{i}] {repo}  {label}" + (f"  {title}" if title else ""))
    print(f"  │  {s.get('branch','')}")
    print(f"  ╰─ {s.get('mode','')}  ·  {s.get('tunnelProvider','')}  ·  {s.get('aiClient','')}")
PY
        fi
        ui_section "$(l "Command bar" "Панель команд")"
        ui_choice N "$(l "NEW SESSION" "НОВАЯ СЕССИЯ")" "$(l "Connect repository and choose access" "Подключить репозиторий и выбрать доступ")" "$C_MAGENTA"
        echo "  │  [number] $(l "Open" "Открыть")   [R] $(l "Refresh" "Обновить")   [G] $(l "Settings" "Настройки")"
        echo "  │  [D] $(l "Diagnostics" "Диагностика")   [U] $(l "Clear history" "Очистить историю")   [X] $(l "Stop all" "Остановить все")"
        printf '%s  │  [Q] %s%s\n\n' "$C_DARK" "$(l "Close manager — LIVE sessions keep running" "Закрыть менеджер — LIVE-сессии продолжат работу")" "$C_RESET"
        printf '  › %s: ' "$(l "Action" "Действие")" >&2
        local x; read -r x
        case "$x" in
            [Nn]) local d; d="$(start_new_session)" && session_menu "$d" || true ;;
            [Rr]) continue ;;
            [Gg]) show_settings ;;
            [Dd]) bash "$SCRIPT_DIR/doctor.sh" || true; read -r _ ;;
            [Uu]) remove_stopped_histories >/dev/null ;;
            [Xx]) ask_yes "$(l "Stop all local workspace sessions?" "Остановить все локальные сессии?")" 0 && stop_all_sessions ;;
            [Qq]) return ;;
            *) if [[ "$x" =~ ^[0-9]+$ ]] && [ "$x" -ge 1 ] && [ "$x" -le "$count" ]; then
                local d; d="$(echo "$sessions_raw" | "$PYTHON_EXE" -c "import json,sys; print(json.load(sys.stdin)[$x-1].get('sessionDir',''))")"; [ -n "$d" ] && session_menu "$d"
            fi ;;
        esac
    done
}

ensure_installed
ensure_language
show_karox_intro
ensure_ai_client
show_manager
