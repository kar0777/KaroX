"""KaroX code-mode worker: model-written Python whose only reach is tool calls.

KaroX starts this file as ``python -I -S code_mode_worker.py`` with an empty
environment and a scratch working directory. The model's scripts run here, in
one persistent namespace, and every tool they call is a message back to KaroX,
which runs it through the same capability policy, risk engine and audit as a
direct tool call. Before any script runs, an audit hook refuses process
creation, sockets, native-code loading, file writes, reads outside the
standard library and the introspection that could reach the hook. The hook is
defense in depth; the boundary is that no credential, environment variable or
repository handle ever reaches this process.

The protocol is one JSON object per line. Host to worker: ``eval`` (code and
the tool list) and ``ret`` (one tool result) or ``abort``. Worker to host:
``ready``, ``call`` (a tool call plus the output printed since the last
message) and ``done`` (remaining output, the last expression's value and the
error, if any). This file imports nothing from KaroX.
"""

from __future__ import annotations

import ast
import builtins
import collections
import io
import json
import linecache
import os
import pprint
import sys
import threading
import time
import traceback

#: Output kept per script; the host clips again to what the model is shown.
OUTPUT_CAP = 200_000
#: Of that, the end of a longer output, kept in a rolling window.
TAIL_CAP = 20_000
#: How often a running script's new output is sent to KaroX.
FLUSH_SECONDS = 0.5
#: A protocol line longer than this is a broken host, not a message.
LINE_CAP = 16 * 1024 * 1024

_PROTOCOL_OUT = sys.stdout.buffer
_PROTOCOL_IN = sys.stdin.buffer

#: Standard modules loaded before the hook, so a script can import them
#: without touching the file system.
_PRELOAD = (
    "base64", "bisect", "collections", "copy", "csv", "dataclasses", "datetime",
    "decimal", "difflib", "enum", "fnmatch", "fractions", "functools", "hashlib",
    "heapq", "itertools", "json", "math", "operator", "pathlib", "pprint", "random",
    "re", "shlex", "statistics", "string", "textwrap", "typing", "unicodedata",
)
#: Modules whose only purpose here would be to escape the worker.
_DENIED_IMPORTS = frozenset({
    "ctypes", "_ctypes", "_winapi", "winreg", "msvcrt", "_posixsubprocess",
    "subprocess", "socket", "_socket", "ssl", "_ssl", "mmap", "multiprocessing",
    "_multiprocessing", "_overlapped", "asyncio", "select", "selectors",
    "concurrent", "sqlite3", "_sqlite3", "urllib.request", "http.client",
    "http.server", "ftplib", "smtplib", "webbrowser", "pty", "resource",
})
#: Audit events refused outright (prefix match).
_DENIED_EVENTS = (
    "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
    "os.forkpty", "os.startfile", "os.kill", "os.remove", "os.rename", "os.rmdir",
    "os.mkdir", "os.chmod", "os.chown", "os.chflags", "os.lchflags", "os.link",
    "os.symlink", "os.truncate", "os.utime", "os.putenv", "os.unsetenv",
    "os.chdir", "os.add_dll_directory", "os.mkfifo",
    "os.mknod", "os.getxattr", "os.setxattr", "os.removexattr", "os.listxattr",
    "glob.", "shutil.", "socket.", "ctypes.", "_winapi.", "winreg.", "msvcrt.",
    "urllib.", "http.", "ftplib.", "smtplib.", "poplib.", "imaplib.", "nntplib.",
    "telnetlib.", "webbrowser.", "pty.", "sqlite3.", "mmap.", "resource.",
    "signal.", "syslog.", "_thread.", "gc.get_objects", "gc.get_referrers",
    "gc.get_referents", "sys.settrace", "sys.setprofile", "builtins.input",
    "tempfile.", "sys.remote_exec", "sys._getframe", "sys._current_frames",
    "sys._current_exceptions", "_posixsubprocess.", "code.__new__",
)
#: Attribute reads that hand out a frame, whose locals reach the audit hook.
_FRAME_ATTRIBUTES = frozenset({"tb_frame", "gi_frame", "cr_frame", "ag_frame", "f_back"})
_GUARDED_ATTRIBUTES = frozenset(
    {"__code__", "__globals__", "__closure__", "__defaults__", "__kwdefaults__"}
)
_NO_FILES = (
    "code_run has no direct file, process or network access: call the tools "
    "instead (repo_read_lines, repo_search, repo_edit_file, checks_run)"
)


class _Stopped(BaseException):
    """The host stopped the script; not catchable by ``except Exception``."""


