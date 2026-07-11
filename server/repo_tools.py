import os
import sys
import base64
import re
import json
import time
import uuid
import fnmatch
import shutil
import glob as glob_module
import subprocess
import shlex
import traceback
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
from typing import Optional, Dict, Any, List, Literal
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field
VERSION = "3.11.0"

def text_env(name: str, default: str = "") -> str:
    encoded = os.environ.get(f"{name}_B64")
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except Exception:
            pass
    return os.environ.get(name, default)

REPO_ROOT = Path(text_env("REPO_ROOT")).resolve()
API_KEY = os.environ["REPO_TOOLS_API_KEY"]
INITIAL_MODE = os.environ.get("REPO_TOOLS_MODE", "read_only").lower()
INITIAL_BRANCH = os.environ.get("REPO_TOOLS_BRANCH", "")
INITIAL_SESSION_TITLE = text_env("REPO_TOOLS_SESSION_TITLE", os.environ.get("REPO_TOOLS_TASK", ""))
INITIAL_TASK = text_env("REPO_TOOLS_INITIAL_TASK", "")
INITIAL_COMMIT_ALLOWED = os.environ.get("REPO_TOOLS_COMMIT_ALLOWED", "false").lower() == "true"
HOME_WORK = Path(os.environ.get("REPO_TOOLS_HOME", str(Path.home() / "promptql-repo-tools"))).resolve()
LOG_FILE = Path(os.environ.get("REPO_TOOLS_LOG_FILE", str(HOME_WORK / "logs" / "repo-tools-v3.jsonl"))).resolve()
RUNS_DIR = Path(os.environ.get("REPO_TOOLS_RUNS_DIR", str(REPO_ROOT / ".promptql" / "runs"))).resolve()
PRECHECK_CMD = os.environ.get("REPO_TOOLS_PRECHECK_CMD", "npm run compile && npm test")
MAX_FILE_SIZE = 25_000_000
MAX_INLINE_OUTPUT = 300_000
DEFAULT_TAIL = 60_000
TASK_STATE: Dict[str, Any] = {
    "mode": INITIAL_MODE,
    "sessionTitle": INITIAL_SESSION_TITLE,
    "task": INITIAL_TASK,
    "taskNote": "task is a real work instruction only when explicitly started; sessionTitle is just a label for returning to this RepoPilot session",
    "branch": INITIAL_BRANCH,
    "commitAllowed": INITIAL_COMMIT_ALLOWED,
    "pushAllowed": False,
    "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    "finishedAt": None,
    "status": "running",
}
app = FastAPI(
    title="KaroX Local Agent API",
    description="Локальный защищённый API для AI-агентов: чтение и изменение файлов, запуск dev-команд, безопасный git commit, audit logs и отчёты по задаче.",
    version=VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def authorization_bearer_compat_middleware(request, call_next):
    headers = list(request.scope.get("headers") or [])
    has_api_key = any(name.lower() == b"x-api-key" for name, _ in headers)
    if not has_api_key:
        for name, value in headers:
            if name.lower() != b"authorization":
                continue
            auth = value.decode("latin-1", errors="ignore").strip()
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
                if key:
                    headers.append((b"x-api-key", key.encode("latin-1", errors="ignore")))
                    request.scope["headers"] = headers
            break
    return await call_next(request)

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=VERSION,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["RepoPilotApiKey"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Paste the per-session RepoPilot X-API-Key from the protected credential card.",
    }

    for path, methods in schema.get("paths", {}).items():
        if path == "/":
            continue
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation["security"] = [{"RepoPilotApiKey": []}]
            parameters = operation.get("parameters")
            if parameters:
                operation["parameters"] = [
                    parameter for parameter in parameters
                    if not (
                        parameter.get("in") == "header"
                        and str(parameter.get("name", "")).lower() == "x-api-key"
                    )
                ]

    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi

@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """Глобальная защита: любая необработанная ошибка возвращает структурированный
    JSON-ответ вместо падения запроса. Сервер продолжает работать."""
    tb = traceback.format_exc()
    try:
        audit("unhandled_error", {
            "path": str(getattr(getattr(request, "url", None), "path", "")),
            "error": repr(exc)[:500],
            "traceback": tb[-2000:],
        })
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "Внутренняя ошибка сервера. Сервер продолжает работать.",
            "detail": str(exc)[:500],
            "hint": "Повторите запрос. Если ошибка повторяется — проверьте параметры запроса или посмотрите GET /audit.",
        },
    )

