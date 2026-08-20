"""Legacy/explicit Windows autostart support for saved bridges.

KaroX v5 does **not** register itself, a saved bridge, or its supervisor for
Windows logon/reboot startup during normal TUI Start/Restart.  Runtime recovery is
session-scoped: the detached saved-bridge supervisor may repair an owned bridge
while the user is running KaroX, but closing/logging out/rebooting does not opt the
user into starting KaroX again.

Older development builds did create a per-user Task Scheduler watchdog and a
Startup-folder script.  Their registration/removal primitives remain here so an
upgrade can identify and remove exactly those KaroX-owned entries, and so an
operator can explicitly experiment with ``--register`` outside the product flow.
The default OS entry point is intentionally self-retiring: if a legacy task fires,
it removes its own KaroX registration and exits without starting a supervisor or
bridge.

All machine mutations remain guarded against tests/sandbox runtime directories and
carry only a saved profile name, never bridge/browser credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

from .paths import config_dir, runtime_dir

ENTRY_MODULE = "karox.saved_bridge_autostart"
# Flat, prefixed task names: creating a Task Scheduler *folder* needs rights a
# standard account does not have, so the prefix is the namespace.
TASK_PREFIX = "KaroX-saved-bridge"
TASK_DESCRIPTION = (
    "KaroX restarts a saved MCP bridge that should be running. "
    "Delete this task to stop that from happening."
)
WATCH_INTERVAL_MINUTES = 5
# ``schtasks /TR`` truncates beyond this; a longer command must take the
# Startup-folder route instead of being silently registered broken. An XML
# definition has no such limit, which is why it is tried first.
MAX_TASK_COMMAND_CHARS = 261
SCHTASKS_TIMEOUT_SECONDS = 20.0
# The run log is append-only and unattended, so it is bounded rather than rotated.
LOG_MAX_BYTES = 64 * 1024

# Conservative on purpose: this value is interpolated into a command line that
# the OS will run at every logon. Every real saved profile name matches it.
_SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,96}$")
_DISABLED_VALUES = {"0", "off", "false", "no", "disable", "disabled"}

# Set only by ``--runtime-dir`` on the revival entry point: the state directory
# the *registering* process used, handed forward so the OS-launched process reads
# the same records instead of whatever its own environment happens to resolve to.
# A pinned value is therefore not a sandbox override -- see ``_runtime_dir_overridden``.
_pinned_runtime_dir: Optional[str] = None
PIN_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Environment                                                                 #
# --------------------------------------------------------------------------- #


def _is_windows() -> bool:
    return os.name == "nt"


def _under_test() -> bool:
    """True when this process is a test runner.

    Registering a Task Scheduler entry mutates the *machine*, not a sandbox, and
    a test cannot undo it for the user. Production KaroX never imports either
    module, so refusing here costs nothing and removes the whole class of
    accidents where a suite leaves per-user tasks behind.
    """
    return "unittest" in sys.modules or "pytest" in sys.modules


def _runtime_dir_overridden() -> bool:
    for name in ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR"):
        value = os.environ.get(name, "").strip()
        if value and value != _pinned_runtime_dir:
            return True
    return False


def _os_autostart_machine_access_supported() -> tuple[bool, str]:
    """Whether this process may inspect/remove real per-user OS startup entries."""
    if not _is_windows():
        return False, "not_windows"
    if _runtime_dir_overridden():
        # A sandbox must never mutate entries belonging to the real user profile.
        return False, "runtime_dir_override"
    if _under_test():
        return False, "test_runner"
    return True, "supported"


def os_autostart_supported() -> tuple[bool, str]:
    """Return whether explicit creation of OS autostart entries is allowed."""
    supported, reason = _os_autostart_machine_access_supported()
    if not supported:
        return supported, reason
    if os.environ.get("KAROX_OS_AUTOSTART", "").strip().lower() in _DISABLED_VALUES:
        return False, "disabled_by_env"
    return True, "supported"


def schtasks_path() -> Optional[str]:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = Path(root) / "System32" / "schtasks.exe"
    if candidate.exists():
        return str(candidate)
    return None


def windowless_python() -> str:
    """Prefer ``pythonw.exe`` so a logon or 5-minute tick never flashes a window."""
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


# --------------------------------------------------------------------------- #
# Identity of the tasks                                                       #
# --------------------------------------------------------------------------- #


def _profile_digest(profile_name: str) -> str:
    # Same scheme as the supervisor's state files, so one profile's OS tasks and
    # its runtime records are recognisably the same family.
    return hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]


def _profile_name_is_safe(profile_name: str) -> bool:
    return bool(_SAFE_PROFILE_NAME.match(profile_name or ""))


def autostart_task_name(profile_name: str) -> str:
    """Return the watchdog task name for ``profile_name``."""
    return f"{TASK_PREFIX}-{_profile_digest(profile_name)}-watch"


def autostart_command_parts(profile_name: str) -> tuple[str, str]:
    """The command split the way Task Scheduler stores it: program, then arguments.

    Both registration paths need the same two halves -- ``schtasks /Create`` joins
    them into one ``/TR`` string, an XML definition keeps them in ``<Command>`` and
    ``<Arguments>`` -- and a task registered either way must read back identically,
    because that read-back is how :func:`_task_matches` recognises its own work.
    """
    pinned = _quoted_path(runtime_dir())
    program = f'"{windowless_python()}"'
    arguments = f'-m {ENTRY_MODULE} --saved "{profile_name}" --runtime-dir {pinned}'
    return program, arguments


def autostart_command(profile_name: str) -> str:
    """The exact command a task runs. It carries no secret, only the profile.

    ``--runtime-dir`` exists because an OS-launched process inherits none of the
    environment of the session that registered it, so it would otherwise *re-derive*
    where KaroX keeps its state instead of being told. Anything that makes that
    derivation context-dependent -- ``KAROX_RUNTIME_DIR`` set in one shell only, a
    redirected or roaming ``LOCALAPPDATA``, a junction that resolves differently
    for a service-launched process -- makes the watchdog read ``desired_running``
    from an empty store and exit 0 having revived nothing. Passing the registering
    process's own directory removes the derivation, and with it the whole class of
    silent no-ops that are impossible to diagnose from the task's exit code.
    """
    program, arguments = autostart_command_parts(profile_name)
    return f"{program} {arguments}"



def _quoted_path(path: Path) -> str:
    return f'"{str(path).rstrip(chr(92))}"'


def pin_path(profile_name: str) -> Path:
    """Where the registering process records the paths the task must reuse."""
    return runtime_dir() / "saved-bridge-autostart" / f"{_profile_digest(profile_name)}.json"


def log_path(profile_name: str) -> Path:
    """Where each unattended revival run records what it did."""
    return runtime_dir() / "saved-bridge-autostart" / f"{_profile_digest(profile_name)}.log"


def _trim_log(target: Path) -> None:
    """Keep the newest half of the log once it passes ``LOG_MAX_BYTES``.

    A file the OS appends to every five minutes forever needs a bound, and the
    newest lines are the ones a diagnosis needs.
    """
    try:
        if target.stat().st_size <= LOG_MAX_BYTES:
            return
        lines = target.read_bytes().splitlines(keepends=True)
        target.write_bytes(b"".join(lines[len(lines) // 2 :]))
    except OSError:
        pass


def _log(profile_name: str, outcome: str, **fields: Any) -> None:
    """Append one line about this run. Never raises, never blocks the revival.

    Without this, an unattended mechanism is undiagnosable: the Task Scheduler
    keeps a single integer per task, so a run that exits 1 for any reason at all
    looks identical to every other kind of failure -- measured the hard way on
    this machine, where a ``Last Result: 1`` said nothing about which of six code
    paths produced it. One JSON line per run costs nothing and answers it.
    """
    record: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "outcome": outcome,
        "pid": os.getpid(),
        "python": sys.executable,
        "cwd": os.getcwd(),
        **fields,
    }
    try:
        target = log_path(profile_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        _trim_log(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _write_pin(profile_name: str) -> tuple[bool, bool, str]:
    """Record every path the revived bridge needs. Returns ``(ok, written, reason)``.

    Only ``--runtime-dir`` fits comfortably in a 261-character task command, so
    the rest of the environment travels in a small file *inside* that directory,
    which the task can always find once it has been told where the directory is.

    An unchanged pin is left alone, timestamp included: the watchdog calls this
    every five minutes, and a file rewritten on every tick would make the report's
    ``created`` list -- the only audit trail this layer has -- meaningless.
    """
    target = pin_path(profile_name)
    payload = {
        "schema_version": PIN_SCHEMA_VERSION,
        "saved_profile": profile_name,
        "runtime_dir": str(runtime_dir()),
        "config_dir": str(config_dir()),
        "python": windowless_python(),
    }
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(current, dict) and all(
            current.get(key) == value for key, value in payload.items()
        ):
            return True, False, "already_registered"
    except (OSError, ValueError):
        pass
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({**payload, "registered_at": time.time()}, indent=1, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        return False, False, f"pin_failed:{type(exc).__name__}"
    return True, True, "ok"


def _apply_pin(runtime_dir_value: str, profile_name: str) -> dict[str, Any]:
    """Adopt the registering process's paths before any state is read.

    Called from ``main`` only. The environment variables set here are the same
    knobs the user already has, and the values come from a file this user's own
    KaroX wrote inside their own runtime directory -- nothing new is trusted.
    Returns what was adopted, for the ``--status`` report.
    """
    global _pinned_runtime_dir
    pinned = runtime_dir_value.strip().rstrip("\\")
    adopted: dict[str, Any] = {"runtime_dir": None, "config_dir": None}
    if not pinned:
        return adopted
    _pinned_runtime_dir = pinned
    os.environ["KAROX_RUNTIME_DIR"] = pinned
    os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)
    adopted["runtime_dir"] = pinned
    # ``runtime_dir()`` now answers ``pinned``, so the pin file is findable.
    try:
        payload = json.loads(pin_path(profile_name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return adopted
    config = str(payload.get("config_dir") or "").strip()
    if config and payload.get("saved_profile") == profile_name:
        os.environ["KAROX_CONFIG_DIR"] = config
        os.environ.pop("KAROX_VNEXT_CONFIG_DIR", None)
        adopted["config_dir"] = config
    return adopted


def startup_script_path(profile_name: str) -> Path:
    return startup_dir() / f"karox-saved-bridge-{_profile_digest(profile_name)}.cmd"


# --------------------------------------------------------------------------- #
# schtasks plumbing                                                           #
# --------------------------------------------------------------------------- #


def _decode_output(raw: bytes) -> str:
    """Decode ``schtasks`` output without ever raising.

    ``subprocess(text=True)`` is unusable here: on a localised Windows the tool
    writes bytes the ANSI code page cannot decode, the failure happens on a
    reader thread, and the caller sees ``stderr is None`` with no explanation --
    a real failure mode observed on a Russian Windows 11. The command strings
    this module matches are pure ASCII, and every codec in this chain preserves
    ASCII, so a mangled localised label can never hide the command.
    """
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for codec in ("utf-8", "cp866", "cp1251", "latin-1"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _run_schtasks(args: list[str]) -> tuple[int, str]:
    """Run ``schtasks`` with ``args``; return ``(returncode, combined output)``."""
    executable = schtasks_path()
    if executable is None:
        return 127, "schtasks.exe not found"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0
    try:
        completed = subprocess.run(  # noqa: S603 - fixed system executable
            [executable, *args],
            capture_output=True,
            timeout=SCHTASKS_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}"
    output = f"{_decode_output(completed.stdout)}\n{_decode_output(completed.stderr)}"
    return completed.returncode, output


def _task_matches(task_name: str, command: str) -> bool:
    """True when the registered task already runs exactly ``command``.

    ``/Query`` output is localised, so no field label is parsed: the command
    string itself is language-independent, and matching it also detects a task
    left pointing at an interpreter that has since moved.
    """
    code, output = _run_schtasks(["/Query", "/TN", task_name, "/FO", "LIST", "/V"])
    if code != 0:
        return False
    return command.lower() in output.lower()


# The settings a ``schtasks /Create`` command line cannot express, and which decide
# whether the watchdog runs at all. Measured on a task created by the CLI path:
# "Не запускать при питании от батареи" -- so on any laptop that is not plugged in,
# the layer that exists to survive a reboot never runs. ``ExecutionTimeLimit`` is
# absent there too, which means the XML default of 72 hours applies to a task whose
# job object now contains the revived bridge itself.
_REQUIRED_TASK_SETTINGS = (
    "<DisallowStartIfOnBatteries>false<",
    "<StopIfGoingOnBatteries>false<",
    "<ExecutionTimeLimit>PT0S<",
)


def _task_settings_are_current(task_name: str) -> bool:
    """True when the task already carries the settings above.

    A task the CLI path created matches its command perfectly, so command matching
    alone would keep a battery-crippled task forever. When Windows will not hand
    back XML at all, this answers ``True``: churning a working task every five
    minutes is worse than leaving one imperfect setting in place.
    """
    code, output = _run_schtasks(["/Query", "/TN", task_name, "/XML"])
    if code != 0:
        return True
    return all(marker.lower() in output.lower() for marker in _REQUIRED_TASK_SETTINGS)


def _task_principal() -> str:
    """``DOMAIN\\user`` for the current account, or ``""`` when it cannot be known.

    Only the logon trigger needs it. Without it the XML still registers -- against
    the invoking user, which is this process -- and the Startup-folder script keeps
    covering logon, so an unknown account costs a redundancy, not the feature.
    """
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return ""
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain else user


def task_definition_xml(profile_name: str) -> str:
    """The watch task as Task Scheduler XML.

    Everything here that ``schtasks /Create`` cannot say is the reason this path
    exists: it runs on battery, it is never stopped by an execution time limit, it
    catches up a trigger missed while the machine slept, and -- registered from XML,
    unlike ``/SC ONLOGON``, which a standard account is refused -- it also fires at
    logon. ``IgnoreNew`` keeps a slow tick from stacking on the next one.
    """
    program, arguments = autostart_command_parts(profile_name)
    principal = _task_principal()
    logon_trigger = (
        f"    <LogonTrigger>\n      <UserId>{escape(principal)}</UserId>\n    </LogonTrigger>\n"
        if principal
        else ""
    )
    start_boundary = time.strftime("%Y-%m-%dT%H:%M:00")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>{escape(TASK_DESCRIPTION)}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        f"{logon_trigger}"
        "    <TimeTrigger>\n"
        f"      <StartBoundary>{start_boundary}</StartBoundary>\n"
        "      <Repetition>\n"
        f"        <Interval>PT{WATCH_INTERVAL_MINUTES}M</Interval>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>false</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(program)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def task_definition_path(profile_name: str) -> Path:
    """Where the XML handed to ``schtasks`` is written, next to the pin it pairs with."""
    return runtime_dir() / "saved-bridge-autostart" / f"{_profile_digest(profile_name)}.task.xml"


def _create_watch_task_from_xml(profile_name: str, task_name: str) -> tuple[bool, str]:
    """Register the watch task from a full definition. Returns ``(ok, reason)``.

    The file is written as UTF-16 with a byte-order mark: that is what the XML
    declaration promises and what ``schtasks`` reads. It is deleted afterwards --
    the registered task is the durable artefact, and a stale definition on disk
    would only invite someone to edit the copy that no longer matters.
    """
    target = task_definition_path(profile_name)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xfe" + task_definition_xml(profile_name).encode("utf-16-le"))
    except OSError as exc:
        return False, f"xml_write_failed:{type(exc).__name__}"
    try:
        code, output = _run_schtasks(["/Create", "/TN", task_name, "/XML", str(target), "/F"])
    finally:
        try:
            target.unlink()
        except OSError:
            pass
    if code != 0:
        return False, f"xml_refused:{output.strip()[:120]}"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Registration                                                                #
# --------------------------------------------------------------------------- #


def _report(profile_name: str, **fields: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "saved_profile": profile_name,
        "mechanism": "none",
        "registered": False,
        "created": [],
        "tasks": [],
        "reason": "",
    }
    report.update(fields)
    return report


def _write_startup_script(profile_name: str) -> tuple[bool, bool, str]:
    """Write the logon script into the user's own Startup folder.

    Always available: no privilege beyond writing to one's own profile. It fires
    once per logon, which is precisely the trigger ``schtasks`` will not grant a
    standard account. Returns ``(ok, written, reason)``; an unchanged script is
    reported as ``already_registered`` so a repeated ensure stays quiet.
    """
    script = startup_script_path(profile_name)
    body = (
        "@echo off\r\n"
        "rem Written by KaroX so a saved bridge survives logoff and reboot.\r\n"
        "rem Delete this file to stop the bridge from starting at logon.\r\n"
        f'start "" /b {autostart_command(profile_name)}\r\n'
    ).encode("utf-8")
    # Bytes, not text: text mode would translate the CRLF endings this file needs
    # into CRCRLF, and the comparison below would never match its own output.
    try:
        if script.read_bytes() == body:
            return True, False, "already_registered"
    except OSError:
        pass
    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(body)
    except OSError as exc:
        return False, False, f"startup_folder_failed:{type(exc).__name__}"
    return True, True, "ok"


def _create_watch_task(profile_name: str, command: str) -> tuple[bool, bool, str]:
    """Ensure the 5-minute watchdog task exists. Returns ``(ok, created, reason)``.

    XML first, because only a full definition can say "run on battery" and "no
    execution time limit"; the ``schtasks`` command line is kept as the fallback for
    a Windows that refuses XML, since a task with awkward defaults still revives the
    bridge and no task at all does not.
    """
    task = autostart_task_name(profile_name)
    if _task_matches(task, command) and _task_settings_are_current(task):
        return True, False, "already_registered"
    xml_ok, xml_reason = _create_watch_task_from_xml(profile_name, task)
    if xml_ok:
        return True, True, "ok_xml"
    if len(command) > MAX_TASK_COMMAND_CHARS:
        # ``/TR`` would be truncated, which registers a task that fails forever.
        return False, False, f"{xml_reason},command_too_long"
    code, output = _run_schtasks(
        [
            "/Create",
            "/TN",
            task,
            "/TR",
            command,
            "/SC",
            "MINUTE",
            "/MO",
            str(WATCH_INTERVAL_MINUTES),
            # Per-user, not elevated, no stored password, overwrite in place.
            "/RL",
            "LIMITED",
            "/F",
        ]
    )
    if code != 0:
        return False, False, f"schtasks_failed:{output.strip()[:120]}"
    return True, True, "ok_command_line"


def register_autostart(profile_name: str) -> dict[str, Any]:
    """Create (or repair) the OS entries that revive ``profile_name``.

    Idempotent: a task that already runs the exact command is left untouched and
    the Startup script is rewritten byte-identically, so this is safe to call on
    every bridge start and on every watchdog tick. Registration succeeds if
    *either* mechanism took, and the report says which.
    """
    if not _profile_name_is_safe(profile_name):
        return _report(profile_name, reason="unsupported_profile_name")
    command = autostart_command(profile_name)
    created: list[str] = []
    reasons: list[str] = []

    # First, because both mechanisms below are useless without it: the paths the
    # revived bridge must reuse.
    pin_ok, pin_written, pin_reason = _write_pin(profile_name)
    if pin_written:
        created.append(str(pin_path(profile_name)))
    if not pin_ok:
        reasons.append(pin_reason)

    task_ok = False
    if schtasks_path() is None:
        reasons.append("schtasks_unavailable")
    else:
        task_ok, task_created, task_reason = _create_watch_task(profile_name, command)
        reasons.append(task_reason)
        if task_created:
            created.append(autostart_task_name(profile_name))

    startup_ok, startup_written, startup_reason = _write_startup_script(profile_name)
    if startup_written:
        created.append(str(startup_script_path(profile_name)))
    if not startup_ok or not startup_written:
        reasons.append(startup_reason)

    if task_ok and startup_ok:
        mechanism = "schtasks+startup_folder"
    elif task_ok:
        mechanism = "schtasks"
    elif startup_ok:
        mechanism = "startup_folder"
    else:
        mechanism = "none"
    return _report(
        profile_name,
        mechanism=mechanism,
        registered=task_ok or startup_ok,
        created=created,
        tasks=[autostart_task_name(profile_name)] if task_ok else [],
        startup_script=str(startup_script_path(profile_name)) if startup_ok else None,
        reason=";".join(dict.fromkeys(reason for reason in reasons if reason)) or "ok",
    )


def remove_autostart(profile_name: str) -> dict[str, Any]:
    """Delete every OS entry for ``profile_name``. Never fails on a missing one."""
    removed: list[str] = []
    if _profile_name_is_safe(profile_name) and schtasks_path() is not None:
        task = autostart_task_name(profile_name)
        code, _output = _run_schtasks(["/Delete", "/TN", task, "/F"])
        if code == 0:
            removed.append(task)
    script = startup_script_path(profile_name)
    try:
        if script.exists():
            script.unlink()
            removed.append(str(script))
    except OSError:
        pass
    pin = pin_path(profile_name)
    try:
        if pin.exists():
            pin.unlink()
            removed.append(str(pin))
    except OSError:
        pass
    log = log_path(profile_name)
    try:
        if log.exists():
            log.unlink()
            removed.append(str(log))
    except OSError:
        pass
    # Normally deleted the moment ``schtasks`` has read it; present only if a
    # registration was interrupted between writing and registering.
    definition = task_definition_path(profile_name)
    try:
        if definition.exists():
            definition.unlink()
            removed.append(str(definition))
    except OSError:
        pass
    report = _report(profile_name, reason="ok", tasks=removed)
    report["removed"] = True
    report["deleted"] = removed
    return report


def autostart_status(profile_name: str) -> dict[str, Any]:
    """Report which OS entries exist right now, without changing any of them."""
    command = autostart_command(profile_name)
    present: list[str] = []
    if _profile_name_is_safe(profile_name) and schtasks_path() is not None:
        task = autostart_task_name(profile_name)
        if _task_matches(task, command):
            present.append(task)
    script = startup_script_path(profile_name)
    try:
        script_present = script.exists()
    except OSError:
        script_present = False
    if present and script_present:
        mechanism = "schtasks+startup_folder"
    elif present:
        mechanism = "schtasks"
    elif script_present:
        mechanism = "startup_folder"
    else:
        mechanism = "none"
    supported, reason = os_autostart_supported()
    pin = pin_path(profile_name)
    try:
        payload = json.loads(pin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    return _report(
        profile_name,
        mechanism=mechanism,
        registered=bool(present or script_present),
        tasks=present,
        reason=reason,
        supported=supported,
        startup_script=str(script) if script_present else None,
        pin_file=str(pin) if payload else None,
        pin_runtime_dir=payload.get("runtime_dir"),
        pin_config_dir=payload.get("config_dir"),
        last_runs=_recent_runs(profile_name),
        watch_interval_minutes=WATCH_INTERVAL_MINUTES,
    )


def _recent_runs(profile_name: str, limit: int = 5) -> list[dict[str, Any]]:
    """The newest lines of the run log, so ``--status`` explains itself.

    The Task Scheduler's own answer to "did the watchdog work?" is one integer.
    This is the part a user can act on.
    """
    try:
        raw = log_path(profile_name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    runs: list[dict[str, Any]] = []
    for line in raw.splitlines()[-limit:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            runs.append(record)
    return runs


# --------------------------------------------------------------------------- #
# Guarded entry points used by production code                                 #
# --------------------------------------------------------------------------- #


def ensure_saved_bridge_autostart(profile_name: str) -> dict[str, Any]:
    """Best-effort registration. Never raises: a bridge start must not depend on it."""
    supported, reason = os_autostart_supported()
    if not supported:
        return _report(profile_name, reason=reason, supported=False)
    try:
        report = register_autostart(profile_name)
    except Exception as exc:  # never let the OS layer break a bridge start
        return _report(profile_name, reason=f"error:{type(exc).__name__}", supported=True)
    report["supported"] = True
    return report


def remove_saved_bridge_autostart(profile_name: str) -> dict[str, Any]:
    """Best-effort cleanup of legacy entries, even when future creation is disabled."""
    supported, reason = _os_autostart_machine_access_supported()
    if not supported:
        return _report(profile_name, reason=reason, supported=False)
    try:
        report = remove_autostart(profile_name)
    except Exception as exc:
        return _report(profile_name, reason=f"error:{type(exc).__name__}", supported=True)
    report["supported"] = True
    return report


# --------------------------------------------------------------------------- #
# The entry point the OS runs                                                  #
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    """Inspect/explicitly manage legacy entries; a default legacy tick self-retires."""
    parser = argparse.ArgumentParser(
        prog=f"python -m {ENTRY_MODULE}",
        description="Restart a saved KaroX bridge's supervisor when it should be running.",
    )
    parser.add_argument("--saved", required=True, help="saved web bridge profile name")
    parser.add_argument(
        "--runtime-dir",
        default="",
        help="state directory recorded at registration; adopted before any state is read",
    )
    parser.add_argument("--status", action="store_true", help="print OS registration as JSON")
    parser.add_argument("--register", action="store_true", help="register the OS entries now")
    parser.add_argument("--remove", action="store_true", help="delete the OS entries now")
    args = parser.parse_args(argv)

    # Before anything reads state: an OS-launched process inherits none of the
    # session environment that decides where KaroX keeps its records.
    adopted = _apply_pin(args.runtime_dir, args.saved)

    if args.status:
        status = autostart_status(args.saved)
        status["adopted"] = adopted
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        return 0
    if args.remove:
        # An explicit human action, so it is not subject to the automatic guard:
        # a user who disabled the layer must still be able to clean it up.
        print(json.dumps(remove_autostart(args.saved), ensure_ascii=False, sort_keys=True))
        return 0
    if args.register:
        print(
            json.dumps(
                ensure_saved_bridge_autostart(args.saved), ensure_ascii=False, sort_keys=True
            )
        )
        return 0

    # v5 no longer uses OS logon/reboot revival in the normal product path.
    # A task created by an older build may still invoke this module after an
    # upgrade, so make that invocation self-retiring: delete the exact KaroX-owned
    # task/startup file and exit without starting a supervisor or bridge.
    try:
        cleanup = remove_autostart(args.saved)
    except Exception as exc:
        _log(
            args.saved,
            "legacy_autostart_cleanup_failed",
            error=f"{type(exc).__name__}: {exc}"[:300],
            adopted=adopted,
        )
        return 1
    _log(
        args.saved,
        "legacy_autostart_retired",
        deleted=len(cleanup.get("deleted") or ()),
        adopted=adopted,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())


__all__ = [
    "ENTRY_MODULE",
    "LOG_MAX_BYTES",
    "MAX_TASK_COMMAND_CHARS",
    "PIN_SCHEMA_VERSION",
    "TASK_DESCRIPTION",
    "TASK_PREFIX",
    "WATCH_INTERVAL_MINUTES",
    "autostart_command",
    "autostart_command_parts",
    "autostart_status",
    "autostart_task_name",
    "ensure_saved_bridge_autostart",
    "log_path",
    "main",
    "os_autostart_supported",
    "pin_path",
    "register_autostart",
    "remove_autostart",
    "remove_saved_bridge_autostart",
    "startup_script_path",
    "task_definition_path",
    "task_definition_xml",
]