class ToolResult(str):
    """What a tool returned: the text KaroX shows for it, plus fields.

    ``ok`` is the tool's own verdict, ``data`` its structured result and
    ``exit_code`` the exit status of a command (``None`` for other tools).
    """

    ok: bool
    data: dict

    def __new__(cls, text: str, ok: bool, data: object) -> "ToolResult":
        value = super().__new__(cls, text)
        value.ok = bool(ok)
        value.data = data if isinstance(data, dict) else {}
        return value

    @property
    def exit_code(self) -> object:
        return self.data.get("exit_code", self.data.get("returncode"))

    def _field(self, name: str) -> object:
        data = self.__dict__.get("data", {})
        if name in data:
            return data[name]
        if name in _TEXT_NAMES:
            return str(self)
        if name in _EXIT_NAMES:
            return self.exit_code
        if name in _OK_NAMES:
            return self.__dict__.get("ok", False)
        return _MISSING

    def get(self, key: str, default: object = None) -> object:
        """``result.get('matches')``: a field of ``.data``, as on a dict."""

        value = self._field(key) if isinstance(key, str) else _MISSING
        return default if value is _MISSING else value

    def __getitem__(self, key: object) -> object:  # type: ignore[override]
        if isinstance(key, str):
            value = self._field(key)
            if value is _MISSING:
                raise KeyError(f"{key!r}: .data has {sorted(self.__dict__.get('data', {}))[:20]}")
            return value
        return str.__getitem__(self, key)  # type: ignore[index]

    def __getattr__(self, name: str) -> object:
        """Names other models give the same things: fields of ``.data`` first.

        A model trained on another tool API writes ``result.output`` or
        ``result.returncode``; each was an AttributeError that cost a step.
        """

        if name.startswith("_"):
            raise AttributeError(name)
        value = self._field(name)
        if value is _MISSING:
            raise AttributeError(
                f"{name!r}: a tool result is the tool's text (a str) with .ok, .exit_code "
                f"and .data; .data has {sorted(self.__dict__.get('data', {}))[:20]}"
            )
        return value


_TEXT_NAMES = frozenset({"output", "text", "content", "result", "stdout", "value", "message", "body"})
_EXIT_NAMES = frozenset({"returncode", "return_code", "exitcode", "status", "status_code", "code"})
_OK_NAMES = frozenset({"success", "succeeded", "passed", "is_ok"})
_MISSING = object()