BLOCKED_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development", ".env.test",
    ".npmrc", ".yarnrc", ".pypirc",
    "id_rsa", "id_ed25519", "known_hosts",
    "credentials", "credentials.json",
    "token", "tokens", "cookies", "cookie",
}
BLOCKED_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
}
IGNORED_DIRS_FOR_TREE = {
    ".git", "node_modules", "dist", "build", ".next", ".gradle",
    "out", "target", ".idea", ".vscode", ".venv", ".promptql",
}
PROTECTED_WRITE_DIRS = {
    ".git", "node_modules", ".gradle", ".idea", ".venv",
}
GENERATED_PATTERNS = [
    ".promptql/**",
    ".promptql/runs/**",
    ".promptql/tmp/**",
    "compile-out.txt",
    "test-out.txt",
    "run-out.txt",
    "*-out.txt",
    "*.tmp",
    "*.log.tmp",
    ".gradle/**",
    "**/.gradle/**",
]
AGENT_HELPER_PATTERNS = [
    "commit.py",
    "commit[0-9]*.py",
    "*commit*.py",
    "push.py",
    "push_*.py",
    "*push*.py",
    "check_and_commit*.py",
    "push_and_check*.py",
    "run_and_commit*.py",
]
GENERATED_PATTERNS.extend(AGENT_HELPER_PATTERNS)
HARD_BLOCK_COMMAND_PATTERNS = [
    r"\bgit\s+push\b",
    r"\bgit\s+remote\s+(add|remove|set-url|rename)\b",
    r"\bgh\s+auth\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\bsftp\b",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\btakeown\b",
    r"\bicacls\b",
    r"\bnet\s+user\b",
    r"\breg\s+delete\b",
    r"\bpowershell\s+.*-enc",
    r"\bpowershell\s+.*encodedcommand",
    r"\brm\s+-rf\s+/",
    r"\brmdir\s+/s\s+[a-z]:\\",
    r"\brmdir\b.*\bsystem32\b",
    r"\brd\s+/s\s+[a-z]:\\",
    r"\bdel\s+/s\s+[a-z]:\\",
    r"\berase\s+/s\s+[a-z]:\\",
    r"\bformat\b.*[a-z]:",
    r"\bnpm\s+publish\b",
    r"\bpip\s+upload\b",
    r"\btwine\s+upload\b",
    # --- POSIX / macOS-опасные команды ---
    r"\bsudo\b",
    r"\brm\s+-rf\s+(~|\$HOME|\.\.|\*)",
    r"\brm\s+-rf\b.*\s(/|~|\$HOME)\s*$",
    r"\bdd\b.*\bof=/dev/",
    r"\bmkfs\b",
    r"\bdiskutil\s+(erasedisk|partitiondisk|unmountdisk)\b",
    r"\blaunchctl\s+(load|unload|bootout|remove)\b",
    r"\bdefaults\s+write\b.*\b(com\.apple|/Library/Preferences)\b",
    r"\bcsrutil\b",
    r"\bnvram\b",
    r"\bpmset\b.*\b(disable|sleep|shutdown|restart)\b",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",  # fork-бомба
    r">\s*/dev/sd[a-z]",
    r">\s*/dev/nvme",
    r"\bkillall\b.*(Finder|Dock|SystemUIServer|loginwindow|WindowServer)",
    r"\bchown\s+-R\b.*\s/(usr|bin|sbin|System|Library)\b",
    r"\bchmod\s+-R\b.*\s/(usr|bin|sbin|System|Library|etc)\b",
]
SENSITIVE_COMMAND_FRAGMENTS = [
    ".env", "id_rsa", "id_ed25519", ".pem", ".p12", ".pfx",
    "github_token", "password", "cookies",
]
AUTOPILOT_ALLOWED_PREFIXES = [
    "npm ",
    "npx ",
    "node ",
    "py ",
    "python ",
    "python3 ",
    "gradle ",
    "gradlew ",
    ".\\gradlew ",
    "./gradlew ",
    "./mvnw ",
    "dotnet ",
    "fable ",
    "powershell ",
    "pwsh ",
    "git status",
    "git diff",
    "git log",
    "git branch",
    "git rev-parse",
    "git show",
    "dir",
    "ls",
    "cat ",
    "type ",
]
AUTOPILOT_BLOCKED_RAW_GIT = [
    "git push",
    "git reset",
    "git clean",
    "git checkout",
    "git switch",
    "git merge",
    "git rebase",
    "git commit",
    "git add",
    "git tag",
    "git remote",
]
def mode() -> str:
    return str(TASK_STATE.get("mode") or "read_only").lower()
