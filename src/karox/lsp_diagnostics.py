"""Bounded read-only Language Server Protocol diagnostics.

KaroX does not accept an arbitrary language-server command from a model.  A
small built-in mapping selects an already-installed executable from the file
extension, starts it with stdio in the repository, opens exactly one confined
UTF-8 file, collects ``textDocument/publishDiagnostics``, then shuts it down.

This module performs no repository mutation and stores no server process beyond
one bounded call.  Capability enforcement belongs to :mod:`karox.core`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import IO, Any, Optional
from urllib.parse import quote

from .security import child_process_environment

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_MESSAGE_BYTES = 5 * 1024 * 1024
_MAX_DIAGNOSTICS = 200


class LspDiagnosticsError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class LanguageServerSpec:
    name: str
    executable: str
    arguments: tuple[str, ...]
    extensions: tuple[str, ...]
    language_id: str


_SPECS: tuple[LanguageServerSpec, ...] = (
    LanguageServerSpec("pyright", "pyright-langserver", ("--stdio",), (".py", ".pyi"), "python"),
    LanguageServerSpec(
        "typescript",
        "typescript-language-server",
        ("--stdio",),
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        "typescript",
    ),
    LanguageServerSpec("rust-analyzer", "rust-analyzer", (), (".rs",), "rust"),
    LanguageServerSpec("gopls", "gopls", (), (".go",), "go"),
)

_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def _uri(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt" and not value.startswith("/"):
        value = "/" + value
    return "file://" + quote(value, safe="/:@")


def _confined(repository: Path, raw: str) -> Path:
    candidate = (repository / Path(raw)).resolve(strict=True)
    try:
        candidate.relative_to(repository)
    except ValueError as exc:
        raise LspDiagnosticsError("diagnostic path escapes the repository") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise LspDiagnosticsError("diagnostic path must be a regular non-link file")
    return candidate


def _spec_for(path: Path) -> tuple[LanguageServerSpec, str]:
    suffix = path.suffix.casefold()
    for spec in _SPECS:
        if suffix not in spec.extensions:
            continue
        executable = shutil.which(spec.executable)
        if executable:
            # JavaScript variants need their own language IDs even though the
            # same server handles them.
            language = spec.language_id
            if spec.name == "typescript":
                language = {
                    ".js": "javascript",
                    ".jsx": "javascriptreact",
                    ".tsx": "typescriptreact",
                }.get(suffix, "typescript")
            return spec, language
        raise LspDiagnosticsError(
            f"{spec.name} is the configured language server for {suffix}, but {spec.executable} is not installed"
        )
    raise LspDiagnosticsError(f"no built-in language server is configured for {suffix or '<no extension>'}")


def available_language_servers() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for spec in _SPECS:
        executable = shutil.which(spec.executable)
        rows.append(
            {
                "name": spec.name,
                "executable": executable,
                "available": bool(executable),
                "extensions": list(spec.extensions),
            }
        )
    return tuple(rows)


def _write_message(stream: IO[bytes], payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > _MAX_MESSAGE_BYTES:
        raise LspDiagnosticsError("outbound LSP message exceeds the safety limit")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


def _reader(stream: IO[bytes], output: "queue.Queue[object]") -> None:
    try:
        while True:
            headers: dict[str, str] = {}
            while True:
                line = stream.readline()
                if not line:
                    output.put(EOFError("language server closed stdout"))
                    return
                if line in {b"\r\n", b"\n"}:
                    break
                try:
                    decoded = line.decode("ascii", errors="strict").strip()
                except UnicodeDecodeError:
                    output.put(LspDiagnosticsError("language server returned non-ASCII LSP headers"))
                    return
                if ":" not in decoded:
                    output.put(LspDiagnosticsError("language server returned malformed LSP headers"))
                    return
                key, value = decoded.split(":", 1)
                headers[key.casefold().strip()] = value.strip()
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                output.put(LspDiagnosticsError("language server returned invalid Content-Length"))
                return
            if not 0 < length <= _MAX_MESSAGE_BYTES:
                output.put(LspDiagnosticsError("language server message exceeds the safety limit"))
                return
            body = stream.read(length)
            if len(body) != length:
                output.put(EOFError("language server closed mid-message"))
                return
            try:
                value = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                output.put(LspDiagnosticsError("language server returned invalid JSON"))
                return
            if isinstance(value, dict):
                output.put(value)
    except BaseException as exc:  # pragma: no cover - defensive reader fence
        output.put(exc)


def _diagnostic_rows(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items[:_MAX_DIAGNOSTICS]:
        if not isinstance(item, dict):
            continue
        raw_range = item.get("range")
        range_value: dict[str, Any] = raw_range if isinstance(raw_range, dict) else {}
        raw_start = range_value.get("start")
        raw_end = range_value.get("end")
        start: dict[str, Any] = raw_start if isinstance(raw_start, dict) else {}
        end: dict[str, Any] = raw_end if isinstance(raw_end, dict) else {}
        severity_raw = item.get("severity")
        severity = _SEVERITY.get(severity_raw, "unknown") if isinstance(severity_raw, int) else "unknown"
        message = str(item.get("message") or "").replace("\x00", "")[:4000]
        rows.append(
            {
                "severity": severity,
                "line": int(start.get("line", 0)) + 1 if isinstance(start.get("line"), int) else None,
                "character": int(start.get("character", 0)) + 1 if isinstance(start.get("character"), int) else None,
                "end_line": int(end.get("line", 0)) + 1 if isinstance(end.get("line"), int) else None,
                "end_character": int(end.get("character", 0)) + 1 if isinstance(end.get("character"), int) else None,
                "message": message,
                "source": str(item.get("source") or "")[:200],
                "code": str(item.get("code") or "")[:200],
            }
        )
    return rows


class LspDiagnostics:
    def __init__(self, repository: Path) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        if not self.repository.is_dir():
            raise LspDiagnosticsError("LSP repository is not a directory")

    def diagnose(self, path: str, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
        if isinstance(timeout_seconds, bool) or not 1.0 <= float(timeout_seconds) <= 30.0:
            raise ValueError("LSP diagnostic timeout must be between 1 and 30 seconds")
        target = _confined(self.repository, path)
        raw = target.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise LspDiagnosticsError("diagnostic file exceeds the 2 MiB safety limit")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise LspDiagnosticsError("diagnostic file must be UTF-8 text") from exc
        spec, language_id = _spec_for(target)
        executable = shutil.which(spec.executable)
        if not executable:  # _spec_for already checks; keeps types explicit.
            raise LspDiagnosticsError("language server executable disappeared before launch")
        argv = [executable, *spec.arguments]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.repository,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_process_environment(os.environ),
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise LspDiagnosticsError(f"cannot start {spec.name}: {type(exc).__name__}") from exc
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise LspDiagnosticsError("language server stdio was not created")
        stdin = process.stdin
        stdout = process.stdout
        messages: "queue.Queue[object]" = queue.Queue()
        reader = threading.Thread(target=_reader, args=(stdout, messages), daemon=True)
        reader.start()
        started = time.monotonic()
        deadline = started + float(timeout_seconds)
        doc_uri = _uri(target)

        def send(payload: dict[str, Any]) -> None:
            _write_message(stdin, payload)

        def next_message(max_wait: float) -> dict[str, Any]:
            try:
                value = messages.get(timeout=max(0.01, max_wait))
            except queue.Empty as exc:
                raise LspDiagnosticsError("language server response timed out") from exc
            if isinstance(value, BaseException):
                raise LspDiagnosticsError(str(value)) from value
            if not isinstance(value, dict):
                raise LspDiagnosticsError("language server returned an invalid message")
            return value

        diagnostics: list[dict[str, Any]] = []
        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": os.getpid(),
                        "rootUri": _uri(self.repository),
                        "capabilities": {
                            "textDocument": {"publishDiagnostics": {"relatedInformation": False}}
                        },
                    },
                }
            )
            initialized = False
            while time.monotonic() < deadline:
                message = next_message(deadline - time.monotonic())
                if message.get("id") == 1:
                    if "error" in message:
                        raise LspDiagnosticsError("language server rejected initialize")
                    initialized = True
                    break
            if not initialized:
                raise LspDiagnosticsError("language server did not initialize")
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": doc_uri,
                            "languageId": language_id,
                            "version": 1,
                            "text": text,
                        }
                    },
                }
            )
            # Wait until one publishDiagnostics arrives, then allow a short
            # quiescence window for a follow-up replacement from the server.
            received_at: Optional[float] = None
            while time.monotonic() < deadline:
                wait = deadline - time.monotonic()
                if received_at is not None:
                    wait = min(wait, max(0.01, received_at + 0.5 - time.monotonic()))
                    if wait <= 0.01 and time.monotonic() >= received_at + 0.5:
                        break
                try:
                    message = next_message(wait)
                except LspDiagnosticsError as exc:
                    if received_at is not None and "timed out" in str(exc):
                        break
                    raise
                if message.get("method") != "textDocument/publishDiagnostics":
                    continue
                raw_params = message.get("params")
                params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
                if str(params.get("uri") or "") != doc_uri:
                    continue
                diagnostics = _diagnostic_rows(params.get("diagnostics"))
                received_at = time.monotonic()
            if received_at is None:
                raise LspDiagnosticsError("language server published no diagnostics before the deadline")
            return {
                "status": "ok",
                "server": spec.name,
                "path": target.relative_to(self.repository).as_posix(),
                "diagnostic_count": len(diagnostics),
                "diagnostics": diagnostics,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
            }
        finally:
            try:
                if process.poll() is None:
                    send({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None})
                    end = time.monotonic() + 0.5
                    while time.monotonic() < end:
                        try:
                            message = next_message(end - time.monotonic())
                        except LspDiagnosticsError:
                            break
                        if message.get("id") == 2:
                            break
                    send({"jsonrpc": "2.0", "method": "exit", "params": None})
            except Exception:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)


__all__ = ["LspDiagnostics", "LspDiagnosticsError", "available_language_servers"]
