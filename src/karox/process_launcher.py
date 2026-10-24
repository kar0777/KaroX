"""Safe executable resolution for guarded subprocess launches.

On Windows a command like ``npm`` lives as ``npm.cmd``: ``CreateProcess``
does not consult ``PATHEXT`` for ``.cmd`` scripts when ``shell=False``, so
``subprocess.Popen(["npm", ...], shell=False)`` fails with ``WinError 2`` even
though ``npm`` is on ``PATH``.  This module replaces the *first* argv element
with an absolute path to the resolved executable (``npm.cmd`` etc.) so every
guarded runner keeps ``shell=False``, argv arrays, repo-scoped cwd, the
command allowlist (which already matched on the logical argv), and redaction.

It never builds a shell string, never sets ``shell=True``, and never alters
the non-first arguments.
"""

from __future__ import annotations

import os
import shutil
from typing import Sequence


# Bare command names that are almost always a ``.cmd`` shim on Windows.
_CMD_SHIM_NAMES = frozenset({"npm", "npx", "pnpm", "yarn", "pnpx"})

# Suffix search order for an extension-less bare name on Windows.  ``.cmd``
# first matches the Node ecosystem shims the KaroX allowlists expose
# (``npm``/``npx``/``pnpm``/``yarn``); ``.exe``/``.bat``/``.com`` cover the
# remaining PATHEXT cases that ``CreateProcess`` will not probe on its own.
_WINDOWS_SUFFIXES = (".cmd", ".exe", ".bat", ".com")


class ExecutableResolutionError(FileNotFoundError):
    """Raised when a guarded command's executable cannot be found.

    A dedicated subclass of ``FileNotFoundError`` (so existing ``except
    OSError``/``except FileNotFoundError`` handlers still catch it) that
    carries the unresolved name, letting the bridge classify a missing
    executable distinctly from a missing repository.
    """

    def __init__(self, executable: str) -> None:
        super().__init__(
            f"could not resolve executable for guarded command: {executable!r}"
        )
        self.executable = executable


def is_executable_resolution_error(exc: BaseException) -> bool:
    """True iff ``exc`` is the missing-executable failure this module raises."""
    return isinstance(exc, ExecutableResolutionError)


def _has_executable_suffix(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in _WINDOWS_SUFFIXES) or lower.endswith(
        ".ps1"
    )


def _real_python_from_path(name: str) -> str | None:
    """Prefer a real CPython install over Windows Store/Manager aliases.

    Microsoft Store aliases can be first on PATH while a normal CPython install
    appears immediately afterwards. They work in an interactive shell because
    Windows may broker them, but they are not a reliable target for guarded
    ``CreateProcess`` launches. Preserve PATH order and only skip entries whose
    directory is a WindowsApps alias location.
    """
    lower = name.lower()
    if lower not in {"python", "python.exe", "python3", "python3.exe"}:
        return None
    executable = "python3.exe" if lower.startswith("python3") else "python.exe"
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        directory = raw_dir.strip().strip('"')
        if not directory:
            continue
        normalized = os.path.normcase(os.path.abspath(directory))
        if "windowsapps" in normalized:
            continue
        candidate = os.path.join(directory, executable)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _resolve_windows_first(name: str) -> str:
    real_python = _real_python_from_path(name)
    if real_python:
        return real_python

    # An absolute or relative path with a suffix is handed straight to
    # ``CreateProcess`` (which accepts an explicit script path).  A bare name
    # needs the suffix probe because ``shell=False`` skips ``PATHEXT``.
    if os.path.sep in name or os.path.altsep in name or _has_executable_suffix(name):
        resolved = shutil.which(name)
        if resolved:
            return resolved
        raise ExecutableResolutionError(name)

    # Prefer the documented ``.cmd`` shims for the Node package managers the
    # allowlists expose, then the generic suffix sweep.  ``shutil.which``
    # searches ``PATH`` with ``PATHEXT`` on Windows, so passing ``npm.cmd``
    # finds the absolute path; passing bare ``npm`` would also resolve here,
    # but the explicit sweep documents intent and keeps the order stable
    # across machines where ``PATHEXT`` ordering differs.
    if name in _CMD_SHIM_NAMES:
        resolved = shutil.which(name + ".cmd")
        if resolved:
            return resolved
    for suffix in _WINDOWS_SUFFIXES:
        resolved = shutil.which(name + suffix)
        if resolved:
            return resolved
    # Last resort: the bare name (e.g. an ``.exe`` whose name has no PATHEXT
    # ambiguity, or a tool whose shim uses an exotic extension).
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise ExecutableResolutionError(name)


def resolve_executable(argv: Sequence[str]) -> list[str]:
    """Return a copy of ``argv`` whose first element is an absolute path.

    On non-Windows hosts the input is returned unchanged: ``execvp`` consults
    ``PATH`` directly and there is no ``.cmd`` resolution gap.  On Windows the
    first element is replaced with the resolved absolute executable (``npm.cmd``
    etc.) when needed; all remaining arguments are preserved verbatim.  No
    shell string is ever built.
    """
    if not argv:
        return list(argv)
    if os.name != "nt":
        return list(argv)
    resolved = _resolve_windows_first(argv[0])
    return [resolved, *argv[1:]]


# Readable alias for call sites that talk about "the process argv" rather than
# "the executable"; the two are the same operation.
def resolve_process_argv(argv: Sequence[str]) -> list[str]:
    return resolve_executable(argv)


__all__ = [
    "ExecutableResolutionError",
    "is_executable_resolution_error",
    "resolve_executable",
    "resolve_process_argv",
]