def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
def audit(action: str, data: Dict[str, Any]):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": now(),
            "version": VERSION,
            "mode": mode(),
            "branch": TASK_STATE.get("branch"),
            "sessionTitle": TASK_STATE.get("sessionTitle"),
            "task": TASK_STATE.get("task"),
            "action": action,
            "data": data,
        }
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
def normalize_supplied_api_key(value: Optional[str]) -> str:
    key = str(value or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if key.lower().startswith("x-api-key:"):
        key = key.split(":", 1)[1].strip()
    return key

def check_auth(x_api_key: Optional[str]):
    supplied = normalize_supplied_api_key(x_api_key)
    if supplied != API_KEY:
        audit("auth_failed", {
            "hasCredential": bool(supplied),
            "credentialLength": len(supplied),
            "expectedLength": len(API_KEY),
            "acceptedHeaders": ["X-API-Key", "Authorization: Bearer"],
        })
        raise HTTPException(status_code=401, detail="Неверный X-API-Key. Проверьте, что в защищённой карточке указан именно ключ сессии RepoPilot, а не provider id.")
def ensure_not_read_only(action: str):
    if mode() == "read_only":
        raise HTTPException(status_code=403, detail=f"Режим read_only блокирует {action}")
def rel_norm(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")
def path_matches(path: str, patterns: List[str]) -> bool:
    path = rel_norm(path)
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
    return False
def is_agent_helper_path(path: str) -> bool:
    return path_matches(path, AGENT_HELPER_PATTERNS)
def command_mentions_agent_helper(cmd: str) -> Optional[str]:
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        tokens = re.split(r"\s+", cmd)
    for token in tokens:
        cleaned = token.strip().strip('"').strip("'")
        cleaned = cleaned.replace("\\", "/")
        if is_agent_helper_path(cleaned):
            return cleaned
    return None
def is_sensitive_path(p: Path) -> bool:
    parts = [x.lower() for x in p.parts]
    name = p.name.lower()
    suffix = p.suffix.lower()
    if name in BLOCKED_NAMES:
        return True
    if suffix in BLOCKED_SUFFIXES:
        return True
    for part in parts:
        if part in BLOCKED_NAMES:
            return True
    return False
def safe_path(rel: str, for_write: bool = False, allow_generated: bool = False) -> Path:
    rel = rel_norm(rel)
    p = (REPO_ROOT / rel).resolve()
    repo_s = str(REPO_ROOT).lower()
    p_s = str(p).lower()
    if not (p_s == repo_s or p_s.startswith(repo_s + os.sep.lower())):
        raise HTTPException(status_code=400, detail="Путь выходит за пределы корня репозитория")
    if is_sensitive_path(p):
        raise HTTPException(status_code=403, detail="Заблокирован чувствительный файл/путь")
    if for_write:
        parts = {x.lower() for x in p.relative_to(REPO_ROOT).parts}
        protected = parts & PROTECTED_WRITE_DIRS
        if protected:
            if not (allow_generated and path_matches(rel, GENERATED_PATTERNS)):
                raise HTTPException(status_code=403, detail="Заблокирована запись в защищённую директорию")
    return p
def hard_block_reason(cmd: str) -> Optional[str]:
    c = cmd.lower().strip()
    for fragment in SENSITIVE_COMMAND_FRAGMENTS:
        # Совпадение по границам токена, а не по подстроке: слова вроде
        # "passwords" или "password-validation" внутри аргументов не должны
        # блокировать легитимные команды, но ".env", "id_rsa" и т.п. как
        # отдельные имена/пути блокируются по-прежнему.
        boundary = r"(^|[\s\\/\"'=:,])" + re.escape(fragment) + r"($|[\s\\/\"'.:,])"
        if re.search(boundary, c):
            return f"Чувствительный фрагмент заблокирован: {fragment}"
    for pattern in HARD_BLOCK_COMMAND_PATTERNS:
        if re.search(pattern, c, flags=re.IGNORECASE):
            return f"Опасная команда заблокирована по шаблону: {pattern}"
    return None
def autopilot_allowed(cmd: str) -> bool:
    c = cmd.lower().strip()

    # Режим autopilot использует чёрный список:
    # модель может запускать обычные команды сборки/разработки,
    # RepoPilot блокирует только опасные git/системные операции и утечку секретов.
    # Общие опасные паттерны (npm publish, pip upload, twine upload, format, rmdir system32)
    # уже проверяются в hard_block_reason через HARD_BLOCK_COMMAND_PATTERNS.
    blocked_patterns = [
        r"\bgit\s+push\b",
        r"\bgit\s+add\b",
        r"\bgit\s+commit\b",
        r"\bgit\s+reset\b",
        r"\bgit\s+clean\b",
        r"\bgit\s+checkout\b",
        r"\bgit\s+switch\b",
        r"\bgit\s+merge\b",
        r"\bgit\s+rebase\b",
        r"\bgit\s+tag\b",
        r"\bgit\s+remote\b",

        r"\bssh\b",
        r"\bscp\b",
        r"\bsftp\b",

        r"\bshutdown\b",
        r"\brestart-computer\b",
        r"\bdiskpart\b",
        r"\bbcdedit\b",

        r"\bpowershell\b.*\s-enc\b",
        r"\bpowershell\b.*encodedcommand",

        r"\bremove-item\b.*\s-recurse\b.*\s-force\b",
        r"\brm\s+-rf\b",
        r"\brmdir\s+/s\b",
        r"\brd\s+/s\b",
        r"\bdel\s+/s\b",
        r"\berase\s+/s\b",

        # --- POSIX / macOS-опасные операции (тоже блокируются в autopilot) ---
        r"\bsudo\b",
        r"\bdd\b.*\bof=/dev/",
        r"\bmkfs\b",
        r"\bdiskutil\s+(erasedisk|partitiondisk|unmountdisk)\b",
        r"\blaunchctl\s+(load|unload|bootout|remove)\b",
        r"\bdefaults\s+write\b",
        r"\bcsrutil\b",
        r"\bnvram\b",
        r"\bkillall\b",
        r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
        r">\s*/dev/sd",
        r">\s*/dev/nvme",
    ]

    for pattern in blocked_patterns:
        if re.search(pattern, c, flags=re.IGNORECASE):
            return False

    # Метасимволы оболочки позволяют прицепить произвольную команду к разрешённому
    # префиксу (например: "npm run build && rm -rf file"). Блокируем их в autopilot.
    if IS_WINDOWS:
        # cmd /d /s /c: &&, ||, |, ;, ^, перенаправления <>, %VAR%, перевод строки
        if re.search(r"[&|;^<>%\r\n]", c):
            return False
    else:
        # /bin/sh -c: |, &, ;, <, >, backticks, $(...), перевод строки.
        # % и ^ не являются метасимволами в POSIX sh.
        if re.search(r"[&|;<>`\r\n]|\$\(", c):
            return False

    # Дополнительная проверка по белому списку разрешённых префиксов
    allowed = any(c.startswith(prefix.lower()) for prefix in AUTOPILOT_ALLOWED_PREFIXES)
    if not allowed:
        return False

    return True

def decode_bytes(data: Optional[bytes]) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")
def run_raw(args: List[str], timeout: int = 7200, capture_file: Optional[Path] = None, tail: int = DEFAULT_TAIL) -> Dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    started = time.time()
    try:
        if capture_file:
            capture_file.parent.mkdir(parents=True, exist_ok=True)
            with capture_file.open("wb") as f:
                r = subprocess.run(
                    args,
                    cwd=str(REPO_ROOT),
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    shell=False,
                    env=env,
                )
            raw = capture_file.read_bytes() if capture_file.exists() else b""
            out = decode_bytes(raw)
            elapsed = round(time.time() - started, 2)
            try:
                out_rel = capture_file.relative_to(REPO_ROOT).as_posix()
            except Exception:
                out_rel = str(capture_file)
            return {
                "command": args,
                "exitCode": r.returncode,
                "elapsedSeconds": elapsed,
                "stdout": out[-tail:],
                "stderr": "",
                "outputFile": out_rel,
                "truncated": len(out) > tail,
            }
        r = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=False,
            timeout=timeout,
            shell=False,
            env=env,
        )
        stdout = decode_bytes(r.stdout)
        stderr = decode_bytes(r.stderr)
        elapsed = round(time.time() - started, 2)
        return {
            "command": args,
            "exitCode": r.returncode,
            "elapsedSeconds": elapsed,
            "stdout": stdout[-MAX_INLINE_OUTPUT:],
            "stderr": stderr[-MAX_INLINE_OUTPUT:],
            "truncated": (len(stdout) > MAX_INLINE_OUTPUT or len(stderr) > MAX_INLINE_OUTPUT),
        }
    except subprocess.TimeoutExpired as e:
        stdout = decode_bytes(e.stdout if isinstance(e.stdout, bytes) else None)
        stderr = decode_bytes(e.stderr if isinstance(e.stderr, bytes) else None)
        return {
            "command": args,
            "exitCode": 408,
            "elapsedSeconds": timeout,
            "stdout": stdout[-MAX_INLINE_OUTPUT:],
            "stderr": stderr[-MAX_INLINE_OUTPUT:],
            "timeout": True,
        }
    except FileNotFoundError as e:
        return {
            "command": args,
            "exitCode": 127,
            "elapsedSeconds": round(time.time() - started, 2),
            "stdout": "",
            "stderr": f"Команда не найдена: {e}",
            "error": "command_not_found",
        }
    except OSError as e:
        return {
            "command": args,
            "exitCode": 126,
            "elapsedSeconds": round(time.time() - started, 2),
            "stdout": "",
            "stderr": f"Ошибка запуска команды: {e}",
            "error": "os_error",
        }
def run_cmd(cmd: str, timeout: int = 7200, capture_file: Optional[Path] = None, tail: int = DEFAULT_TAIL) -> Dict[str, Any]:
    # Windows: команды исполняются через cmd.exe; POSIX (macOS/Linux): через /bin/sh -c.
    # Метасимволы и блок-листы настраиваются в autopilot_allowed/hard_block_reason.
    if IS_WINDOWS:
        shell_argv = ["cmd", "/d", "/s", "/c", cmd]
    else:
        shell_argv = ["/bin/sh", "-c", cmd]
    return run_raw(shell_argv, timeout=timeout, capture_file=capture_file, tail=tail)
def run_git(git_args: List[str], timeout: int = 300, capture_file: Optional[Path] = None) -> Dict[str, Any]:
    return run_raw(["git"] + git_args, timeout=timeout, capture_file=capture_file)
def current_branch() -> str:
    r = run_git(["branch", "--show-current"], timeout=60)
    return (r.get("stdout") or "").strip()
def require_promptql_branch():
    br = current_branch()
    if not br.startswith("promptql/"):
        raise HTTPException(status_code=403, detail=f"Безопасные git-изменения требуют ветку promptql/*. Текущая ветка: {br}")
    return br
def changed_files_porcelain() -> List[Dict[str, str]]:
    r = run_git(["status", "--porcelain=v1"], timeout=120)
    raw = r.get("stdout", "")
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        path = path.strip().strip('"')
        out.append({"status": status, "path": rel_norm(path)})
    return out
def read_audit_tail(limit: int = 200) -> List[Dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"raw": line})
    return out
