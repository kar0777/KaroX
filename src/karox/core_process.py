"""Process and check execution for the Core action boundary.

The Core runtime exposes process execution and the approved-check path through
this mixin so the repository, search and file surfaces stay readable on their
own. Everything here is about one child process: its argv guard, its isolated
launch, its tree termination, its captured output and the timeout plan derived
from the user's approved verification commands.
"""

from __future__ import annotations

from .process_tree import (  # noqa: F401
    CapturedStream as CapturedStream,
    ProcessTree as ProcessTree,
    _WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION as _WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION,
    _WINDOWS_JOB_LIMIT_BREAKAWAY_OK as _WINDOWS_JOB_LIMIT_BREAKAWAY_OK,
    _WINDOWS_JOB_LIMIT_KILL_ON_CLOSE as _WINDOWS_JOB_LIMIT_KILL_ON_CLOSE,
    _WINDOWS_PROCESS_SET_QUOTA as _WINDOWS_PROCESS_SET_QUOTA,
    _WINDOWS_PROCESS_TERMINATE as _WINDOWS_PROCESS_TERMINATE,
    _decode_captured_bytes as _decode_captured_bytes,
    _kill_posix_process_group as _kill_posix_process_group,
    _kill_windows_process_tree as _kill_windows_process_tree,
    _legacy_output_encodings as _legacy_output_encodings,
    _new_process_group_kwargs as _new_process_group_kwargs,
    _windows_job_api as _windows_job_api,
)

import hashlib
import math
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import check_baseline
from .check_report import analyze as analyze_check_output
from .core_types import InvalidCommand, VerificationCommandNotApproved, _path_label
from .models import EvidenceRecord
from .process_launcher import resolve_executable as _resolve_executable
from .security import child_process_environment, redact

if TYPE_CHECKING:
    from .core import VerificationRule


#: Appended to the header of every inline ``python -c`` check. A probe that
#: leaves a non-daemon thread blocked (a waiter its fake clock never wakes)
#: keeps the interpreter alive after its last line, and the check then sat
#: out its whole timeout: 120 s of a measured 312 s run (val1 eng-rate-limit).
#: Interpreter shutdown calls this before it joins those threads; after a
#: short grace it names them and exits 3, so the probe reports instead of
#: hanging. ``threading._register_atexit`` exists from Python 3.9; it runs
#: in the header's single line through exec().
_INLINE_THREAD_REAPER = (
    "def _karox_reap_threads():\n"
    "    import os, sys, threading, time\n"
    "    main = threading.main_thread()\n"
    "    alive = [t for t in threading.enumerate() if t is not main and not t.daemon and t.is_alive()]\n"
    "    end = time.monotonic() + 3\n"
    "    for thread in alive:\n"
    "        thread.join(max(0.0, end - time.monotonic()))\n"
    "    alive = [t for t in alive if t.is_alive()]\n"
    "    if alive:\n"
    "        names = ', '.join(t.name for t in alive[:5])\n"
    "        print(f'[karox] the probe ended with {len(alive)} non-daemon thread(s) still blocked ({names}); '\n"
    "              'exited without waiting for them', file=sys.stderr)\n"
    "        sys.stdout.flush(); sys.stderr.flush()\n"
    "        os._exit(3)\n"
    "import threading as _karox_threading\n"
    "if hasattr(_karox_threading, '_register_atexit'):\n"
    "    _karox_threading._register_atexit(_karox_reap_threads)\n"
    "del _karox_threading\n"
)


def _is_karox_module_command(argv: Sequence[str]) -> bool:
    """Whether this argv runs one of KaroX's own modules, by any interpreter.

    Such a command is resolved by module name, which is exactly what a checked
    repository can shadow: Python puts the working directory first on sys.path.
    """

    for index, item in enumerate(argv):
        if item != "-m":
            continue
        target = argv[index + 1] if index + 1 < len(argv) else ""
        return target == "karox" or target.startswith("karox.")
    return False


# The package this process is running. A KaroX module check is pinned to it, so
# the checker that answers is the one that ships with this runtime: never a
# repository file, and never a stale copy elsewhere on the interpreter's path.
_KAROX_PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)

# Launch shim for a KaroX module check. Isolated mode (-I) keeps the working
# directory, PYTHONPATH and user site out of sys.path on every supported
# interpreter, including 3.10 where PYTHONSAFEPATH does not exist. Isolation also
# drops PYTHONPATH, so a runtime started from a source checkout would otherwise
# lose its own checker (or silently run an older installed one); the shim puts
# this runtime's package first and runs the module exactly as ``-m`` would.
_KAROX_MODULE_SHIM = (
    "import runpy, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)\n"
)


