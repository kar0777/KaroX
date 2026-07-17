"""KaroX 4.0 — Phase 7: auto project map, per-repo persistent memos,
multi-repo workspace registry (switch is opt-in via env).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel

import repo_tools as core
import karox4_core as k4

app = core.app

# --------------------------------------------------------------------------
# Project map
# --------------------------------------------------------------------------

_PROJECT_MAP_CACHE: Dict[str, Any] = {"builtAt": 0.0, "map": None, "root": None}

_MARKERS: List[tuple] = [
    ("package.json", "web/node"),
    ("pnpm-workspace.yaml", "web/node-monorepo"),
    ("build.gradle", "jvm/gradle"),
    ("build.gradle.kts", "jvm/gradle"),
    ("pom.xml", "jvm/maven"),
    ("Cargo.toml", "rust"),
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("go.mod", "go"),
]


def _detect_commands(root: Path, kinds: List[str]) -> Dict[str, Optional[str]]:
    commands: Dict[str, Optional[str]] = {"build": None, "test": None, "run": None}
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = data.get("scripts") or {}
            if "build" in scripts:
                commands["build"] = "npm run build"
            if "test" in scripts:
                commands["test"] = "npm test"
            for candidate in ("dev", "start", "serve"):
                if candidate in scripts:
                    commands["run"] = f"npm run {candidate}"
                    break
        except Exception:
            pass
    if any(k.startswith("jvm/gradle") for k in kinds):
        gradle = "gradlew" if (root / "gradlew").exists() else "gradle"
        commands["build"] = commands["build"] or f"{gradle} build"
        commands["test"] = commands["test"] or f"{gradle} test"
        commands["run"] = commands["run"] or (f"{gradle} runClient" if (root / "src" / "main" / "resources" / "fabric.mod.json").exists() else f"{gradle} run")
    if "jvm/maven" in kinds:
        commands["build"] = commands["build"] or "mvn package"
        commands["test"] = commands["test"] or "mvn test"
    if "rust" in kinds:
        commands["build"] = commands["build"] or "cargo build"
        commands["test"] = commands["test"] or "cargo test"
        commands["run"] = commands["run"] or "cargo run"
    if "python" in kinds:
        if (root / "pyproject.toml").exists():
            commands["test"] = commands["test"] or "pytest"
        elif (root / "requirements.txt").exists():
            commands["test"] = commands["test"] or "python -m pytest"
    return commands


def _entry_points(root: Path) -> List[str]:
    entries: List[str] = []
    for candidate in (
        "src/index.ts", "src/index.js", "src/main.ts", "src/main.tsx", "src/App.tsx",
        "main.py", "app.py", "server.py", "manage.py",
        "src/main/java", "src/main/kotlin", "src/main.rs", "cmd",
        "index.html", "server/app_entry.py",
    ):
        if (root / candidate).exists():
            entries.append(candidate)
    return entries[:10]


def build_project_map(force: bool = False) -> Dict[str, Any]:
    root = core.REPO_ROOT
    if (
        not force
        and _PROJECT_MAP_CACHE["map"] is not None
        and _PROJECT_MAP_CACHE["root"] == str(root)
        and time.time() - _PROJECT_MAP_CACHE["builtAt"] < 600
    ):
        return _PROJECT_MAP_CACHE["map"]
    kinds = [kind for marker, kind in _MARKERS if (root / marker).exists()]
    if (root / "src" / "main" / "resources" / "fabric.mod.json").exists() or (root / "mods.toml").exists():
        kinds.append("minecraft-mod")
    if not kinds:
        kinds = ["generic"]
    top_dirs = []
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name not in core.IGNORED_DIRS_FOR_TREE and not child.name.startswith("."):
                count = sum(1 for _ in child.rglob("*") if _.is_file()) if child.name != "node_modules" else -1
                top_dirs.append({"name": child.name, "files": count})
            if len(top_dirs) >= 20:
                break
    except OSError:
        pass
    project_map = {
        "root": str(root),
        "kinds": kinds,
        "entryPoints": _entry_points(root),
        "commands": _detect_commands(root, kinds),
        "topLevelDirs": top_dirs,
        "builtAt": core.now(),
    }
    _PROJECT_MAP_CACHE.update({"builtAt": time.time(), "map": project_map, "root": str(root)})
    return project_map


@app.get("/context/project-map")
def project_map_endpoint(refresh: bool = False, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    return build_project_map(force=refresh)

# --------------------------------------------------------------------------
# Per-repo persistent memos (survive restarts and sessions)
# --------------------------------------------------------------------------

def _memo_file() -> Path:
    repo_hash = hashlib.sha256(str(core.REPO_ROOT.resolve()).encode("utf-8")).hexdigest()[:16]
    memos_dir = k4.karox_home() / "memos"
    memos_dir.mkdir(parents=True, exist_ok=True)
    return memos_dir / f"{repo_hash}.json"


def _load_memos() -> Dict[str, Any]:
    f = _memo_file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_memos(memos: Dict[str, Any]) -> None:
    _memo_file().write_text(json.dumps(memos, ensure_ascii=False, indent=1), encoding="utf-8")


class MemoSetBody(BaseModel):
    key: str
    value: str


@app.post("/memo/set")
def memo_set(body: MemoSetBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    if not body.key or len(body.key) > 200:
        raise HTTPException(status_code=400, detail="Некорректный ключ")
    if len(body.value) > 20000:
        raise HTTPException(status_code=413, detail="Значение слишком длинное (макс. 20000)")
    memos = _load_memos()
    memos[body.key] = {"value": body.value, "updatedAt": core.now()}
    if len(memos) > 500:
        oldest = sorted(memos.items(), key=lambda kv: kv[1].get("updatedAt", ""))[: len(memos) - 500]
        for key, _ in oldest:
            memos.pop(key, None)
    _save_memos(memos)
    core.audit("memo_set", {"key": body.key})
    return {"ok": True, "key": body.key}


@app.get("/memo/get")
def memo_get(key: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    memos = _load_memos()
    entry = memos.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Заметка не найдена: {key}")
    return {"key": key, **entry}


@app.get("/memo/list")
def memo_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    memos = _load_memos()
    return {"memos": [{"key": k, "updatedAt": v.get("updatedAt"), "preview": (v.get("value") or "")[:120]} for k, v in sorted(memos.items())]}


class MemoDeleteBody(BaseModel):
    key: str


@app.post("/memo/delete")
def memo_delete(body: MemoDeleteBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    memos = _load_memos()
    existed = memos.pop(body.key, None) is not None
    _save_memos(memos)
    core.audit("memo_delete", {"key": body.key, "existed": existed})
    return {"ok": True, "deleted": existed}

# --------------------------------------------------------------------------
# Multi-repo workspaces (switch requires explicit env opt-in)
# --------------------------------------------------------------------------

def _workspaces_file() -> Path:
    home = k4.karox_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / "workspaces.json"


def _load_workspaces() -> Dict[str, str]:
    f = _workspaces_file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


@app.get("/workspace/list")
def workspace_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    workspaces = _load_workspaces()
    return {
        "current": str(core.REPO_ROOT),
        "workspaces": [{"name": k, "path": v} for k, v in sorted(workspaces.items())],
        "switchEnabled": os.environ.get("KAROX_ALLOW_WORKSPACE_SWITCH") == "1",
    }


class WorkspaceRegisterBody(BaseModel):
    name: str
    path: str


@app.post("/workspace/register")
def workspace_register(body: WorkspaceRegisterBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    p = Path(body.path).resolve()
    if not p.is_dir() or not (p / ".git").exists():
        raise HTTPException(status_code=400, detail="Путь должен быть существующим git-репозиторием")
    if not body.name or len(body.name) > 60:
        raise HTTPException(status_code=400, detail="Некорректное имя")
    workspaces = _load_workspaces()
    workspaces[body.name] = str(p)
    _workspaces_file().write_text(json.dumps(workspaces, ensure_ascii=False, indent=1), encoding="utf-8")
    core.audit("workspace_register", {"name": body.name, "path": str(p)})
    return {"ok": True, "name": body.name, "path": str(p)}


class WorkspaceSwitchBody(BaseModel):
    name: str


@app.post("/workspace/switch")
def workspace_switch(body: WorkspaceSwitchBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /workspace/switch")
    if os.environ.get("KAROX_ALLOW_WORKSPACE_SWITCH") != "1":
        raise HTTPException(status_code=403, detail="Переключение репозиториев выключено. Включите переменной окружения KAROX_ALLOW_WORKSPACE_SWITCH=1 при запуске сервера")
    workspaces = _load_workspaces()
    target = workspaces.get(body.name)
    if not target:
        raise HTTPException(status_code=404, detail=f"Репозиторий не зарегистрирован: {body.name}")
    p = Path(target).resolve()
    if not p.is_dir() or not (p / ".git").exists():
        raise HTTPException(status_code=410, detail=f"Путь больше не является git-репозиторием: {target}")
    previous = str(core.REPO_ROOT)
    core.REPO_ROOT = p
    _PROJECT_MAP_CACHE["map"] = None
    core.audit("workspace_switch", {"from": previous, "to": str(p)})
    k4.emit_event("workspace_switched", {"from": previous, "to": str(p)})
    return {"ok": True, "previous": previous, "current": str(p), "projectMap": build_project_map(force=True)}