def scan_secret_content(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    if path.stat().st_size > 2_000_000:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    # Маркеры собраны конкатенацией, чтобы сканер не срабатывал на самого себя:
    # этот файл содержит список маркеров, и при буквальной записи /git/commit
    # блокировал бы коммит собственного кода сервера.
    _pk = "-----BEGIN " + "PRIVATE KEY-----"
    _opk = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
    high_confidence_strings = [
        _pk,
        _opk,
        "ghp" + "_",
        "gho" + "_",
        "github" + "_pat_",
        "sk-" + "ant-",
        "sk-" + "proj-",
    ]
    for marker in high_confidence_strings:
        if marker in text:
            return f"Найден высокодоверительный маркер секрета: {marker}"
    if re.search(r"sk-[A-Za-z0-9_-]{20,}", text):
        return "Найден высокодоверительный маркер секрета в стиле OpenAI"
    return None
def collect_project_info() -> Dict[str, Any]:
    project: Dict[str, Any] = {"repoRoot": str(REPO_ROOT), "files": {}}
    # package.json
    pkg = REPO_ROOT / "package.json"
    if pkg.is_file() and pkg.stat().st_size <= MAX_FILE_SIZE:
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            project["files"]["package.json"] = {
                "name": data.get("name"),
                "version": data.get("version"),
                "scripts": data.get("scripts"),
                "dependencies": bool(data.get("dependencies")),
                "devDependencies": bool(data.get("devDependencies")),
            }
        except Exception as e:
            project["files"]["package.json"] = {"error": str(e)}
    # .csproj / .fsproj
    for ext in ["*.csproj", "*.fsproj"]:
        for fp in REPO_ROOT.rglob(ext):
            if is_sensitive_path(fp):
                continue
            rel = fp.relative_to(REPO_ROOT).as_posix()
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()[:50]
                project["files"][rel] = {"head": "\n".join(lines)}
            except Exception as e:
                project["files"][rel] = {"error": str(e)}
    # .sln
    for fp in REPO_ROOT.glob("*.sln"):
        rel = fp.relative_to(REPO_ROOT).as_posix()
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()[:30]
            project["files"][rel] = {"head": "\n".join(lines)}
        except Exception as e:
            project["files"][rel] = {"error": str(e)}
    # fable.config.json
    fable = REPO_ROOT / "fable.config.json"
    if fable.is_file() and fable.stat().st_size <= MAX_FILE_SIZE:
        try:
            data = json.loads(fable.read_text(encoding="utf-8", errors="replace"))
            project["files"]["fable.config.json"] = data
        except Exception as e:
            project["files"]["fable.config.json"] = {"error": str(e)}
    # README.md first 100 lines
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        try:
            lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()[:100]
            project["files"]["README.md"] = {"head": "\n".join(lines)}
        except Exception as e:
            project["files"]["README.md"] = {"error": str(e)}
    # Summary
    project["summary"] = {
        "hasPackageJson": "package.json" in project["files"],
        "hasFableConfig": "fable.config.json" in project["files"],
        "hasReadme": "README.md" in project["files"],
        "projectFiles": [k for k in project["files"].keys() if k.endswith((".csproj", ".fsproj"))],
        "solutionFiles": [k for k in project["files"].keys() if k.endswith(".sln")],
    }
    return project

def build_tree_dir(rel_dir: str, max_files: int = 20000) -> Dict[str, Any]:
    base = safe_path(rel_dir) if rel_dir else REPO_ROOT
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="Директория не найдена")
    directories = []
    files = []
    count = 0
    try:
        entries = sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Нет доступа к директории")
    for entry in entries:
        if entry.name in IGNORED_DIRS_FOR_TREE:
            continue
        if is_sensitive_path(entry):
            continue
        rel = entry.relative_to(REPO_ROOT).as_posix()
        if entry.is_dir():
            child = {"name": entry.name, "path": rel, "type": "directory"}
            directories.append(child)
        elif entry.is_file():
            size = entry.stat().st_size
            files.append({"name": entry.name, "path": rel, "type": "file", "size": size})
        count += 1
        if count >= max_files:
            break
    return {
        "path": rel_dir or ".",
        "directories": directories,
        "files": files,
        "truncated": count >= max_files,
        "count": count,
    }