def _karox_module_launch(argv: Sequence[str]) -> List[str]:
    """The argv actually launched for a KaroX module check.

    Core approves and reports the logical argv unchanged. Only the launch is
    isolated (-I), kept UTF-8 (-X utf8, because isolation ignores PYTHONUTF8 and
    PYTHONIOENCODING) and pinned to this runtime's package.
    """

    parts = list(argv)
    index = parts.index("-m")
    options = [item for item in parts[1:index] if item != "-I"]
    return [
        parts[0], *options, "-I", "-X", "utf8", "-c", _KAROX_MODULE_SHIM,
        _KAROX_PACKAGE_PARENT, *parts[index + 1:],
    ]

WILDCARD_ARGUMENT = "*"
STALLED_TEST_SECONDS = 20  # pytest faulthandler_timeout for every run KaroX starts
# Options that hand a program text to the executable instead of a file to work
# on.  A rule's literal prefix is what the user actually approved; the wildcard
# tail is not, so it must not be able to turn "run my test suite" into "run
# whatever you like" by appending an interpreter's own code-execution flag.
_CODE_EXECUTION_OPTIONS = frozenset(
    {"-c", "-e", "--code", "--command", "--eval", "--exec", "--execute"}
)
# Short options may carry their value in the same token (``python -cCODE``) and
# may be bundled with other short options (``python -Bc CODE``), so a whole-token
# comparison alone would let the very flag above straight back in.
_CODE_EXECUTION_SHORT_LETTERS = frozenset({"c", "e"})


