"""KaroX 4.0 — Phase 2: binary files & images, file operations, unified-diff
apply_patch, working-tree checkpoints, search v2.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Header, HTTPException, Query
from pydantic import BaseModel, Field

import repo_tools as core
import karox4_core as k4
import karox4_exec as kexec

app = core.app

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
}
MAX_BYTES_CHUNK = 5_000_000
MAX_IMAGE_RAW = 8_000_000

# --------------------------------------------------------------------------
# Binary files
# --------------------------------------------------------------------------

@app.get("/bytes")
def read_bytes(
    path: str,
    offset: int = 0,
    length: int = 1_000_000,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    p = core.safe_path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    size = p.stat().st_size
    offset = max(0, int(offset))
    length = max(1, min(int(length), MAX_BYTES_CHUNK))
    with p.open("rb") as f:
        f.seek(offset)
        data = f.read(length)
    core.audit("read_bytes", {"path": path, "offset": offset, "length": len(data)})
    return {
        "path": core.rel_norm(path),
        "size": size,
        "offset": offset,
        "length": len(data),
        "eof": offset + len(data) >= size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "contentBase64": base64.b64encode(data).decode("ascii"),
    }


class WriteBytesBody(BaseModel):
    path: str
    contentBase64: str
    append: bool = False


@app.post("/bytes")
def write_bytes(
    body: WriteBytesBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /bytes")
    cached = k4.idem_lookup("POST /bytes", idempotency_key)
    if cached is not None:
        return cached
    if core.is_agent_helper_path(body.path):
        raise HTTPException(status_code=403, detail="Служебные helper-скрипты запрещены")
    p = core.safe_path(body.path, for_write=True)
    try:
        data = base64.b64decode(body.contentBase64, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Некорректный base64: {e}")
    if len(data) > core.MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Слишком большой объём")
    try:
        text = data.decode("utf-8")
        findings = scan_text_for_secrets(text)  # from karox4_git via late import fallback
        if findings:
            raise HTTPException(status_code=403, detail={"message": "Запись заблокирована сканером секретов", "findings": findings[:5]})
    except UnicodeDecodeError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("ab" if body.append else "wb") as f:
        f.write(data)
    core.audit("write_bytes", {"path": body.path, "bytes": len(data), "append": body.append})
    result = {"ok": True, "path": core.rel_norm(body.path), "bytes": len(data)}
    k4.idem_store("POST /bytes", idempotency_key, result)
    return result


def scan_text_for_secrets(text: str) -> List[Dict[str, Any]]:
    """Late-bound bridge to karox4_git scanner (import cycle safe)."""
    try:
        import karox4_git
        return karox4_git.scan_text_for_secrets(text)
    except Exception:
        return []


@app.get("/image")
def read_image(
    path: str,
    max_dimension: int = 1400,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    p = core.safe_path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    ext = p.suffix.lower()
    mime = MIME_BY_EXT.get(ext)
    if not mime:
        raise HTTPException(status_code=415, detail=f"Неподдерживаемое расширение изображения: {ext}")
    raw = p.read_bytes()
    width = height = None
    max_dimension = max(64, min(int(max_dimension), 4000))
    if ext != ".svg":
        try:
            from PIL import Image  # type: ignore
            img = Image.open(io.BytesIO(raw))
            width, height = img.size
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension))
                buf = io.BytesIO()
                img.convert("RGBA" if ext in (".png", ".webp", ".ico", ".bmp", ".gif") else "RGB").save(
                    buf, format="PNG" if ext != ".jpg" and ext != ".jpeg" else "JPEG")
                raw = buf.getvalue()
                mime = "image/png" if ext not in (".jpg", ".jpeg") else "image/jpeg"
                width, height = img.size
        except ImportError:
            if len(raw) > MAX_IMAGE_RAW:
                raise HTTPException(status_code=413, detail="Изображение слишком большое, а Pillow не установлен (pip install pillow)")
        except Exception:
            pass
    if len(raw) > MAX_IMAGE_RAW:
        raise HTTPException(status_code=413, detail="Изображение слишком большое после обработки")
    core.audit("read_image", {"path": path, "bytes": len(raw), "mime": mime})
    return {
        "path": core.rel_norm(path),
        "mimeType": mime,
        "width": width,
        "height": height,
        "bytes": len(raw),
        "contentBase64": base64.b64encode(raw).decode("ascii"),
    }

# --------------------------------------------------------------------------
# File operations: move / copy / mkdir / guarded delete_dir / glob
# --------------------------------------------------------------------------

class FsOpBody(BaseModel):
    op: Literal["move", "copy", "mkdir", "delete_dir"]
    src: Optional[str] = None
    dst: Optional[str] = None
    confirm: bool = False


class AllowDeleteDirBody(BaseModel):
    enabled: bool


@app.post("/fs/allow-delete-dir")
def allow_delete_dir(body: AllowDeleteDirBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    k4.SESSION_FLAGS["deleteDirAllowed"] = bool(body.enabled)
    core.audit("fs_allow_delete_dir", {"enabled": bool(body.enabled)})
    return {"ok": True, "deleteDirAllowed": k4.SESSION_FLAGS["deleteDirAllowed"]}


@app.post("/fs/op")
def fs_op(
    body: FsOpBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /fs/op")
    cached = k4.idem_lookup("POST /fs/op", idempotency_key)
    if cached is not None:
        return cached
    result: Dict[str, Any]
    if body.op == "mkdir":
        if not body.dst:
            raise HTTPException(status_code=400, detail="Нужен dst")
        p = core.safe_path(body.dst, for_write=True)
        p.mkdir(parents=True, exist_ok=True)
        result = {"ok": True, "op": "mkdir", "path": core.rel_norm(body.dst)}
    elif body.op in ("move", "copy"):
        if not body.src or not body.dst:
            raise HTTPException(status_code=400, detail="Нужны src и dst")
        src = core.safe_path(body.src, for_write=(body.op == "move"))
        dst = core.safe_path(body.dst, for_write=True)
        if not src.exists():
            raise HTTPException(status_code=404, detail=f"Источник не найден: {body.src}")
        if core.is_agent_helper_path(body.dst):
            raise HTTPException(status_code=403, detail="Служебные helper-скрипты запрещены")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if body.op == "move":
                shutil.move(str(src), str(dst))
            elif src.is_dir():
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
            else:
                shutil.copy2(str(src), str(dst))
        except OSError as e:
            raise HTTPException(status_code=409, detail=f"Операция не удалась: {e}")
        result = {"ok": True, "op": body.op, "src": core.rel_norm(body.src), "dst": core.rel_norm(body.dst)}
    elif body.op == "delete_dir":
        if not body.dst:
            raise HTTPException(status_code=400, detail="Нужен dst")
        if not k4.SESSION_FLAGS.get("deleteDirAllowed"):
            raise HTTPException(status_code=403, detail="Удаление директорий выключено. Сначала явный opt-in: POST /fs/allow-delete-dir {\"enabled\": true}")
        if not body.confirm:
            raise HTTPException(status_code=400, detail="Требуется confirm=true")
        p = core.safe_path(body.dst, for_write=True)
        resolved_root = core.REPO_ROOT.resolve()
        if p.resolve() == resolved_root:
            raise HTTPException(status_code=403, detail="Нельзя удалить корень репозитория")
        if not p.exists() or not p.is_dir():
            raise HTTPException(status_code=404, detail="Директория не найдена")
        shutil.rmtree(p)
        result = {"ok": True, "op": "delete_dir", "deleted": core.rel_norm(body.dst)}
    else:
        raise HTTPException(status_code=400, detail=f"Неизвестная операция: {body.op}")
    core.audit("fs_op", result)
    k4.idem_store("POST /fs/op", idempotency_key, result)
    return result


@app.get("/fs/glob")
def fs_glob(
    pattern: str,
    max_files: int = 200,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    max_files = max(1, min(int(max_files), 2000))
    out: List[Dict[str, Any]] = []
    for root, dirs, filenames in os.walk(core.REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in core.IGNORED_DIRS_FOR_TREE]
        for name in filenames:
            p = Path(root) / name
            rel = p.relative_to(core.REPO_ROOT).as_posix()
            if not fnmatch.fnmatch(rel, pattern) and not fnmatch.fnmatch(name, pattern):
                continue
            if core.is_sensitive_path(p):
                continue
            out.append({"path": rel, "size": p.stat().st_size})
            if len(out) >= max_files:
                return {"pattern": pattern, "files": out, "truncated": True}
    return {"pattern": pattern, "files": out, "truncated": False}

# --------------------------------------------------------------------------
# apply_patch — unified diff
# --------------------------------------------------------------------------

_PATCH_TARGET = re.compile(r"^\+\+\+\s+(?:b/)?(?P<path>[^\t\n]+)", re.MULTILINE)
_PATCH_SOURCE = re.compile(r"^---\s+(?:a/)?(?P<path>[^\t\n]+)", re.MULTILINE)


def patch_paths(patch: str) -> List[str]:
    paths: List[str] = []
    for rx in (_PATCH_TARGET, _PATCH_SOURCE):
        for m in rx.finditer(patch):
            p = m.group("path").strip()
            if p and p != "/dev/null" and p not in paths:
                paths.append(p)
    return paths


class ApplyPatchBody(BaseModel):
    patch: str
    stripLevel: int = Field(default=1, ge=0, le=3)
    checkOnly: bool = False


@app.post("/patch")
def apply_patch(
    body: ApplyPatchBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /patch")
    cached = k4.idem_lookup("POST /patch", idempotency_key)
    if cached is not None:
        return cached
    if not body.patch.strip():
        raise HTTPException(status_code=400, detail="Пустой патч")
    targets = patch_paths(body.patch)
    if not targets:
        raise HTTPException(status_code=400, detail="Не найдены целевые файлы в unified diff")
    for rel in targets:
        if core.is_agent_helper_path(rel):
            raise HTTPException(status_code=403, detail=f"Служебный helper-скрипт запрещён: {rel}")
        core.safe_path(rel, for_write=True, allow_generated=True)
    findings = scan_text_for_secrets(body.patch)
    added_findings = [f for f in findings if f.get("inAddedLine", True)]
    if added_findings:
        raise HTTPException(status_code=403, detail={"message": "Патч заблокирован сканером секретов", "findings": added_findings[:5]})
    tmp = core.RUNS_DIR / f"patch-{uuid.uuid4().hex[:10]}.diff"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(body.patch if body.patch.endswith("\n") else body.patch + "\n", encoding="utf-8")
    args = ["apply", f"-p{body.stripLevel}", "--whitespace=nowarn"]
    if body.checkOnly:
        args.append("--check")
    check = core.run_git(args + ["--check", str(tmp)] if not body.checkOnly else args + [str(tmp)], timeout=120)
    if check.get("exitCode") != 0:
        raise HTTPException(status_code=409, detail={"message": "Патч не применяется чисто", "gitApplyCheck": check})
    if body.checkOnly:
        result = {"ok": True, "checked": True, "files": targets}
    else:
        applied = core.run_git(args + [str(tmp)], timeout=180)
        if applied.get("exitCode") != 0:
            raise HTTPException(status_code=500, detail={"message": "git apply завершился с ошибкой", "result": applied})
        result = {"ok": True, "applied": True, "files": targets, "diffStat": core.run_git(["diff", "--stat"], timeout=120)}
    try:
        tmp.unlink()
    except OSError:
        pass
    core.audit("apply_patch", {"files": targets, "checkOnly": body.checkOnly})
    k4.idem_store("POST /patch", idempotency_key, result)
    return result

# --------------------------------------------------------------------------
# Working-tree checkpoints (no commits on the branch)
# --------------------------------------------------------------------------

CHECKPOINTS_FILE = k4.state_dir() / "checkpoints.json"
CHECKPOINTS: Dict[str, Dict[str, Any]] = {}


def _load_checkpoints() -> None:
    global CHECKPOINTS
    try:
        if CHECKPOINTS_FILE.exists():
            data = json.loads(CHECKPOINTS_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                CHECKPOINTS = data
    except Exception:
        CHECKPOINTS = {}


def _save_checkpoints() -> None:
    try:
        CHECKPOINTS_FILE.write_text(json.dumps(CHECKPOINTS, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


_load_checkpoints()


def _git_with_env(args: List[str], env_extra: Dict[str, str], timeout: int = 180) -> Dict[str, Any]:
    env = os.environ.copy()
    env.update(env_extra)
    started = time.time()
    try:
        r = subprocess.run(["git"] + args, cwd=str(core.REPO_ROOT), capture_output=True, timeout=timeout, env=env)
        return {
            "exitCode": r.returncode,
            "stdout": kexec.normalize_bytes(r.stdout),
            "stderr": kexec.normalize_bytes(r.stderr),
            "elapsedSeconds": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired:
        return {"exitCode": 408, "stdout": "", "stderr": "timeout", "timeout": True}


class CheckpointCreateBody(BaseModel):
    label: str = ""


@app.post("/checkpoint/create")
def checkpoint_create(body: CheckpointCreateBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /checkpoint/create")
    cp_id = "cp-" + uuid.uuid4().hex[:8]
    tmp_index = core.RUNS_DIR / f"cp-index-{cp_id}"
    tmp_index.parent.mkdir(parents=True, exist_ok=True)
    env = {"GIT_INDEX_FILE": str(tmp_index)}
    try:
        head = core.run_git(["rev-parse", "HEAD"], timeout=60)
        has_head = head.get("exitCode") == 0
        add = _git_with_env(["add", "-A", "."], env, timeout=300)
        if add["exitCode"] != 0:
            raise HTTPException(status_code=500, detail={"message": "Не удалось проиндексировать рабочее дерево", "result": add})
        tree = _git_with_env(["write-tree"], env, timeout=120)
        if tree["exitCode"] != 0:
            raise HTTPException(status_code=500, detail={"message": "write-tree не удался", "result": tree})
        tree_hash = tree["stdout"].strip()
        commit_args = ["commit-tree", tree_hash, "-m", f"karox checkpoint {cp_id}: {body.label or 'no label'}"]
        if has_head:
            commit_args = ["commit-tree", tree_hash, "-p", head["stdout"].strip(), "-m", f"karox checkpoint {cp_id}: {body.label or 'no label'}"]
        commit = _git_with_env(commit_args, env, timeout=120)
        if commit["exitCode"] != 0:
            raise HTTPException(status_code=500, detail={"message": "commit-tree не удался", "result": commit})
        commit_hash = commit["stdout"].strip()
        core.run_git(["update-ref", f"refs/karox/checkpoints/{cp_id}", commit_hash], timeout=60)
    finally:
        try:
            tmp_index.unlink()
        except OSError:
            pass
    CHECKPOINTS[cp_id] = {
        "id": cp_id,
        "label": body.label,
        "hash": commit_hash,
        "branch": core.current_branch(),
        "createdAt": core.now(),
    }
    _save_checkpoints()
    core.audit("checkpoint_create", CHECKPOINTS[cp_id])
    return {"ok": True, "checkpoint": CHECKPOINTS[cp_id]}


@app.get("/checkpoint/list")
def checkpoint_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    return {"checkpoints": sorted(CHECKPOINTS.values(), key=lambda c: c.get("createdAt") or "", reverse=True)}


class CheckpointRestoreBody(BaseModel):
    checkpointId: str
    deleteNewFiles: bool = False


@app.post("/checkpoint/restore")
def checkpoint_restore(body: CheckpointRestoreBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /checkpoint/restore")
    cp = CHECKPOINTS.get(body.checkpointId)
    if not cp:
        raise HTTPException(status_code=404, detail=f"Чекпоинт не найден: {body.checkpointId}")
    commit_hash = cp["hash"]
    restore = core.run_git(["checkout", commit_hash, "--", "."], timeout=300)
    if restore.get("exitCode") != 0:
        raise HTTPException(status_code=500, detail={"message": "Восстановление не удалось", "result": restore})
    core.run_git(["reset", "--"], timeout=120)
    deleted: List[str] = []
    kept: List[str] = []
    if body.deleteNewFiles:
        snapshot_r = core.run_git(["ls-tree", "-r", "--name-only", commit_hash], timeout=120)
        snapshot = set((snapshot_r.get("stdout") or "").splitlines())
        for item in core.changed_files_porcelain():
            if not item["status"].startswith("??"):
                continue
            rel = item["path"]
            if rel in snapshot:
                continue
            try:
                p = core.safe_path(rel, for_write=True, allow_generated=True)
            except HTTPException:
                kept.append(rel)
                continue
            if p.is_file():
                p.unlink()
                deleted.append(rel)
            elif p.is_dir():
                shutil.rmtree(p)
                deleted.append(rel + "/")
            else:
                kept.append(rel)
    status = core.run_git(["status", "--short", "--branch"], timeout=120)
    core.audit("checkpoint_restore", {"id": body.checkpointId, "deleted": deleted, "kept": kept})
    return {"ok": True, "restoredFrom": cp, "deletedNewFiles": deleted, "keptNewFiles": kept, "status": status}

# --------------------------------------------------------------------------
# Search v2: regex, filename search, size/type limits
# --------------------------------------------------------------------------

@app.get("/search/v2")
def search_v2(
    q: str = Query(..., min_length=1),
    regex: bool = False,
    names_only: bool = False,
    glob: str = "*",
    extensions: Optional[str] = None,
    max_size: int = 2_000_000,
    max_files: int = 100,
    context: int = 0,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    max_files = max(1, min(int(max_files), 500))
    max_size = max(1024, min(int(max_size), core.MAX_FILE_SIZE))
    context = max(0, min(int(context), 5))
    exts = {e.strip().lower().lstrip(".") for e in (extensions or "").split(",") if e.strip()}
    compiled: Optional[re.Pattern] = None
    if regex:
        try:
            compiled = re.compile(q)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Некорректный regex: {e}")
    results: List[Dict[str, Any]] = []
    scanned = 0
    for root, dirs, filenames in os.walk(core.REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in core.IGNORED_DIRS_FOR_TREE]
        for name in filenames:
            if len(results) >= max_files:
                return {"q": q, "files": results, "scanned": scanned, "truncated": True}
            p = Path(root) / name
            rel = p.relative_to(core.REPO_ROOT).as_posix()
            if core.is_sensitive_path(p):
                continue
            if exts and p.suffix.lower().lstrip(".") not in exts:
                continue
            if glob != "*" and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob)):
                continue
            name_hit = (compiled.search(rel) if compiled else (q.lower() in rel.lower()))
            if names_only:
                if name_hit:
                    results.append({"path": rel, "size": p.stat().st_size, "matchType": "name"})
                continue
            if p.stat().st_size > max_size:
                continue
            scanned += 1
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            matches: List[Dict[str, Any]] = []
            lines = text.splitlines()
            for i, line in enumerate(lines, start=1):
                hit = compiled.search(line) if compiled else (q in line)
                if hit:
                    snippet_lines = lines[max(0, i - 1 - context): i + context]
                    matches.append({"line": i, "snippet": "\n".join(snippet_lines)[:800]})
                    if len(matches) >= 5:
                        break
            if matches:
                results.append({"path": rel, "size": p.stat().st_size, "matchType": "content", "matches": matches})
            elif name_hit:
                results.append({"path": rel, "size": p.stat().st_size, "matchType": "name"})
    core.audit("search_v2", {"q": q[:200], "regex": regex, "results": len(results)})
    return {"q": q, "files": results, "scanned": scanned, "truncated": False}