def search_files(q: str, glob: str = "*", max_files: int = 100) -> List[Dict[str, Any]]:
    results = []
    pattern = glob or "*"
    seen = set()
    for fp in REPO_ROOT.rglob(pattern):
        if not fp.is_file():
            continue
        if is_sensitive_path(fp):
            continue
        if fp.stat().st_size > MAX_FILE_SIZE:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            if q in text:
                rel = fp.relative_to(REPO_ROOT).as_posix()
                if rel in seen:
                    continue
                seen.add(rel)
                results.append({"path": rel, "size": fp.stat().st_size})
                if len(results) >= max_files:
                    break
        except (OSError, PermissionError):
            continue
    return results

def cleanup_generated_internal() -> Dict[str, Any]:
    cleaned = []
    skipped = []
    for item in changed_files_porcelain():
        path = item["path"]
        status = item["status"]
        if not path_matches(path, GENERATED_PATTERNS):
            skipped.append({"path": path, "status": status})
            continue
        p = safe_path(path, for_write=True, allow_generated=True)
        if status.startswith("??"):
            if p.is_file():
                p.unlink()
                cleaned.append({"path": path, "action": "delete-untracked-file"})
            elif p.is_dir():
                shutil.rmtree(p)
                cleaned.append({"path": path, "action": "delete-untracked-dir"})
            else:
                cleaned.append({"path": path, "action": "untracked-missing"})
        else:
            r = run_git(["restore", "--", path], timeout=120)
            cleaned.append({"path": path, "action": "git-restore", "exitCode": r.get("exitCode")})
    audit("cleanup_generated", {"cleaned": cleaned, "skippedCount": len(skipped)})
    return {"ok": True, "cleaned": cleaned, "skipped": skipped}
class ReadFilesBody(BaseModel):
    paths: List[str]
class SearchFilesQuery:
    q: str = Query(..., min_length=1, description="Строка поиска по содержимому файлов")
    glob: Optional[str] = Query("*", description="Glob-маска файлов, например *.fs")
    max_files: int = Query(100, ge=1, le=500, description="Максимальное количество файлов в результате")
class TreeDirQuery:
    path: Optional[str] = Query("", description="Относительный путь к директории")
    max_files: int = Query(20000, ge=1, le=100000, description="Максимальное количество файлов в дереве")
class WriteFileBody(BaseModel):
    path: str
    content: str
class BatchWriteBody(BaseModel):
    files: List[WriteFileBody]
class RunBody(BaseModel):
    cmd: str
    timeoutSeconds: Optional[int] = Field(default=None, ge=5, le=21600)
    capture: Literal["inline", "file"] = "inline"
    outputFile: Optional[str] = None
    tail: Optional[int] = Field(default=None, ge=1000, le=500000)
class GitCommitBody(BaseModel):
    message: str
    include: List[str]
    cleanupGenerated: bool = True
    runPreCommitChecks: bool = False
class GitRestoreBody(BaseModel):
    paths: List[str]
class TaskStartBody(BaseModel):
    task: str
    mode: Optional[str] = None
    commitAllowed: Optional[bool] = None
class TaskFinishBody(BaseModel):
    status: str = "finished"
@app.get("/")
def root():
    return {
        "name": "KaroX",
        "version": VERSION,
        "message": "Star For KaroX готов. Начните с /session, /health и /git/status; используйте X-API-Key.",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }
@app.get("/health")
def health(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    br = current_branch() if (REPO_ROOT / ".git").exists() else TASK_STATE.get("branch")
    return {
        "ok": True,
        "version": VERSION,
        "repoRoot": str(REPO_ROOT),
        "mode": mode(),
        "branch": br,
        "sessionTitle": TASK_STATE.get("sessionTitle"),
        "task": TASK_STATE.get("task"),
        "taskNote": TASK_STATE.get("taskNote"),
        "commitAllowed": TASK_STATE.get("commitAllowed"),
        "pushAllowed": False,
        "runsDir": str(RUNS_DIR),
    }
@app.get("/session")
def session(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return {
        "version": VERSION,
        "repoRoot": str(REPO_ROOT),
        "mode": mode(),
        "branch": current_branch() if (REPO_ROOT / ".git").exists() else TASK_STATE.get("branch"),
        "sessionTitle": TASK_STATE.get("sessionTitle"),
        "task": TASK_STATE.get("task"),
        "taskNote": TASK_STATE.get("taskNote"),
        "commitAllowed": TASK_STATE.get("commitAllowed"),
        "pushAllowed": False,
        "startedAt": TASK_STATE.get("startedAt"),
        "finishedAt": TASK_STATE.get("finishedAt"),
        "status": TASK_STATE.get("status"),
        "logFile": str(LOG_FILE),
    }
@app.post("/task/start")
def task_start(body: TaskStartBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    if body.mode:
        if body.mode not in ["read_only", "autopilot", "full"]:
            raise HTTPException(status_code=400, detail="Некорректный режим mode")
        TASK_STATE["mode"] = body.mode
    TASK_STATE["task"] = body.task
    TASK_STATE["status"] = "running"
    TASK_STATE["startedAt"] = now()
    TASK_STATE["finishedAt"] = None
    if body.commitAllowed is not None:
        TASK_STATE["commitAllowed"] = bool(body.commitAllowed)
    audit("task_start", dict(TASK_STATE))
    return {"ok": True, "task": TASK_STATE}
@app.get("/task/status")
def task_status(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return {
        "task": TASK_STATE,
        "session": session(x_api_key),
        "changedFiles": changed_files_porcelain(),
    }
@app.post("/task/finish")
def task_finish(body: TaskFinishBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    TASK_STATE["status"] = body.status
    TASK_STATE["finishedAt"] = now()
    audit("task_finish", dict(TASK_STATE))
    return {"ok": True, "task": TASK_STATE}
@app.get("/task/report")
def task_report(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return {
        "session": session(x_api_key),
        "changedFiles": changed_files_porcelain(),
        "gitStatus": run_git(["status", "--short", "--branch"], timeout=120),
        "diffStat": run_git(["diff", "--stat"], timeout=180),
        "latestCommit": run_git(["log", "-1", "--stat", "--oneline"], timeout=120),
        "auditTail": read_audit_tail(80),
    }
@app.get("/tree")
def tree(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    max_files: int = Query(20000, ge=1, le=100000),
):
    check_auth(x_api_key)
    files = []
    for root, dirs, filenames in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS_FOR_TREE]
        for name in filenames:
            p = Path(root) / name
            if is_sensitive_path(p):
                continue
            rel = p.relative_to(REPO_ROOT).as_posix()
            files.append(rel)
            if len(files) >= max_files:
                audit("tree", {"count": len(files), "truncated": True})
                return {"files": files, "truncated": True, "count": len(files)}
    audit("tree", {"count": len(files), "truncated": False})
    return {"files": files, "truncated": False, "count": len(files)}
@app.get("/file")
def read_file(path: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    if p.stat().st_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    content = p.read_text(encoding="utf-8", errors="replace")
    audit("read_file", {"path": path, "chars": len(content)})
    return {"path": rel_norm(path), "content": content}
@app.post("/file")
def write_file(body: WriteFileBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("POST /file")
    if is_agent_helper_path(body.path):
        raise HTTPException(status_code=403, detail="Служебные helper-скрипты для commit/push/check запрещены. Используйте /run для проверок и /git/commit для коммита.")
    p = safe_path(body.path, for_write=True)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=409, detail=f"Не удалось записать файл (возможно, занят другим процессом или диск недоступен): {e}")
    audit("write_file", {"path": body.path, "chars": len(body.content)})
    return {"ok": True, "path": rel_norm(body.path)}
@app.post("/files/batch-write")
def batch_write(body: BatchWriteBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("POST /files/batch-write")
    # Сначала валидируем все пути, потом пишем — чтобы отказ валидации не оставлял частичную запись
    checked = []
    for item in body.files:
        if is_agent_helper_path(item.path):
            raise HTTPException(status_code=403, detail=f"Служебный helper-скрипт запрещён: {item.path}. Используйте /run для проверок и /git/commit для коммита.")
        checked.append((item, safe_path(item.path, for_write=True)))
    written = []
    errors = []
    for item, p in checked:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(item.content, encoding="utf-8")
            written.append(rel_norm(item.path))
        except OSError as e:
            errors.append({"path": rel_norm(item.path), "error": str(e)})
    audit("batch_write", {"count": len(written), "files": written, "errors": errors})
    if errors:
        return {"ok": False, "written": written, "errors": errors}
    return {"ok": True, "written": written}
@app.delete("/file")
def delete_file(path: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("DELETE /file")
    p = safe_path(path, for_write=True)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not p.is_file():
        raise HTTPException(status_code=403, detail="Удаление директорий через этот endpoint запрещено")
    try:
        p.unlink()
    except OSError as e:
        raise HTTPException(status_code=409, detail=f"Не удалось удалить файл (возможно, занят другим процессом): {e}")
    audit("delete_file", {"path": path})
    return {"ok": True, "deleted": rel_norm(path)}
@app.post("/run")
def run_command(body: RunBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("POST /run")
    cmd = body.cmd.strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="Пустая команда")
    reason = hard_block_reason(cmd)
    if reason:
        audit("run_blocked", {"cmd": cmd, "reason": reason})
        raise HTTPException(status_code=403, detail=reason)
    helper = command_mentions_agent_helper(cmd)
    if helper:
        reason = f"Запуск служебного helper-скрипта запрещён: {helper}. Используйте /run для прямой проверки и /git/commit для коммита."
        audit("run_blocked_helper", {"cmd": cmd, "helper": helper, "reason": reason})
        raise HTTPException(status_code=403, detail=reason)
    if mode() == "autopilot" and not autopilot_allowed(cmd):
        reason = "Команда заблокирована в режиме autopilot. Используйте специализированный endpoint, например /git/commit, или переключитесь в режим full."
        audit("run_blocked_autopilot", {"cmd": cmd, "reason": reason})
        raise HTTPException(status_code=403, detail=reason)
    timeout = body.timeoutSeconds or 7200
    tail = body.tail or DEFAULT_TAIL
    capture_file = None
    if body.capture == "file":
        run_id = "run-" + uuid.uuid4().hex[:12]
        if body.outputFile:
            rel = rel_norm(body.outputFile)
            if not rel.startswith(".promptql/runs/"):
                rel = ".promptql/runs/" + rel
            capture_file = safe_path(rel, for_write=True, allow_generated=True)
        else:
            capture_file = REPO_ROOT / ".promptql" / "runs" / f"{run_id}.txt"
    audit("run_start", {"cmd": cmd, "capture": body.capture, "timeout": timeout})
    result = run_cmd(cmd, timeout=timeout, capture_file=capture_file, tail=tail)
    audit("run_result", {
        "cmd": cmd,
        "exitCode": result.get("exitCode"),
        "elapsedSeconds": result.get("elapsedSeconds"),
        "outputFile": result.get("outputFile"),
        "truncated": result.get("truncated"),
    })
    return result
@app.get("/git/status")
def git_status(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return run_git(["status", "--short", "--branch"], timeout=120)
@app.get("/git/changed-files")
def git_changed_files(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return {"files": changed_files_porcelain()}
@app.get("/git/diff/stat")
def git_diff_stat(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return run_git(["diff", "--stat"], timeout=180)
@app.get("/git/diff/name-only")
def git_diff_name_only(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return run_git(["diff", "--name-only"], timeout=180)
@app.get("/git/diff/file")
def git_diff_file(path: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    safe_path(path)
    return run_git(["diff", "--", rel_norm(path)], timeout=240)
@app.get("/git/diff/full")
def git_diff_full(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    max_chars: int = Query(300000, ge=1000, le=1000000),
):
    check_auth(x_api_key)
    r = run_git(["diff"], timeout=300)
    out = r.get("stdout", "")
    r["stdout"] = out[-max_chars:]
    r["truncated"] = len(out) > max_chars
    return r
@app.post("/git/cleanup-generated")
def git_cleanup_generated(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("POST /git/cleanup-generated")
    return cleanup_generated_internal()
@app.post("/git/restore")
def git_restore(body: GitRestoreBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("POST /git/restore")
    require_promptql_branch()
    restored = []
    for path in body.paths:
        path = rel_norm(path)
        safe_path(path, for_write=True, allow_generated=True)
        if not path_matches(path, GENERATED_PATTERNS):
            raise HTTPException(status_code=403, detail=f"Отказано в восстановлении не-сгенерированного пути через этот endpoint: {path}")
        r = run_git(["restore", "--", path], timeout=120)
        restored.append({"path": path, "exitCode": r.get("exitCode")})
    audit("git_restore", {"restored": restored})
    return {"ok": True, "restored": restored}
@app.post("/git/commit")
def git_commit(body: GitCommitBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    ensure_not_read_only("POST /git/commit")
    if mode() == "autopilot" and not bool(TASK_STATE.get("commitAllowed")):
        raise HTTPException(status_code=403, detail="Git commit запрещён в режиме autopilot для этой сессии")
    br = require_promptql_branch()
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Сообщение коммита пустое")
    include = [rel_norm(p) for p in body.include]
    if not include:
        raise HTTPException(status_code=400, detail="Не указаны файлы для коммита")
    for p in include:
        safe = safe_path(p)
        if is_sensitive_path(safe):
            raise HTTPException(status_code=403, detail=f"Чувствительный путь не может быть закоммичен: {p}")
        secret = scan_secret_content(safe)
        if secret:
            raise HTTPException(status_code=403, detail=f"Сканирование секретов заблокировало {p}: {secret}")
        if path_matches(p, GENERATED_PATTERNS):
            raise HTTPException(status_code=403, detail=f"Сгенерированный/временный файл не может быть закоммичен: {p}")
    cleanup_result = None
    if body.cleanupGenerated:
        cleanup_result = cleanup_generated_internal()
    # Фиксируем, что было в staged до нашего reset, и возвращаем это в ответе
    # (unstagedByCommit) — чтобы снятие вручную застейдженных файлов не было молчаливым.
    pre_staged_r = run_git(["diff", "--cached", "--name-only"], timeout=120)
    previously_staged = [rel_norm(x) for x in pre_staged_r.get("stdout", "").splitlines() if x.strip()]
    run_git(["reset", "--"], timeout=120)
    add_result = run_git(["add", "--"] + include, timeout=120)
    if add_result.get("exitCode") != 0:
        raise HTTPException(status_code=500, detail={"stageFailed": add_result, "message": "Этапа git add не удалось выполнить"})
    cached = run_git(["diff", "--cached", "--name-only"], timeout=120)
    staged = [rel_norm(x) for x in cached.get("stdout", "").splitlines() if x.strip()]
    allowed = set(include)
    extra = [x for x in staged if x not in allowed]
    if extra:
        run_git(["reset", "--"], timeout=120)
        raise HTTPException(status_code=403, detail=f"Отказано в коммите неожиданных staged файлов: {extra}")
    if not staged:
        raise HTTPException(status_code=400, detail="Нет файлов в staged для коммита")
    check = run_git(["diff", "--cached", "--check"], timeout=120)
    if check.get("exitCode") != 0:
        run_git(["reset", "--"], timeout=120)
        raise HTTPException(status_code=400, detail={"diffCheckFailed": check, "message": "Проверка diff завершилась с ошибками"})
    precheck_result = None
    if body.runPreCommitChecks:
        precheck_file = REPO_ROOT / ".promptql" / "runs" / ("precommit-" + uuid.uuid4().hex[:10] + ".txt")
        precheck_result = run_cmd(PRECHECK_CMD, timeout=3600, capture_file=precheck_file, tail=DEFAULT_TAIL)
        if precheck_result.get("exitCode") != 0:
            run_git(["reset", "--"], timeout=120)
            raise HTTPException(status_code=400, detail={"preCommitChecksFailed": precheck_result, "message": "Предварительные проверки не пройдены"})
    commit_result = run_git(["commit", "-m", message], timeout=300)
    if commit_result.get("exitCode") != 0:
        raise HTTPException(status_code=500, detail={"commitFailed": commit_result, "message": "Git commit не выполнен"})
    log_result = run_git(["log", "-1", "--stat", "--oneline"], timeout=120)
    status_result = run_git(["status", "--short", "--branch"], timeout=120)
    hash_result = run_git(["rev-parse", "--short", "HEAD"], timeout=60)
    audit("git_commit", {
        "branch": br,
        "message": message,
        "staged": staged,
        "exitCode": commit_result.get("exitCode"),
        "hash": (hash_result.get("stdout") or "").strip(),
    })
    return {
        "ok": True,
        "branch": br,
        "message": message,
        "hash": (hash_result.get("stdout") or "").strip(),
        "committedFiles": staged,
        "unstagedByCommit": previously_staged,
        "cleanup": cleanup_result,
        "precheck": precheck_result,
        "commit": commit_result,
        "log": log_result,
        "status": status_result,
    }
@app.get("/git/log/latest")
def git_log_latest(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return run_git(["log", "-1", "--stat", "--oneline"], timeout=120)
@app.get("/logs", response_class=PlainTextResponse)
def logs(x_api_key: Optional[str] = Header(None, alias="X-API-Key"), tail: int = Query(300, ge=1, le=5000)):
    check_auth(x_api_key)
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])
@app.get("/audit")
def audit_json(x_api_key: Optional[str] = Header(None, alias="X-API-Key"), tail: int = Query(200, ge=1, le=2000)):
    check_auth(x_api_key)
    return {"items": read_audit_tail(tail)}
@app.get("/session/report")
def session_report(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    return task_report(x_api_key)

@app.post("/files/read")
def read_files(body: ReadFilesBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    out = []
    for rel in body.paths:
        rel = rel_norm(rel)
        try:
            p = safe_path(rel)
        except HTTPException as e:
            out.append({"path": rel, "content": None, "error": e.detail})
            continue
        if not p.exists():
            out.append({"path": rel, "content": None, "error": "Файл не найден"})
            continue
        if not p.is_file():
            out.append({"path": rel, "content": None, "error": "Путь не является файлом"})
            continue
        if p.stat().st_size > MAX_FILE_SIZE:
            out.append({"path": rel, "content": None, "error": "Файл слишком большой"})
            continue
        content = p.read_text(encoding="utf-8", errors="replace")
        out.append({"path": rel, "content": content, "error": None})
    audit("read_files", {"count": len(out)})
    return {"files": out}

@app.get("/files/search")
def files_search(
    q: str = Query(..., min_length=1, description="Строка поиска"),
    glob: Optional[str] = Query("*", description="Glob-маска файлов"),
    max_files: int = Query(100, ge=1, le=500, description="Максимум файлов"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    check_auth(x_api_key)
    results = search_files(q, glob or "*", max_files)
    audit("files_search", {"q": q, "glob": glob, "results": len(results)})
    return {"q": q, "glob": glob, "count": len(results), "files": results}


@app.get("/context/brief")
def context_brief(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Return a compact, secret-free operating brief for an AI handoff."""
    check_auth(x_api_key)
    current = mode()
    actual_branch = current_branch()
    changed = changed_files_porcelain()
    project = collect_project_info()
    latest = run_git(["log", "-1", "--oneline"], timeout=60)
    git_status = run_git(["status", "--short", "--branch"], timeout=120)

    if current == "read_only":
        allowed_prefixes: List[str] = []
    elif current == "autopilot":
        allowed_prefixes = list(AUTOPILOT_ALLOWED_PREFIXES)
    else:
        allowed_prefixes = [
            "* (full mode still applies hard command, path, secret, and push blocks)"
        ]

    warnings: List[str] = []
    expected_branch = str(TASK_STATE.get("branch") or "")
    task_status = str(TASK_STATE.get("status") or "not_started")
    if expected_branch and actual_branch != expected_branch:
        warnings.append(
            f"Branch mismatch: session expects {expected_branch}, repository is on {actual_branch}"
        )
    if changed:
        warnings.append(
            f"Working tree has {len(changed)} changed path(s); inspect diffs before editing or committing"
        )
    if task_status != "running":
        warnings.append(
            "No task is currently running; wait for or explicitly start the real user task"
        )
    if current == "read_only":
        warnings.append("Session is read-only; repository mutations and commits are unavailable")
    warnings.append("Push is disabled for KaroX sessions")

    if expected_branch and actual_branch != expected_branch:
        next_action = "stop_and_report_branch_mismatch"
    elif task_status != "running":
        next_action = "wait_for_or_start_real_task"
    elif changed:
        next_action = "inspect_existing_changes"
    else:
        next_action = "inspect_project_context_then_execute_task"

    brief = {
        "generatedAt": now(),
        "identity": {
            "product": "Star For KaroX",
            "repoRoot": str(REPO_ROOT),
            "branch": actual_branch,
            "expectedBranch": expected_branch,
            "mode": current,
        },
        "task": {
            "status": task_status,
            "instruction": TASK_STATE.get("task"),
            "sessionLabel": TASK_STATE.get("sessionTitle"),
            "startedAt": TASK_STATE.get("startedAt"),
            "finishedAt": TASK_STATE.get("finishedAt"),
        },
        "permissions": {
            "commitAllowed": bool(TASK_STATE.get("commitAllowed")),
            "pushAllowed": False,
            "allowedCommandPrefixes": allowed_prefixes,
            "hardBlocksRemainActive": True,
        },
        "git": {
            "clean": not bool(changed),
            "changedCount": len(changed),
            "changedFiles": changed,
            "status": git_status,
            "latestCommit": latest,
        },
        "project": {
            "summary": project.get("summary", {}),
            "detectedContextFiles": list(project.get("files", {}).keys()),
        },
        "workflow": {
            "preflight": ["/session", "/health", "/git/status", "/context/brief"],
            "largeOutput": "Use capture=file",
            "beforeCommit": "Call /git/cleanup-generated",
            "commit": "Use /git/commit only",
            "push": "Never push",
        },
        "warnings": warnings,
        "recommendedNextAction": next_action,
        "discovery": {
            "project": ["/project/info", "/tree/dir", "/files/search", "/files/read"],
            "changes": ["/git/changed-files", "/git/diff/stat", "/git/diff/file", "/git/log/latest"],
            "operations": ["/run/allowed", "/audit", "/session/report"],
        },
    }
    audit("context_brief", {
        "taskStatus": task_status,
        "changedCount": len(changed),
        "warningCount": len(warnings),
        "recommendedNextAction": next_action,
    })
    return brief

@app.get("/project/info")
def project_info(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    info = collect_project_info()
    audit("project_info", {"files": list(info.get("files", {}).keys())})
    return {"project": info}

@app.get("/tree/dir")
def tree_dir(
    path: Optional[str] = Query("", description="Относительный путь к директории"),
    max_files: int = Query(20000, ge=1, le=100000, description="Максимум элементов"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    check_auth(x_api_key)
    result = build_tree_dir(path or "", max_files)
    audit("tree_dir", {"path": path or "", "count": result.get("count")})
    return result

@app.get("/run/allowed")
def run_allowed(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    check_auth(x_api_key)
    current = mode()
    prefixes = []
    if current == "read_only":
        prefixes = []
    elif current == "autopilot":
        prefixes = list(AUTOPILOT_ALLOWED_PREFIXES)
    else:
        prefixes = ["* (полный режим: команды проходят жёсткую блокировку, но нет ограничения по префиксам)"]
    return {"mode": current, "allowedPrefixes": prefixes}