class _Capture:
    """Collects what a script prints: its start in order, then a rolling end.

    The start (up to OUTPUT_CAP - TAIL_CAP chars) is handed out as increments
    by a tool call, the flusher thread and the script's end. Past it only the
    last TAIL_CAP chars are kept, so a long log still shows how it ended; the
    start alone used to be kept and the end, usually the summary, was lost.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._pending: list = []
        self.size = 0
        self.overflow = 0
        self._tail: collections.deque = collections.deque()
        self._tail_size = 0

    def write(self, text: object) -> int:
        value = str(text)
        length = len(value)
        with self.lock:
            room = OUTPUT_CAP - TAIL_CAP - self.size
            if room > 0:
                kept = value[:room]
                self._pending.append(kept)
                self.size += len(kept)
                value = value[len(kept):]
            if value:
                self.overflow += len(value)
                self._tail.append(value)
                self._tail_size += len(value)
                while self._tail_size - len(self._tail[0]) >= TAIL_CAP:
                    self._tail_size -= len(self._tail.popleft())
        return length

    def tail(self) -> tuple:
        """The kept end of the output and how many chars before it were dropped."""

        with self.lock:
            text = "".join(self._tail)[-TAIL_CAP:]
            return text, self.overflow - len(text)

    def writelines(self, lines: object) -> None:
        for line in lines:  # type: ignore[attr-defined]
            self.write(line)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def take(self) -> str:
        with self.lock:
            text = "".join(self._pending)
            self._pending = []
            return text

    def reset(self) -> None:
        with self.lock:
            self._pending = []
            self.size = 0
            self.overflow = 0
            self._tail = collections.deque()
            self._tail_size = 0


_capture = _Capture()
_namespace: dict = {"__name__": "__code_run__", "__builtins__": builtins}
#: The eval being run; every message names it, so KaroX drops a stale or
#: forged one left over from an earlier script.
_eval_id: object = None
#: Whether a script is running, so the flusher sends nothing between scripts.
_running = False
#: Held from taking output to sending it, so increments arrive in order.
_send_lock = threading.RLock()


def _send(message: dict) -> None:
    line = json.dumps(message, ensure_ascii=False, default=str) + "\n"
    with _send_lock:
        _PROTOCOL_OUT.write(line.encode("utf-8", "replace"))
        _PROTOCOL_OUT.flush()


def _flusher() -> None:
    """Send a running script's new output every FLUSH_SECONDS.

    Output used to travel only with a tool call or the script's end, so what
    a script printed before it hung or ran out of time died with the worker.
    The kept end of a long output is sent too, at most every two seconds.
    """

    tail_sent: tuple = (None, 0)
    tail_at = 0.0
    while True:
        time.sleep(FLUSH_SECONDS)
        with _send_lock:
            if not _running:
                continue
            message: dict = {"t": "out", "id": _eval_id, "out": _capture.take()}
            now = time.monotonic()
            if (_eval_id, _capture.overflow) != tail_sent and _capture.overflow and now - tail_at >= 2.0:
                message["tail"], message["dropped"] = _capture.tail()
                tail_sent, tail_at = (_eval_id, _capture.overflow), now
            if message["out"] or "tail" in message:
                _send(message)


def _receive() -> dict:
    line = _PROTOCOL_IN.readline(LINE_CAP)
    if not line:
        os._exit(0)
    value = json.loads(line.decode("utf-8", "replace"))
    return value if isinstance(value, dict) else {}


def _call(name: str, arguments: dict) -> ToolResult:
    with _send_lock:
        _send({"t": "call", "id": _eval_id, "name": name, "args": arguments, "out": _capture.take()})
    reply = _receive()
    if reply.get("t") == "abort":
        raise _Stopped(str(reply.get("reason") or "stopped by KaroX"))
    return ToolResult(str(reply.get("text") or ""), bool(reply.get("ok")), reply.get("data"))


def _tool(name: str, required: tuple) -> object:
    def tool(*args: object, **kwargs: object) -> ToolResult:
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            return _call(name, dict(args[0]))
        if len(args) > len(required):
            raise TypeError(
                f"{name}() takes at most {len(required)} positional argument(s) "
                f"({', '.join(required) or 'none'}); pass the rest by name"
            )
        arguments = dict(zip(required, args))
        for key, value in kwargs.items():
            if key in arguments:
                raise TypeError(f"{name}() got {key!r} twice")
            arguments[key] = value
        return _call(name, arguments)

    tool.__name__ = name
    tool.__qualname__ = name
    return tool


def _format_error(error: BaseException, filename: str, source: list, frames_allowed: list) -> str:
    entries = []
    frames_allowed[0] = True
    try:
        tb = error.__traceback__
        while tb is not None and len(entries) < 200:
            if tb.tb_frame.f_code.co_filename == filename:
                entries.append(tb.tb_lineno)
            tb = tb.tb_next
    except Exception:  # noqa: BLE001 - a broken traceback still reports its error
        pass
    finally:
        frames_allowed[0] = False
    lines = []
    if entries:
        lines.append("Traceback (script lines, most recent last):")
        for number in entries[-6:]:
            text = source[number - 1].strip() if 0 < number <= len(source) else ""
            lines.append(f"  line {number}: {text}")
    try:
        lines.extend(
            part.rstrip("\n") for part in traceback.format_exception_only(type(error), error)
        )
    except Exception:  # noqa: BLE001 - it walks chained tracebacks, whose frames stay closed
        try:
            detail = str(error)
        except Exception:  # noqa: BLE001 - a __str__ that raises still names its type
            detail = ""
        name = type(error).__name__
        lines.append(f"{name}: {detail}" if detail else name)
    return "\n".join(lines)


def _show(value: object) -> str:
    if isinstance(value, str):
        return str(value)
    try:
        return pprint.pformat(value, width=110, compact=True, sort_dicts=False)
    except Exception:  # noqa: BLE001 - a broken __repr__ still gets a line
        return object.__repr__(value)


def _evaluate(message: dict, frames_allowed: list) -> None:
    global _eval_id, _running
    _eval_id = message.get("id")
    code = str(message.get("code") or "")
    filename = f"<code_run {message.get('id')}>"
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    for item in message.get("tools") or ():
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            required = tuple(str(key) for key in item.get("required") or ())
            _namespace[item["name"]] = _tool(item["name"], required)
    _capture.reset()
    _running = True
    shown = None
    error = None
    saved = sys.stdout, sys.stderr, sys.stdin
    sys.stdout = sys.stderr = _capture  # type: ignore[assignment]
    sys.stdin = io.StringIO("")
    try:
        tree = ast.parse(code, filename, "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        exec(compile(tree, filename, "exec"), _namespace)
        if last is not None:
            value = eval(compile(last, filename, "eval"), _namespace)
            if value is not None:
                shown = _show(value)[:OUTPUT_CAP]
    except _Stopped as stopped:
        error = f"stopped: {stopped}"
    except SystemExit as stop:
        if stop.code not in (None, 0):
            error = f"SystemExit({stop.code!r})"
    except BaseException as failure:  # noqa: BLE001 - every failure is reported
        error = _format_error(failure, filename, code.splitlines(), frames_allowed)
        if isinstance(failure, NameError):
            # read_file(), bash(), grep(): names from another tool API.
            tools = sorted(str(item["name"]) for item in message.get("tools") or () if isinstance(item, dict))
            error += f"\nTool functions here: {', '.join(tools)}" if tools else ""
    finally:
        sys.stdout, sys.stderr, sys.stdin = saved
    with _send_lock:
        _running = False
        tail, dropped = _capture.tail()
        _send(
            {
                "t": "done",
                "id": _eval_id,
                "out": _capture.take(),
                "tail": tail,
                "dropped": dropped,
                "value": shown,
                "error": error,
            }
        )


def _key(path: str, windows: bool) -> str:
    """A comparable form of a path using only str methods (nothing a script can patch)."""

    text = path.replace("\\", "/") if windows else path
    if windows:
        text = text.lower()
        if text.startswith("//?/"):
            text = text[4:]
    return text.rstrip("/") + "/"


def _stdlib_roots(windows: bool) -> tuple:
    base = os.path.dirname(os.__file__)
    candidates = [base, os.path.join(base, "lib-dynload"), os.path.join(sys.base_prefix, "DLLs")]
    candidates.extend(entry for entry in sys.path if entry)
    roots = []
    for path in candidates:
        if os.path.isdir(path):
            for form in (path, os.path.realpath(path)):
                key = _key(os.path.abspath(form), windows)
                if key not in roots:
                    roots.append(key)
    return tuple(roots)


def _seal() -> list:
    """Install the audit hook; return the switch that lets the worker read frames.

    The hook only ever calls what it bound here, never a global or builtin by
    name, and it judges only exact ``str`` values: a script can rebind this
    module's globals (``import __main__``) and the builtins, and a str subclass
    or path object would run script code inside the check.
    """

    windows = os.name == "nt"
    roots = _stdlib_roots(windows)
    write_flags = os.O_WRONLY | os.O_RDWR
    for flag in ("O_CREAT", "O_TRUNC", "O_APPEND"):
        write_flags |= getattr(os, flag, 0)
    denied_events = _DENIED_EVENTS
    denied_imports = _DENIED_IMPORTS
    guarded = _GUARDED_ATTRIBUTES
    frame_attributes = _FRAME_ATTRIBUTES
    message = _NO_FILES
    key_of = _key
    str_type, int_type, type_of, length, range_of = str, int, type, len, range
    permission_error, import_error = PermissionError, ImportError
    frames_allowed = [False]

    def inside(path: object) -> bool:
        if type_of(path) is not str_type or "\x00" in path:  # type: ignore[operator]
            return False
        key = key_of(path, windows)
        return "/../" not in key and "/./" not in key and key.startswith(roots)

    def audit(event: str, args: tuple) -> None:
        if event == "object.__getattr__":
            if length(args) > 1 and args[1] in frame_attributes and not frames_allowed[0]:
                raise permission_error("code_run cannot inspect frames")
            return
        if event in ("os.listdir", "os.scandir"):
            # The import system lists standard-library directories to find
            # a module; any other directory stays unreadable.
            if not args or not inside(args[0]):
                raise permission_error(message)
            return
        if event == "open":
            path = args[0] if args else None
            mode = args[1] if length(args) > 1 else None
            flags = args[2] if length(args) > 2 else None
            if mode is not None and (type_of(mode) is not str_type or "w" in mode
                                     or "a" in mode or "x" in mode or "+" in mode):
                raise permission_error(message)
            if flags is not None and (type_of(flags) is not int_type or flags & write_flags):
                raise permission_error(message)
            if not inside(path):
                raise permission_error(message)
            return
        if event == "import":
            module = args[0] if args else None
            if type_of(module) is not str_type:
                raise import_error("code_run imports modules by plain name only")
            parts = module.split(".")  # type: ignore[union-attr]
            for size in range_of(1, length(parts) + 1):
                if ".".join(parts[:size]) in denied_imports:
                    raise import_error(f"{module} is not available in code_run. {message}")
            return
        if event == "sys._getframemodulename":
            # namedtuple and enum ask for a module name, never a frame.
            return
        if event.startswith(denied_events):
            raise permission_error(f"{event} is not available in code_run. {message}")
        if event == "object.__setattr__" and length(args) > 1 and args[1] in guarded:
            raise permission_error("code_run cannot rewrite functions")

    sys.addaudithook(audit)
    return frames_allowed


def _limit_memory() -> None:
    try:
        import resource
    except ImportError:
        return
    limit = 2 * 1024 * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass


def main() -> None:
    _limit_memory()
    for name in _PRELOAD:
        try:
            __import__(name)
        except ImportError:
            pass
    # Started before the hook, which refuses new threads to scripts.
    threading.Thread(target=_flusher, name="code-run-flush", daemon=True).start()
    frames_allowed = _seal()
    _send({"t": "ready", "python": sys.version.split()[0]})
    while True:
        message = _receive()
        if message.get("t") == "eval":
            _evaluate(message, frames_allowed)


if __name__ == "__main__":
    main()