def inline_python_rule(
    rules: Sequence["VerificationRule"], argv: Sequence[str]
) -> Optional[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    """Match ``<python> -c CODE [plain args]`` against a wildcard python rule.

    Returns (prefix, code, trailing args) when a wildcard rule whose executable
    is a Python interpreter approves running a plain script, so the same code
    can be run from a temporary file. Only the exact ``-c`` token is admitted;
    ``-e``/bundled forms and non-Python interpreters stay refused.
    """
    for rule in rules:
        if not rule.wildcard:
            continue
        prefix = rule.prefix
        # Only a bare interpreter rule (python *) approves running a script.
        # A rule like python -m compileall -q * approves a module's own
        # arguments; turning its -c into a script path would change meaning.
        if len(prefix) != 1:
            continue
        executable = os.path.basename(prefix[0]).lower()
        if not executable.startswith("python"):
            continue
        if tuple(argv[: len(prefix)]) != prefix:
            continue
        tail = list(argv[len(prefix) :])
        if len(tail) < 2 or tail[0] != "-c" or not tail[1].strip():
            continue
        rest = tuple(tail[2:])
        if not all(is_plain_argument(item) for item in rest):
            continue
        return prefix, tail[1], rest
    return None

#: POSIX shells a verification rule may approve by name (``bash -lc *``).
_APPROVABLE_SHELLS = frozenset({"bash", "sh", "dash", "zsh"})


def is_plain_argument(value: str) -> bool:
    """True when a wildcard tail argument is one this rule will admit.

    The filter refuses the code-execution options of the interpreters KaroX
    actually approves by default. It is a guard, not a boundary: a rule like
    ``node *`` authorises node with arguments of the caller's choosing, and no
    flag list can make that mean something narrower. What holds the line is the
    literal prefix the user approved, which always includes the executable.
    """
    if value == WILDCARD_ARGUMENT:
        # Checks run without a shell, so a literal `*` reaches the child as a
        # filename that does not exist. Admitting it produced an approved
        # command that always failed, which read as the allowlist being broken.
        return False
    if not value.startswith("-"):
        return True
    name = value.split("=", 1)[0].lower()
    if name in _CODE_EXECUTION_OPTIONS:
        return False
    if value.startswith("--"):
        return True
    return not _CODE_EXECUTION_SHORT_LETTERS.intersection(name[1:])


# Modules a probe may run with ``-m``. Each runs or compiles Python code the
# approved interpreter would run anyway; none installs, downloads or serves
# anything, which is why pip, ensurepip, venv and http.server are absent.
PROBE_MODULES = frozenset(
    {
        "pytest",
        "unittest",
        "doctest",
        "py_compile",
        "compileall",
        "timeit",
        "cProfile",
        "json.tool",
    }
)


@dataclass(frozen=True)
class ProbeCommand:
    """A non-evidence Python run admitted next to the approved checks.

    ``form`` is ``inline`` (``-c CODE``), ``module`` (``-m`` one of
    :data:`PROBE_MODULES`) or ``script`` (a ``.py`` file inside the repository).
    """

    interpreter: str
    form: str
    code: Optional[str] = None
    tail: tuple[str, ...] = ()


def probe_interpreters(rules: Sequence["VerificationRule"]) -> tuple[str, ...]:
    """Python interpreters named by approved rules, in the order given."""
    found: List[str] = []
    for rule in rules:
        executable = rule.prefix[0]
        if not os.path.basename(executable).lower().startswith("python"):
            continue
        if executable not in found:
            found.append(executable)
    return tuple(found)


def probe_python_command(
    rules: Sequence["VerificationRule"], argv: Sequence[str], repository: Path
) -> Optional[ProbeCommand]:
    """Admit a reproduction probe run by an approved Python interpreter.

    An approved ``python run_tests.py`` already runs whatever repository code
    the agent edits, so the same interpreter running a snippet, a test module
    or a repository script grants no new reach. What it changes is cost: in the
    sandbox SWE runs (2026-11-04) the exact rule refused every ``python -c``
    reproduction and the model spent its steps on git and file reads instead.
    A probe never counts as verification; the caller keeps it out of evidence.
    """
    if len(argv) < 2:
        return None
    interpreter = argv[0]
    if interpreter not in probe_interpreters(rules):
        return None
    head = argv[1]
    if head in {"-c", "-m"}:
        if len(argv) < 3 or not argv[2].strip():
            return None
        tail = tuple(argv[3:])
        if not all(is_plain_argument(item) for item in tail):
            return None
        if head == "-c":
            return ProbeCommand(interpreter, "inline", argv[2], tail)
        if argv[2] not in PROBE_MODULES:
            return None
        return ProbeCommand(interpreter, "module", None, tail)
    if head.startswith("-") or not head.lower().endswith(".py"):
        return None
    tail = tuple(argv[2:])
    if not all(is_plain_argument(item) for item in tail):
        return None
    try:
        root = repository.resolve()
        candidate = Path(head)
        target = (candidate if candidate.is_absolute() else root / candidate).resolve()
        relative = target.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    if ".git" in relative.parts or not target.is_file():
        return None
    return ProbeCommand(interpreter, "script", None, tail)


def probe_forms_text(interpreters: Sequence[str]) -> str:
    """The probe shapes, named with the interpreter the user approved."""
    python = interpreters[0] if interpreters else "python"
    return (
        f"'{python} -c CODE', '{python} -m pytest|unittest|doctest ...' and "
        f"'{python} <file>.py ...' for a .py file inside the repository"
    )
















def _max_check_seconds() -> float:
    """The longest one checks.run may take (KAROX_MAX_CHECK_SECONDS, default 300)."""
    raw = os.environ.get("KAROX_MAX_CHECK_SECONDS", "300")
    value = float(raw) if raw.replace(".", "", 1).isdigit() else 300.0
    return value if 0 < value < math.inf else 300.0


@dataclass(frozen=True)
class CheckPlan:
    argv: List[str]
    requested_timeout: float
    effective_timeout: float
    clamped_by: Optional[str]
    verification_eligible: bool
    # Inline interpreter code (``python -c CODE``) admitted under a wildcard
    # interpreter rule. The interpreter already runs any file the model writes,
    # so refusing the same code passed inline only cost turns: measured SWE runs
    # (swer5-k5..swer8-k10) hit 23 refusals, every one a ``python -c`` probe.
    # The code is run from a KaroX-owned temporary file outside the repository,
    # which is removed afterwards, so nothing lands in the user's tree.
    inline_code: Optional[str] = None
    inline_tail: tuple[str, ...] = ()
    inline_prefix: tuple[str, ...] = ()
    # A reproduction probe (see probe_python_command): it runs, its output is
    # returned, and it is never verification evidence.
    probe: bool = False


class ProcessToolsMixin:
    """The process, check and argv surfaces of :class:`karox.core.CoreRuntime`."""

    repository: Path
    _verification_rules: Optional[tuple[VerificationRule, ...]]
    _required: Callable[..., Any]
    MAX_OUTPUT_BYTES: int
    #: How much unelided output a diagnostic parser may read. The display
    #: stream keeps its head and tail at MAX_OUTPUT_BYTES, but a failure block
    #: can sit in the elided middle; this is the budget for finding it.
    MAX_MAP_BYTES: int = 2_000_000
    #: Whether checks.run admits reproduction probes (probe_python_command).
    _allow_probes: bool = False
    #: Pre-existing failures per approved argv (check_baseline), set by CoreRuntime.
    _check_baselines: Dict[tuple[str, ...], Any]
    MIN_PROCESS_TIMEOUT_SECONDS: float
    MAX_PROCESS_TIMEOUT_SECONDS: float
    DEFAULT_CHECK_TIMEOUT_SECONDS: float

    @staticmethod
    def _validate_process(argv: Iterable[str], approved_shell: bool = False) -> List[str]:
        """Refuse argv that can carry an external effect.

        ``approved_shell``: the argv matches a verification rule whose literal
        prefix is this shell (the user approved e.g. ``bash -lc *``). Then the
        shell runs, and only its script is screened for push/publish/auth/
        remote actions. Benchmark 2026-11-13 (TB 4.1 risk-scorer-replay): every
        ``bash -lc`` was refused under such a rule, so the task's own
        ``rebuild_parity_report.sh`` never ran once.
        """
        values = list(argv)
        if not values or len(values) > 100 or not all(isinstance(item, str) for item in values):
            raise InvalidCommand(
                "argv must contain 1-100 strings; got %d: %s"
                % (len(values), _path_label(" ".join(values)))
            )
        if any("\x00" in item or len(item) > 10_000 for item in values):
            oversized = next(
                (item for item in values if len(item) > 10_000), None
            )
            if oversized is not None:
                raise InvalidCommand(
                    "argument is too long (%d characters): %s"
                    % (len(oversized), _path_label(oversized))
                )
            raise InvalidCommand("argv contains a NUL byte")
        lowered = [item.lower() for item in values]
        executable = lowered[0].replace("\\", "/").rsplit("/", 1)[-1]
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        joined = " ".join(lowered[:4])
        if approved_shell and executable in _APPROVABLE_SHELLS:
            from .command_guard import shell_script_block_reason

            reason = shell_script_block_reason(" ".join(values[1:]))
            if reason is not None:
                raise InvalidCommand(reason)
            return values
        denied = (
            executable
            in {
                "bash",
                "cmd",
                "dash",
                "fish",
                "nu",
                "powershell",
                "pwsh",
                "scp",
                "sftp",
                "sh",
                "ssh",
                "wsl",
                "zsh",
            }
            or " publish" in f" {joined}"
            or " login" in f" {joined}"
            or " logout" in f" {joined}"
        )
        if denied:
            raise InvalidCommand("publishing, authentication, and remote Git commands are blocked")
        from .command_guard import process_kill_block_reason

        kill_reason = process_kill_block_reason(values)
        if kill_reason is not None:
            raise InvalidCommand(kill_reason)
        if executable == "git" or executable.startswith("python") or executable in {
            "node",
            "nodejs",
        }:
            # Git is allowed only through the same read-only subcommand
            # boundary used by developer commands. Python/Node -c/-e payloads
            # are checked by the same inline remote-action guard; a normal
            # approved probe has no such payload and is unaffected.
            from .command_guard import (
                DeveloperCommandBlocked,
                validate_developer_command_argv,
            )

            try:
                validate_developer_command_argv(values)
            except DeveloperCommandBlocked as exc:
                raise InvalidCommand(str(exc)) from exc
        return values

    def _run(
        self,
        argv: List[str],
        timeout_seconds: float,
        *,
        inherit_environment: bool = False,
        redact_output: bool = True,
        extra_environment: Optional[Dict[str, str]] = None,
        keep_full: bool = False,
    ) -> Dict[str, Any]:
        timeout = min(
            max(float(timeout_seconds), self.MIN_PROCESS_TIMEOUT_SECONDS),
            self.MAX_PROCESS_TIMEOUT_SECONDS,
        )
        if inherit_environment:
            # Trusted Full developer mode intentionally behaves like a normal
            # terminal launched by this KaroX process: CLI auth/deploy tools may
            # rely on inherited SDK/token/config variables. Protected checks and
            # every other Core process keep the minimal secret-filtered env.
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
        else:
            env = child_process_environment()
        if extra_environment:
            env.update({str(k): str(v) for k, v in extra_environment.items()})
        env.update({"PYTEST_ADDOPTS": f"{env.get('PYTEST_ADDOPTS', '')} -o faulthandler_timeout={STALLED_TEST_SECONDS}".strip()} if "pytest" in argv[:3] or Path(argv[0]).stem == "pytest" else {})  # a slow test leaves its stack
        ours = _is_karox_module_command(argv)
        if ours:
            # A check that runs one of KaroX's own modules must not be shadowed by
            # the repository it checks. PYTHONSAFEPATH is kept as defense in
            # depth, while isolated mode below removes the working directory from
            # Python's import path on every supported interpreter (including 3.10).
            env["PYTHONSAFEPATH"] = "1"
        started = time.perf_counter()
        # ``shell=False`` on Windows does not consult ``PATHEXT`` for ``.cmd``
        # shims, so ``npm`` (which lives as ``npm.cmd``) would raise WinError 2.
        # Resolve the executable to an absolute path *after* the verification
        # allowlist has already matched on the logical argv. Karo-owned module
        # checks are launched isolated and pinned to this runtime's package, but
        # the logical argv that Core approved and reports remains unchanged.
        launch_source = _karox_module_launch(argv) if ours else list(argv)
        launch_argv = _resolve_executable(launch_source)
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                launch_argv,
                cwd=self.repository,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                **_new_process_group_kwargs(),
            )
            tree = ProcessTree(process)
            interrupted = False
            cancellation_source: Optional[str] = None
            signal_sent: Optional[str] = None
            try:
                try:
                    exit_code: Optional[int] = process.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = None
                    cancellation_source = "command_timeout"
                    signal_sent = "terminate_owned_child_tree"
                    tree.terminate()
                except KeyboardInterrupt:
                    # A shared-console control event or expired hosted request is
                    # a cancellation of this command, never an instruction to
                    # stop the MCP bridge. Contain it to the owned child tree and
                    # return structured diagnostics instead of re-raising.
                    interrupted = True
                    timed_out = False
                    exit_code = None
                    cancellation_source = "request_interrupt"
                    signal_sent = "terminate_owned_child_tree"
                    tree.terminate()
                except BaseException:
                    # SystemExit and other non-user interrupts still receive
                    # bounded child cleanup, but are not silently converted.
                    tree.terminate()
                    raise
            finally:
                tree.close()
            stdout = self._bounded_stream(stdout_file, keep_full=keep_full)
            stderr = self._bounded_stream(stderr_file, keep_full=keep_full)
        result = {
            # Redaction of the captured streams is a diagnostic-surface rule:
            # audit/console rows must never echo a credential a child printed.
            # Byte-faithful consumers (search backends) opt out; their own
            # contract is that a matched line round-trips byte for byte.
            "argv": redact(argv),
            "exit_code": exit_code,
            "stdout": str(redact(stdout.text)) if redact_output else stdout.text,
            "stderr": str(redact(stderr.text)) if redact_output else stderr.text,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "child_pid": process.pid,
            "bridge_pid": os.getpid(),
            "process_group": (
                "windows-new-process-group+no-window"
                if os.name == "nt"
                else "posix-new-session"
            ),
            "cancellation_source": cancellation_source,
            "signal_sent": signal_sent,
            "stdout_sha256": stdout.sha256,
            "stderr_sha256": stderr.sha256,
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "stdout_bytes": stdout.total_bytes,
            "stderr_bytes": stderr.total_bytes,
            "stdout_elided_bytes": stdout.elided_bytes,
            "stderr_elided_bytes": stderr.elided_bytes,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if keep_full:
            # Redacted like the display streams; the consumer pops these and
            # keeps only the parsed map, so the unelided text never persists.
            result["_map_stdout"] = (
                str(redact(stdout.full_text)) if redact_output else stdout.full_text
            )
            result["_map_stderr"] = (
                str(redact(stderr.full_text)) if redact_output else stderr.full_text
            )
        return result

    def _bounded_stream(self, handle: Any, *, keep_full: bool = False) -> CapturedStream:
        handle.flush()
        handle.seek(0)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        full_text = ""
        if keep_full:
            budget = max(0, int(self.MAX_MAP_BYTES))
            if size <= budget:
                handle.seek(0)
                full_text, _ = _decode_captured_bytes(handle.read(size))
            else:
                # A pathological stream: keep both ends so the first failure
                # and the final summary are still parseable.
                half = max(1, budget // 2)
                handle.seek(0)
                head_full = handle.read(half)
                handle.seek(size - half)
                tail_full = handle.read(half)
                head_part, _ = _decode_captured_bytes(head_full)
                tail_part, _ = _decode_captured_bytes(tail_full, mid_stream_start=True)
                full_text = head_part + "\n" + tail_part
        limit = max(0, int(self.MAX_OUTPUT_BYTES))
        if size <= limit:
            handle.seek(0)
            body = handle.read(size)
            text, _ = _decode_captured_bytes(body)
            return CapturedStream(
                text,
                digest.hexdigest(),
                False,
                size,
                0,
                full_text,
            )
        # A compiler or a test runner states the root cause first and then repeats
        # it in a summary, so returning only the tail throws away the half of the
        # output that explains the failure.
        head_budget = (limit + 1) // 2
        tail_budget = limit - head_budget
        handle.seek(0)
        head = handle.read(head_budget)
        tail = b""
        if tail_budget:
            handle.seek(size - tail_budget)
            tail = handle.read(tail_budget)
        # Decoding each half independently drops any bytes of a character the
        # cut landed inside, so the count has to be taken from what is actually
        # returned rather than from the budgets. It sits beside a sha256 of the
        # whole stream, which makes an approximate number worse than useless.
        head_text, head_bytes = _decode_captured_bytes(head)
        tail_text, tail_bytes = _decode_captured_bytes(tail, mid_stream_start=True)
        elided = size - head_bytes - tail_bytes
        text = (
            head_text
            + f"\n[karox: {elided} bytes elided from the middle of this stream]\n"
            + tail_text
        )
        return CapturedStream(text, digest.hexdigest(), True, size, elided, full_text)

    def _run_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        plan = self._prepare_check(arguments, deadline_seconds)
        ours = _is_karox_module_command(plan.argv)
        # One random value per run, handed to the child that is supposed to be
        # KaroX's own checker and required back in its verdict. A repository that
        # shadows the checker cannot know it, so its answer is refused instead of
        # being counted as evidence -- which matters because a measured run wrote
        # its own web_check.py into the repository and KaroX certified the page on
        # the strength of it. PYTHONSAFEPATH stops the shadowing where it exists;
        # this stops the verdict where it cannot.
        challenge = secrets.token_hex(16) if ours else ""
        if plan.inline_code is not None:
            result = self._run_inline_check(plan)
        else:
            result = self._run(
                plan.argv,
                plan.effective_timeout,
                extra_environment={"KAROX_CHECK_CHALLENGE": challenge}
                if ours
                else None,
                keep_full=True,
            )
        if ours and challenge not in str(result.get("stdout", "")):
            result["exit_code"] = 1
            result["checker_identity"] = "unconfirmed"
            result["detail"] = (
                "the checker did not identify itself as KaroX's own, so its exit "
                "code is not evidence: this repository may contain a file that "
                "shadows karox.web_check"
            )
        result["verification_eligible"] = plan.verification_eligible
        if tool := check_baseline.missing_tool(plan.argv, result):  # offline image: neither proof nor owed
            result["tool_unavailable"], result["detail"] = tool, f"{tool} is not installed and the sandbox is offline: it is not owed; do not run or install it again, verify with the project's tests"
        if plan.probe:
            result["probe"] = True
        baseline = None
        current = check_baseline.of_result(result)
        if plan.inline_code is None and not plan.probe:
            baselines = getattr(self, "_check_baselines", None)
            if baselines is not None:
                baseline = baselines.get(tuple(plan.argv))
                if (
                    baseline is None
                    and tuple(plan.argv) not in baselines
                    and plan.verification_eligible
                    and check_baseline.is_pytest(plan.argv)
                    and current.exit_code != 0
                    and current.parsed
                    and current.failing
                    and float(result.get("duration_ms") or 0.0)
                    <= check_baseline.SWAP_MAX_CHECK_SECONDS * 1000
                ):
                    baseline = self._baseline_on_start_files(plan)
                    baselines[tuple(plan.argv)] = baseline
        if baseline is not None:
            info = check_baseline.compare(
                baseline, current, getattr(self, "_task_test_names", frozenset())
            )
            if info is not None:
                result["baseline"] = info
                if info["preexisting_only"] and plan.verification_eligible:
                    result["preexisting_only"] = True
        # Extracted from the unelided streams: the result the model reads has
        # its middle cut, and for a many-failure suite that is exactly where
        # the FAILURES blocks live. The map keeps every failure's file, line
        # and test id even when the raw log later loses them.
        full_stdout = str(result.pop("_map_stdout", result.get("stdout") or ""))
        full_stderr = str(result.pop("_map_stderr", result.get("stderr") or ""))
        if result.get("exit_code") != 0 or result.get("timed_out"):
            result["failure_map"] = analyze_check_output(
                full_stdout, full_stderr, root=self.repository
            )
        # A caller that asked for ten minutes and silently got the host's deadline
        # cannot tell a genuine hang from a clamp, so both numbers are reported.
        result["requested_timeout"] = plan.requested_timeout
        result["effective_timeout"] = plan.effective_timeout
        result["timeout_clamped_by"] = plan.clamped_by
        if float(result.get("duration_ms") or 0) > 90_000 and not result["timed_out"]:  # slow checks were 70-90% of slow HARD12 runs
            result["detail"] = "this check took over 90 s: prefer one test file, -k or node ids"
        if result["timed_out"]:
            detail = f"the check was stopped after {plan.effective_timeout:g}s and its process tree was terminated"
            if plan.clamped_by is not None:
                detail += f"; the requested {plan.requested_timeout:g}s was reduced by {plan.clamped_by}"
            pytest_run = check_baseline.is_pytest(plan.argv)
            # The stack dump and -k advice are pytest's; a hung npm or cargo run
            # sent the model looking for a [stderr] stack that was never written.
            hint = (
                f"a test over {STALLED_TEST_SECONDS}s left its stack under [stderr]; deselect it (-k) or run the narrowest tests that cover the change (one file, -k or node ids)"
                if pytest_run
                else "run a narrower command, or find what waits for input or never exits"
            )
            result["detail"] = detail + f"; it proves nothing (not owed either): {hint}"
        display_argv = result["argv"]
        outcome = "passed" if result["exit_code"] == 0 else "failed"
        result["_evidence"] = [
            EvidenceRecord(
                kind="probe" if plan.probe else "check",
                summary=(f"Probe {outcome}" if plan.probe else outcome.capitalize())
                + f": {' '.join(display_argv)}",
                command=display_argv,
                exit_code=result["exit_code"],
                metadata={
                    "timed_out": result["timed_out"],
                    "effective_timeout": plan.effective_timeout,
                    "timeout_clamped_by": plan.clamped_by,
                },
            )
        ]
        return result

    def _baseline_on_start_files(self, plan: CheckPlan) -> Optional[Any]:
        """``git stash; pytest; git stash pop`` for the session's changed files:
        journaled outside the repo, every byte restored and verified; None = strict."""
        from . import git_baseline

        start = git_baseline.current()
        changed = [p for p in git_baseline.changed_paths() if p]
        if not start or start.get("kind") != "commit":
            return None
        if not 0 < len(changed) <= check_baseline.SWAP_MAX_FILES:
            return None
        commit = str(start.get("commit"))
        journal = check_baseline.swap_journal_path(self.repository)
        check_baseline.recover_swap(journal)
        saved: Dict[str, Optional[bytes]] = {}
        start_bytes: Dict[str, Optional[bytes]] = {}
        for relative in changed:
            try:
                path = self.safe_path(relative)
            except Exception:  # noqa: BLE001 - nothing moved yet: no baseline
                return None
            current_bytes = path.read_bytes() if path.is_file() else None
            if path.is_dir() or len(current_bytes or b"") > check_baseline.SWAP_MAX_BYTES:
                return None
            shown = subprocess.run(
                ["git", "show", f"{commit}:{Path(relative).as_posix()}"], cwd=str(self.repository),
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                env=child_process_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            saved[str(path)] = current_bytes
            start_bytes[str(path)] = shown.stdout if shown.returncode == 0 else None
        if all(saved[p] == start_bytes[p] for p in saved):
            return None
        check_baseline.write_swap_journal(journal, saved)
        try:
            check_baseline.place(start_bytes)
            result = self._run(
                plan.argv,
                min(plan.effective_timeout, check_baseline.BASELINE_TIMEOUT_SECONDS),
                keep_full=True,
            )
        except Exception:  # noqa: BLE001 - restore below; no baseline
            result = None
        finally:
            check_baseline.place(saved)
            if not check_baseline.verify_placed(saved):
                raise RuntimeError(
                    "KaroX could not restore the working tree after a baseline "
                    f"check; the journal {journal} holds the bytes"
                )
            journal.unlink(missing_ok=True)
        return None if result is None else check_baseline.of_result(result)

    def _run_inline_check(self, plan: CheckPlan) -> Dict[str, Any]:
        """Run ``python -c CODE`` from a temporary file outside the repository.

        ``python -c`` puts the working directory first on sys.path; a script
        puts its own directory there instead, so the first line restores the
        repository root to keep ``import package`` working exactly as inline.
        """

        scratch = tempfile.mkdtemp(prefix="karox-inline-")
        try:
            script = os.path.join(scratch, "inline_check.py")
            # One header line, so traceback line numbers stay off by one only.
            header = (
                "import sys as _karox_sys; _karox_sys.path.insert(0, ''); "
                f"del _karox_sys; exec({_INLINE_THREAD_REAPER!r})\n"
            )
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(header + str(plan.inline_code))
            result = self._run(
                [*plan.inline_prefix, script, *plan.inline_tail],
                plan.effective_timeout,
                keep_full=True,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        # Report the command the model asked for, not the temporary path.
        result["argv"] = list(plan.argv)
        result["inline_code_from_file"] = True
        return result

    def _shell_rule_matches(self, raw: Any) -> bool:
        """True when an approved rule names this shell literally and matches ``raw``."""

        rules = self._verification_rules
        if not rules or not isinstance(raw, list) or not raw or not all(isinstance(a, str) for a in raw):
            return False
        return any(
            rule.prefix
            and rule.prefix[0] == raw[0]
            and raw[0].replace("\\", "/").rsplit("/", 1)[-1].lower() in _APPROVABLE_SHELLS
            and rule.matches(raw)
            for rule in rules
        )

    def _prepare_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> CheckPlan:
        raw = self._required(arguments, "argv", list)
        argv = self._validate_process(raw, approved_shell=self._shell_rule_matches(raw))
        eligible = self._verification_rules is not None and any(
            rule.matches(argv) for rule in self._verification_rules
        )
        inline = None
        if self._verification_rules is not None and not eligible:
            inline = inline_python_rule(self._verification_rules, argv)
            if inline is not None:
                eligible = True
        probe = None
        if (
            self._verification_rules is not None
            and not eligible
            and getattr(self, "_allow_probes", False)
        ):
            probe = probe_python_command(
                self._verification_rules, argv, self.repository
            )
        # Refusing here rather than after the run keeps a rejected command from
        # reserving a durable idempotency intent that then needs reconciliation.
        # The refusal names the command and the approved set, because a model told
        # only "not approved" retries variants of the same rejected argv while a
        # model shown the list picks one that will run.
        if self._verification_rules is not None and not eligible and probe is None:
            approved = ", ".join(
                " ".join(rule.prefix) for rule in self._verification_rules
            )
            hint = ""
            # The most common refusal under a "python *" rule is inline code
            # (python -c ...): the wildcard deliberately refuses code-execution
            # flags. Saying so, with the route that does run, saves the model the
            # several turns it otherwise spends guessing at variants.
            if any(
                rule.wildcard
                and tuple(argv[: len(rule.prefix)]) == rule.prefix
                and not all(is_plain_argument(a) for a in argv[len(rule.prefix) :])
                for rule in self._verification_rules
            ):
                hint = (
                    "; under a wildcard rule inline code runs only as exactly "
                    "'<interpreter> -c CODE' (no other flags such as -W or -X); "
                    "otherwise write a scratch .py file, run it, then delete it"
                )
            interpreters = (
                probe_interpreters(self._verification_rules)
                if getattr(self, "_allow_probes", False)
                else ()
            )
            if interpreters:
                hint = (
                    "; probes (never verification evidence) run as "
                    + probe_forms_text(interpreters)
                )
            raise VerificationCommandNotApproved(
                "check command is not in the user-approved verification set: "
                + _path_label(" ".join(argv))
                + ("; approved commands: " + approved if approved else "")
                + hint
            )
        requested = float(
            arguments.get("timeout_seconds", self.DEFAULT_CHECK_TIMEOUT_SECONDS)
        )
        if not math.isfinite(requested) or requested <= 0:
            raise InvalidCommand("timeout_seconds must be positive")
        effective = requested
        # Every clamp that fired is named, not just the last one. Reporting one
        # cause when two applied is the same half-truth the silent clamp was.
        causes: List[str] = []
        if effective > float(deadline_seconds):
            effective = float(deadline_seconds)
            causes.append("request_deadline")
        if effective > self.MAX_PROCESS_TIMEOUT_SECONDS:
            effective = self.MAX_PROCESS_TIMEOUT_SECONDS
            causes.append("runtime_maximum")
        # One check may not eat the run: HARD12 runs spent 30-60 minutes of a
        # 60-minute budget on whole-suite checks the model gave 1800-3600 s.
        # The cap (KAROX_MAX_CHECK_SECONDS, default 600) keeps the narrow test
        # loop fast; a check that needs more is a check that should be narrower.
        check_cap = _max_check_seconds()
        if effective > check_cap:
            effective = check_cap
            causes.append("check_cap")
        if effective < self.MIN_PROCESS_TIMEOUT_SECONDS:
            effective = self.MIN_PROCESS_TIMEOUT_SECONDS
            causes.append("runtime_minimum")
        if inline is not None:
            return CheckPlan(
                argv,
                requested,
                effective,
                "+".join(causes) or None,
                eligible,
                inline_code=inline[1],
                inline_tail=inline[2],
                inline_prefix=inline[0],
            )
        if probe is not None:
            inline_probe = probe.form == "inline"
            return CheckPlan(
                argv,
                requested,
                effective,
                "+".join(causes) or None,
                False,
                inline_code=probe.code if inline_probe else None,
                inline_tail=probe.tail if inline_probe else (),
                inline_prefix=(probe.interpreter,) if inline_probe else (),
                probe=True,
            )
        return CheckPlan(argv, requested, effective, "+".join(causes) or None, eligible)