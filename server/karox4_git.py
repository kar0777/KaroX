"""KaroX 4.0 — Phase 3: full local git cycle (push stays hard-blocked),
secret-scan v2 with line numbers + entropy on write & commit, hunk commits.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Header, HTTPException, Query
from pydantic import BaseModel, Field

import repo_tools as core
import karox4_core as k4

app = core.app

# --------------------------------------------------------------------------
# Secret scan v2: token regexes + entropy, with line numbers
# --------------------------------------------------------------------------
# NOTE: markers are assembled by concatenation so this file never triggers
# its own scanner when committed.

_SECRET_PATTERNS: List[tuple] = [
    ("private_key", re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh" + r"[pousr]_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bgithub" + r"_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA" + r"[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox" + r"[baprs]-[A-Za-z0-9-]{10,}")),
    ("openai_style_key", re.compile(r"\bsk-" + r"[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza" + r"[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"\beyJ" + r"[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]
_ASSIGNMENT_RX = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|passwd|password|credential)\b\s*[:=]\s*[\"']([A-Za-z0-9+/=_-]{16,})[\"']"
)


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def scan_text_for_secrets(text: str, max_findings: int = 20) -> List[Dict[str, Any]]:
    """Return findings with 1-based line numbers."""
    findings: List[Dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if len(findings) >= max_findings:
            break
        for kind, rx in _SECRET_PATTERNS:
            m = rx.search(line)
            if m:
                token = m.group(0)
                findings.append({
                    "kind": kind,
                    "line": line_no,
                    "preview": (token[:6] + "\u2026" + token[-4:]) if len(token) > 14 else "***",
                    "inAddedLine": not line.startswith("-"),
                })
                break
        else:
            m = _ASSIGNMENT_RX.search(line)
            if m:
                candidate = m.group(2)
                if shannon_entropy(candidate) >= 3.8:
                    findings.append({
                        "kind": "high_entropy_assignment",
                        "line": line_no,
                        "preview": m.group(1) + "=***",
                        "entropy": round(shannon_entropy(candidate), 2),
                        "inAddedLine": not line.startswith("-"),
                    })
    return findings


def scan_secret_content_v2(path: Path) -> Optional[str]:
    """Drop-in upgrade for core.scan_secret_content: reports line numbers."""
    if not path.exists() or not path.is_file():
        return None
    if path.stat().st_size > 2_000_000:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    findings = scan_text_for_secrets(text, max_findings=5)
    if not findings:
        return None
    parts = [f"строка {f['line']}: {f['kind']} ({f.get('preview', '***')})" for f in findings]
    return "Обнаружены возможные секреты: " + "; ".join(parts)


# Upgrade the commit-time scanner globally (git_commit looks it up at call time).
core.scan_secret_content = scan_secret_content_v2


# Scan-on-write: wrap POST /file and /files/batch-write.

def _write_file_v3(
    body: core.WriteFileBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    cached = k4.idem_lookup("POST /file", idempotency_key)
    if cached is not None:
        return cached
    findings = scan_text_for_secrets(body.content, max_findings=5)
    if findings:
        core.audit("write_blocked_secret", {"path": body.path, "findings": findings})
        raise HTTPException(status_code=403, detail={
            "message": f"Запись {body.path} заблокирована сканером секретов",
            "findings": findings,
        })
    result = core.write_file(body, x_api_key)
    k4.idem_store("POST /file", idempotency_key, result)
    return result


def _batch_write_v3(
    body: core.BatchWriteBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    cached = k4.idem_lookup("POST /files/batch-write", idempotency_key)
    if cached is not None:
        return cached
    for item in body.files:
        findings = scan_text_for_secrets(item.content, max_findings=5)
        if findings:
            core.audit("write_blocked_secret", {"path": item.path, "findings": findings})
            raise HTTPException(status_code=403, detail={
                "message": f"Запись {item.path} заблокирована сканером секретов",
                "findings": findings,
            })
    result = core.batch_write(body, x_api_key)
    k4.idem_store("POST /files/batch-write", idempotency_key, result)
    return result


k4.swap_route("POST", "/file", _write_file_v3)
k4.swap_route("POST", "/files/batch-write", _batch_write_v3)


class ScanBody(BaseModel):
    text: str


@app.post("/secrets/scan")
def secrets_scan(body: ScanBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    return {"findings": scan_text_for_secrets(body.text)}

# --------------------------------------------------------------------------
# Branches / stash / log / show / blame / restore / diff
# --------------------------------------------------------------------------

def _require_writable(action: str) -> None:
    core.ensure_not_read_only(action)


class BranchBody(BaseModel):
    action: Literal["create", "switch", "list"]
    name: Optional[str] = None
    stashFirst: bool = False


@app.post("/git/v2/branch")
def git_branch(body: BranchBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    if body.action == "list":
        r = core.run_git(["branch", "--list", "--format=%(refname:short)"], timeout=60)
        return {"branches": [b for b in (r.get("stdout") or "").splitlines() if b.strip()], "current": core.current_branch()}
    _require_writable("POST /git/v2/branch")
    if not body.name or not re.fullmatch(r"[\w./-]{1,120}", body.name):
        raise HTTPException(status_code=400, detail="Некорректное имя ветки")
    stash_result = None
    if body.action == "switch" and body.stashFirst and core.changed_files_porcelain():
        stash_result = core.run_git(["stash", "push", "--include-untracked", "-m", f"karox auto-stash before switch to {body.name}"], timeout=180)
    if body.action == "create":
        r = core.run_git(["switch", "-c", body.name], timeout=120)
    else:
        r = core.run_git(["switch", body.name], timeout=120)
    if r.get("exitCode") != 0:
        raise HTTPException(status_code=409, detail={"message": "Переключение ветки не удалось", "result": r, "stash": stash_result})
    core.TASK_STATE["branch"] = core.current_branch()
    k4.save_session_state()
    core.audit("git_branch", {"action": body.action, "name": body.name})
    note = None
    if not core.current_branch().startswith("promptql/"):
        note = "Внимание: коммиты разрешены только на ветках promptql/*"
    return {"ok": True, "current": core.current_branch(), "stash": stash_result, "note": note}


class StashBody(BaseModel):
    action: Literal["push", "pop", "list"]
    message: Optional[str] = None
    includeUntracked: bool = True


@app.post("/git/v2/stash")
def git_stash(body: StashBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    if body.action == "list":
        return core.run_git(["stash", "list"], timeout=60)
    _require_writable("POST /git/v2/stash")
    if body.action == "push":
        args = ["stash", "push"]
        if body.includeUntracked:
            args.append("--include-untracked")
        if body.message:
            args += ["-m", body.message]
        r = core.run_git(args, timeout=300)
    else:
        r = core.run_git(["stash", "pop"], timeout=300)
    core.audit("git_stash", {"action": body.action, "exitCode": r.get("exitCode")})
    if r.get("exitCode") != 0:
        raise HTTPException(status_code=409, detail={"message": "stash не удался", "result": r})
    return {"ok": True, "result": r}


@app.get("/git/v2/log")
def git_log(
    max_count: int = 20,
    author: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    path: Optional[str] = None,
    grep: Optional[str] = None,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    args = ["log", f"--max-count={max(1, min(int(max_count), 200))}", "--date=iso", "--pretty=format:%h%x09%an%x09%ad%x09%s"]
    if author:
        args.append(f"--author={author}")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if grep:
        args.append(f"--grep={grep}")
    if path:
        core.safe_path(path)
        args += ["--", core.rel_norm(path)]
    r = core.run_git(args, timeout=120)
    commits = []
    for line in (r.get("stdout") or "").splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"commits": commits, "exitCode": r.get("exitCode")}


@app.get("/git/v2/show")
def git_show(
    rev: str,
    path: Optional[str] = None,
    max_chars: int = Query(200000, ge=1000, le=1000000),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    if not re.fullmatch(r"[\w./~^{}-]{1,120}", rev):
        raise HTTPException(status_code=400, detail="Некорректная ревизия")
    target = rev if not path else f"{rev}:{core.rel_norm(path)}"
    if path:
        core.safe_path(path)
    r = core.run_git(["show", target], timeout=180)
    out = r.get("stdout", "")
    r["stdout"] = out[-max_chars:]
    r["truncated"] = len(out) > max_chars
    return r


@app.get("/git/v2/blame")
def git_blame(
    path: str,
    start_line: int = 1,
    end_line: int = 0,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    core.safe_path(path)
    args = ["blame", "--date=short"]
    if end_line and end_line >= start_line:
        args += ["-L", f"{max(1, start_line)},{end_line}"]
    args += ["--", core.rel_norm(path)]
    return core.run_git(args, timeout=180)


class RestoreV2Body(BaseModel):
    paths: List[str]
    source: Optional[str] = None


@app.post("/git/v2/restore")
def git_restore_v2(body: RestoreV2Body, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    _require_writable("POST /git/v2/restore")
    core.require_promptql_branch()
    if not body.paths:
        raise HTTPException(status_code=400, detail="Не указаны пути")
    restored = []
    for raw in body.paths:
        rel = core.rel_norm(raw)
        core.safe_path(rel, for_write=True, allow_generated=True)
        args = ["restore"]
        if body.source:
            if not re.fullmatch(r"[\w./~^{}-]{1,120}", body.source):
                raise HTTPException(status_code=400, detail="Некорректный source")
            args.append(f"--source={body.source}")
        args += ["--", rel]
        r = core.run_git(args, timeout=120)
        restored.append({"path": rel, "exitCode": r.get("exitCode"), "stderr": r.get("stderr", "")[:300]})
    core.audit("git_restore_v2", {"restored": restored, "source": body.source})
    return {"ok": all(item["exitCode"] == 0 for item in restored), "restored": restored}


@app.get("/git/v2/diff")
def git_diff_revs(
    from_rev: str,
    to_rev: str = "",
    path: Optional[str] = None,
    max_chars: int = Query(300000, ge=1000, le=1000000),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    for rev in (from_rev, to_rev):
        if rev and not re.fullmatch(r"[\w./~^{}-]{1,120}", rev):
            raise HTTPException(status_code=400, detail=f"Некорректная ревизия: {rev}")
    args = ["diff", f"{from_rev}..{to_rev}" if to_rev else from_rev]
    if path:
        core.safe_path(path)
        args += ["--", core.rel_norm(path)]
    r = core.run_git(args, timeout=300)
    out = r.get("stdout", "")
    r["stdout"] = out[-max_chars:]
    r["truncated"] = len(out) > max_chars
    return r

# --------------------------------------------------------------------------
# Local merge / rebase with conflict report (no push, ever)
# --------------------------------------------------------------------------

class MergeBody(BaseModel):
    branch: str
    abortOnConflict: bool = True


def _conflicts() -> List[str]:
    r = core.run_git(["diff", "--name-only", "--diff-filter=U"], timeout=120)
    return [line for line in (r.get("stdout") or "").splitlines() if line.strip()]


@app.post("/git/v2/merge")
def git_merge(body: MergeBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    _require_writable("POST /git/v2/merge")
    core.require_promptql_branch()
    if not re.fullmatch(r"[\w./-]{1,120}", body.branch):
        raise HTTPException(status_code=400, detail="Некорректное имя ветки")
    r = core.run_git(["merge", "--no-edit", body.branch], timeout=300)
    conflicts = _conflicts()
    aborted = False
    if conflicts and body.abortOnConflict:
        core.run_git(["merge", "--abort"], timeout=120)
        aborted = True
    core.audit("git_merge", {"branch": body.branch, "exitCode": r.get("exitCode"), "conflicts": conflicts, "aborted": aborted})
    return {"ok": r.get("exitCode") == 0, "result": r, "conflicts": conflicts, "aborted": aborted}


class RebaseBody(BaseModel):
    onto: str
    abortOnConflict: bool = True


@app.post("/git/v2/rebase")
def git_rebase(body: RebaseBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    _require_writable("POST /git/v2/rebase")
    core.require_promptql_branch()
    if not re.fullmatch(r"[\w./-]{1,120}", body.onto):
        raise HTTPException(status_code=400, detail="Некорректное имя ветки")
    r = core.run_git(["rebase", body.onto], timeout=600)
    conflicts = _conflicts()
    aborted = False
    if conflicts and body.abortOnConflict:
        core.run_git(["rebase", "--abort"], timeout=120)
        aborted = True
    core.audit("git_rebase", {"onto": body.onto, "exitCode": r.get("exitCode"), "conflicts": conflicts, "aborted": aborted})
    return {"ok": r.get("exitCode") == 0, "result": r, "conflicts": conflicts, "aborted": aborted}


class AbortBody(BaseModel):
    op: Literal["merge", "rebase"]


@app.post("/git/v2/abort")
def git_abort(body: AbortBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    _require_writable("POST /git/v2/abort")
    r = core.run_git([body.op, "--abort"], timeout=120)
    core.audit("git_abort", {"op": body.op, "exitCode": r.get("exitCode")})
    return {"ok": r.get("exitCode") == 0, "result": r}

# --------------------------------------------------------------------------
# Partial commit by hunks
# --------------------------------------------------------------------------

def split_hunks(diff_text: str) -> tuple[str, List[str]]:
    header_lines: List[str] = []
    hunks: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("@@"):
            current = [line]
            hunks.append(current)
        elif current is None:
            header_lines.append(line)
        else:
            current.append(line)
    return "".join(header_lines), ["".join(h) for h in hunks]


class CommitHunksBody(BaseModel):
    path: str
    hunkIndexes: List[int]
    message: str


@app.post("/git/v2/commit-hunks")
def git_commit_hunks(
    body: CommitHunksBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    _require_writable("POST /git/v2/commit-hunks")
    cached = k4.idem_lookup("POST /git/v2/commit-hunks", idempotency_key)
    if cached is not None:
        return cached
    if core.mode() == "autopilot" and not bool(core.TASK_STATE.get("commitAllowed")):
        raise HTTPException(status_code=403, detail="Git commit запрещён в режиме autopilot для этой сессии")
    br = core.require_promptql_branch()
    rel = core.rel_norm(body.path)
    safe = core.safe_path(rel)
    if core.is_sensitive_path(safe) or core.path_matches(rel, core.GENERATED_PATTERNS):
        raise HTTPException(status_code=403, detail=f"Путь не может быть закоммичен: {rel}")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Пустое сообщение коммита")
    diff = core.run_git(["diff", "--", rel], timeout=240)
    header, hunks = split_hunks(diff.get("stdout") or "")
    if not hunks:
        raise HTTPException(status_code=400, detail="Нет изменений (hunks) в этом файле")
    selected: List[str] = []
    for index in body.hunkIndexes:
        if index < 0 or index >= len(hunks):
            raise HTTPException(status_code=400, detail=f"Нет hunk с индексом {index} (всего {len(hunks)})")
        selected.append(hunks[index])
    partial_patch = header + "".join(selected)
    findings = scan_text_for_secrets(partial_patch, max_findings=5)
    added = [f for f in findings if f.get("inAddedLine")]
    if added:
        raise HTTPException(status_code=403, detail={"message": "Коммит заблокирован сканером секретов", "findings": added})
    import uuid as _uuid
    tmp = core.RUNS_DIR / f"hunks-{_uuid.uuid4().hex[:10]}.diff"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(partial_patch if partial_patch.endswith("\n") else partial_patch + "\n", encoding="utf-8")
    core.run_git(["reset", "--"], timeout=120)
    applied = core.run_git(["apply", "--cached", "--whitespace=nowarn", str(tmp)], timeout=180)
    try:
        tmp.unlink()
    except OSError:
        pass
    if applied.get("exitCode") != 0:
        raise HTTPException(status_code=409, detail={"message": "Не удалось застейджить выбранные hunks", "result": applied})
    cached_files = core.run_git(["diff", "--cached", "--name-only"], timeout=120)
    staged = [core.rel_norm(x) for x in (cached_files.get("stdout") or "").splitlines() if x.strip()]
    if staged != [rel]:
        core.run_git(["reset", "--"], timeout=120)
        raise HTTPException(status_code=403, detail=f"Неожиданные staged файлы: {staged}")
    commit_result = core.run_git(["commit", "-m", message], timeout=300)
    if commit_result.get("exitCode") != 0:
        raise HTTPException(status_code=500, detail={"message": "Коммит не удался", "result": commit_result})
    hash_result = core.run_git(["rev-parse", "--short", "HEAD"], timeout=60)
    result = {
        "ok": True,
        "branch": br,
        "path": rel,
        "hunksCommitted": body.hunkIndexes,
        "hunksTotal": len(hunks),
        "hash": (hash_result.get("stdout") or "").strip(),
        "status": core.run_git(["status", "--short", "--branch"], timeout=120),
    }
    core.audit("git_commit_hunks", {"path": rel, "hunks": body.hunkIndexes, "hash": result["hash"]})
    k4.idem_store("POST /git/v2/commit-hunks", idempotency_key, result)
    return result


@app.get("/git/v2/hunks")
def git_list_hunks(path: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    rel = core.rel_norm(path)
    core.safe_path(rel)
    diff = core.run_git(["diff", "--", rel], timeout=240)
    header, hunks = split_hunks(diff.get("stdout") or "")
    return {
        "path": rel,
        "count": len(hunks),
        "hunks": [{"index": i, "header": h.splitlines()[0] if h else "", "preview": h[:1500]} for i, h in enumerate(hunks)],
    }
