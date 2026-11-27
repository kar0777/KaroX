"""`karox6 update`: install the newest KaroX from its git branch into this environment.

The source is a branch of a git repository (``karox6/main`` of the project's GitHub
repository by default; ``update_repo`` / ``update_branch`` in config.toml or
``KAROX_UPDATE_REPO`` / ``KAROX_UPDATE_BRANCH`` change it). Each update:

1. clones that branch into a new temporary folder (never into a working copy),
2. compares its version and commit with the installed ones and lists the commits between,
3. writes ``karox6/_build.py`` (repository, branch, commit) into the clone and builds the
   wheel with this interpreter's pip (or uv, for an environment made without pip),
4. checks the wheel before touching the installation: its version, and that it starts
   (``--version``) from a scratch folder,
5. installs its dependencies, then the wheel itself (``pip install --force-reinstall
   --no-deps``) into the interpreter KaroX runs on (``sys.executable``).

A failure at any step leaves the installed KaroX as it was. On Windows the running
``karox6.exe`` cannot be overwritten but can be renamed: the launchers are moved aside to
``*.exe.old`` before pip runs (and put back if it fails) and deleted on a later start.

The background check of the chat (:func:`check_cached`) asks the network at most once a
day, reads only the branch's version file and commit, and never installs anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.tui_text import _plain_text

DEFAULT_REPO = "https://github.com/kar0777/KaroX.git"
DEFAULT_BRANCH = "karox6/main"
CHECK_EVERY_S = 24 * 3600
STATE_FILE = "update-check.json"
_VERSION_LINE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_PEP440 = re.compile(
    r"^v?(\d+(?:\.\d+)*)(?:[-_.]?(a|b|rc|alpha|beta|c)[-_.]?(\d*))?"
    r"(?:[-_.]?post[-_.]?(\d*))?(?:[-_.]?dev[-_.]?(\d*))?(?:\+.*)?$",
    re.IGNORECASE,
)
_PRE = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2}

Out = Callable[[str], None]


class UpdateError(RuntimeError):
    """A step failed; the message says what happened and what to do."""


@dataclass(frozen=True, slots=True)
class Source:
    repo: str = DEFAULT_REPO
    branch: str = DEFAULT_BRANCH

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any] | None = None,
        repo: str | None = None,
        branch: str | None = None,
    ) -> Source:
        settings = settings or {}
        return cls(
            repo=repo
            or os.environ.get("KAROX_UPDATE_REPO")
            or settings.get("update_repo")
            or DEFAULT_REPO,
            branch=branch
            or os.environ.get("KAROX_UPDATE_BRANCH")
            or settings.get("update_branch")
            or DEFAULT_BRANCH,
        )


@dataclass(slots=True)
class Installed:
    version: str
    commit: str = ""
    branch: str = ""


@dataclass(slots=True)
class Result:
    status: str  # "updated" | "current" | "older" | "available"
    old: Installed
    new_version: str
    new_commit: str
    changes: list[str] = field(default_factory=list)


def version_key(version: str) -> tuple[Any, ...]:
    """A sort key for PEP 440 versions (release, pre, post, dev); unknown text sorts first."""
    match = _PEP440.match(version.strip())
    if match is None:
        return ((-1,),)
    release = tuple(int(x) for x in match.group(1).split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    pre_tag, pre_n, post, dev = match.group(2), match.group(3), match.group(4), match.group(5)
    if pre_tag is None and post is None and dev is not None:
        pre: tuple[int, int] = (-1, 0)  # 1.0.dev1 < 1.0a1
    elif pre_tag is None:
        pre = (9, 0)
    else:
        pre = (_PRE[pre_tag.lower()], int(pre_n or 0))
    post_key = -1 if post is None else int(post or 0)
    dev_key = 1 << 30 if dev is None else int(dev or 0)
    return (release, pre, post_key, dev_key)


def newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def installed(python: str | None = None, *, ask: bool = False) -> Installed:
    """The KaroX version and build of ``python``: this process's when None or the same
    interpreter, unless ``ask`` (after an install the running process is out of date)."""
    if not ask and _same_python(python):
        from karox6 import __version__

        build = _build_info()
        return Installed(__version__, build.get("commit", ""), build.get("branch", ""))
    code = (
        "import json, karox6\n"
        "try:\n from karox6 import _build as b; info = vars(b)\n"
        "except Exception:\n info = {}\n"
        "print(json.dumps({'version': karox6.__version__, 'commit': info.get('COMMIT', ''),"
        " 'branch': info.get('BRANCH', '')}))"
    )
    proc = _run([python or sys.executable, "-c", code], timeout=60)
    if proc.returncode != 0:
        return Installed("0")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return Installed(data["version"], data["commit"], data["branch"])


def _build_info() -> dict[str, str]:
    try:
        from karox6 import _build  # type: ignore[attr-defined]  # written into update builds
    except Exception:  # absent (a source tree or a wheel built otherwise) or unreadable
        return {}
    return {
        "commit": str(getattr(_build, "COMMIT", "")),
        "branch": str(getattr(_build, "BRANCH", "")),
        "repo": str(getattr(_build, "REPO", "")),
    }


def from_checkout() -> bool:
    """KaroX runs from a source tree (a developer's checkout, the tests), not from an
    installed wheel: the background check stays off there."""
    package = Path(__file__).resolve().parent
    return not any(part in ("site-packages", "dist-packages") for part in package.parts)


# -- the update itself ---------------------------------------------------------------------


def run_update(
    source: Source,
    out: Out,
    *,
    python: str | None = None,
    force: bool = False,
    check_only: bool = False,
    workdir: Path | None = None,
) -> Result:
    """Clone, compare, build, check and install; raises :class:`UpdateError` on failure."""
    _ABORT.clear()  # a new update re-arms; abort() only stops the one in flight
    python = python or sys.executable
    if shutil.which("git") is None:
        raise UpdateError(
            "git is not installed or not on PATH; install Git (https://git-scm.com) and run "
            "`karox6 update` again"
        )
    old = installed(python)
    temp = Path(tempfile.mkdtemp(prefix="karox6-update-", dir=workdir))
    try:
        out(f"fetching {source.branch} from {source.repo} ...")
        repo = temp / "repo"
        _git(
            temp,
            "clone",
            "--quiet",
            "--filter=blob:none",
            "--single-branch",
            "--branch",
            source.branch,
            source.repo,
            str(repo),
            what=f"cannot fetch {source.branch} from {source.repo}",
            timeout=600,
        )
        head = _git(repo, "rev-parse", "HEAD").strip()
        version = _read_version(repo / "src" / "karox6" / "__init__.py")
        status = _compare(repo, old, version, head)
        changes = _changes(repo, old, head) if status != "older" else []
        result = Result(status, old, version, head, changes)
        if check_only or (status in ("current", "older") and not force):
            return result
        _cleanup_old_launchers(python)
        (repo / "src" / "karox6" / "_build.py").write_text(
            '"""Written by `karox6 update`: where this build came from."""\n\n'
            f"REPO = {source.repo!r}\nBRANCH = {source.branch!r}\nCOMMIT = {head!r}\n",
            encoding="utf-8",
            newline="\n",
        )
        out(f"building KaroX {version} ({head[:7]}) ...")
        wheel = _build_wheel(python, repo, temp / "dist")
        out("checking the new build ...")
        requires = _check_wheel(python, wheel, version, temp / "check")
        out("installing ...")
        _install(python, wheel, requires)
        now = installed(python, ask=True)
        if now.version != version:
            raise UpdateError(
                f"pip reported success but KaroX {now.version} is installed, not {version}; "
                f"run `{python} -m pip install --force-reinstall {wheel.name}` to see why"
            )
        result.status = "updated"
        return result
    finally:
        _remove_tree(temp)


def _compare(repo: Path, old: Installed, version: str, head: str) -> str:
    if old.commit and old.commit == head:
        return "current"
    if newer(version, old.version):
        return "available"
    if newer(old.version, version):
        return "older"
    if not old.commit:
        return "current"  # same version, build unknown (installed some other way)
    known = _git_ok(repo, "merge-base", "--is-ancestor", head, old.commit)
    return "current" if known else "available"


def _changes(repo: Path, old: Installed, head: str, limit: int = 30) -> list[str]:
    """Titles of the commits between the installed build and ``head``, newest first."""
    base = ""
    if old.commit and _git_ok(repo, "cat-file", "-e", f"{old.commit}^{{commit}}"):
        base = old.commit
    elif old.version and old.version != "0":
        found = _git(
            repo,
            "log",
            "--format=%H",
            "-S",
            f'__version__ = "{old.version}"',
            "--",
            "src/karox6/__init__.py",
            check=False,
        ).split()
        base = found[-1] if found else ""  # the oldest: where that version was set
    span = f"{base}..{head}" if base else head
    log = _git(repo, "log", "--no-merges", f"-{limit + 1}", "--format=%h %s", span, check=False)
    lines = [line for line in log.splitlines() if line.strip()]
    if len(lines) > limit:
        lines = [*lines[:limit], "..."]
    return lines


def _read_version(init: Path) -> str:
    try:
        match = _VERSION_LINE.search(init.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UpdateError(f"the branch has no {init.name} of karox6 ({exc})") from exc
    if match is None:
        raise UpdateError("the branch's karox6/__init__.py has no __version__")
    return match.group(1)


def _installer(python: str) -> tuple[list[str], list[str]]:
    """(build a wheel, install) commands for ``python``'s environment: its own pip, or uv
    for an environment made without pip (``uv tool install karox6``)."""
    if _run([python, "-m", "pip", "--version"], timeout=60).returncode == 0:
        return (
            [python, "-m", "pip", "wheel", "--disable-pip-version-check", "--no-deps", "-w"],
            [python, "-m", "pip", "install", "--disable-pip-version-check"],
        )
    uv = shutil.which("uv")
    if uv:
        return [uv, "build", "--wheel", "--out-dir"], [uv, "pip", "install", "--python", python]
    raise UpdateError(
        f"{python} has no pip and uv is not on PATH; run `{python} -m ensurepip` (or install "
        "uv) and `karox6 update` again"
    )


def _build_wheel(python: str, repo: Path, dist: Path) -> Path:
    build, _install_cmd = _installer(python)
    proc = _run([*build, str(dist), str(repo)], timeout=900)
    wheels = sorted(dist.glob("karox6-*.whl"))
    if proc.returncode != 0 or not wheels:
        raise UpdateError("building the wheel failed:\n" + _tail(proc))
    return wheels[-1]


def _check_wheel(python: str, wheel: Path, version: str, scratch: Path) -> list[str]:
    """The wheel holds the expected version and starts; returns its requirements."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            init = archive.read("karox6/__init__.py").decode("utf-8")
            meta_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            metadata = archive.read(meta_name).decode("utf-8")
    except (OSError, KeyError, StopIteration, zipfile.BadZipFile) as exc:
        raise UpdateError(f"the built wheel {wheel.name} is not usable: {exc}") from exc
    match = _VERSION_LINE.search(init)
    if match is None or match.group(1) != version:
        raise UpdateError(f"the built wheel {wheel.name} does not hold KaroX {version}")
    _build, install = _installer(python)
    proc = _run([*install, "--no-deps", "--target", str(scratch), str(wheel)], timeout=300)
    if proc.returncode != 0:
        raise UpdateError("unpacking the new build failed:\n" + _tail(proc))
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import karox6.cli as c; "
        "raise SystemExit(c.main(['--version']))"
    )
    proc = _run([python, "-I", "-c", probe, str(scratch)], timeout=120)
    if proc.returncode != 0 or version not in proc.stdout:
        raise UpdateError("the new build does not start (`karox6 --version`):\n" + _tail(proc))
    return [
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:") and "extra ==" not in line
    ]


def _install(python: str, wheel: Path, requires: list[str]) -> None:
    _build, pip = _installer(python)
    if requires:
        proc = _run([*pip, *requires], timeout=900)
        if proc.returncode != 0:
            raise UpdateError(
                "installing the dependencies failed (KaroX itself was not changed):\n" + _tail(proc)
            )
    moved = _move_launchers(python, wheel)
    proc = _run([*pip, "--force-reinstall", "--no-deps", str(wheel)], timeout=900)
    if proc.returncode != 0:
        _restore_launchers(moved)
        raise UpdateError(
            "installing the new build failed (the installed KaroX was kept):\n" + _tail(proc)
        )
    for old in moved.values():
        with contextlib.suppress(OSError):
            old.unlink()  # works for launchers that were not running


# -- Windows launchers ---------------------------------------------------------------------


def _scripts_dir(python: str) -> Path:
    if _same_python(python):
        import sysconfig

        return Path(sysconfig.get_path("scripts"))
    proc = _run([python, "-c", "import sysconfig; print(sysconfig.get_path('scripts'))"])
    return Path(proc.stdout.strip())


def _launcher_names(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        name = next((n for n in archive.namelist() if n.endswith("entry_points.txt")), None)
        text = archive.read(name).decode("utf-8") if name else ""
    names: list[str] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == "[console_scripts]" and "=" in line:
            names.append(line.split("=", 1)[0].strip())
    return names


def _move_launchers(python: str, wheel: Path) -> dict[Path, Path]:
    """Windows: rename the ``.exe`` launchers out of pip's way (a running one cannot be
    overwritten or deleted, but it can be renamed). Returns {original: moved}."""
    if sys.platform != "win32":
        return {}
    scripts = _scripts_dir(python)
    moved: dict[Path, Path] = {}
    stamp = f"{os.getpid()}-{int(time.time())}"
    for name in _launcher_names(wheel):
        exe = scripts / f"{name}.exe"
        if exe.exists():
            target = scripts / f"{name}.exe.{stamp}.old"
            try:
                exe.rename(target)
            except OSError:
                continue
            moved[exe] = target
    return moved


def _restore_launchers(moved: dict[Path, Path]) -> None:
    for exe, old in moved.items():
        # A partial exe pip managed to write before failing must not shadow the good
        # launcher that was moved aside — remove it, then restore unconditionally.
        with contextlib.suppress(OSError):
            exe.unlink()
        with contextlib.suppress(OSError):
            old.rename(exe)


def _cleanup_old_launchers(python: str | None = None) -> None:
    """Delete launchers an earlier update moved aside (those still running stay)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError):
        for old in _scripts_dir(python or sys.executable).glob("*.exe.*.old"):
            with contextlib.suppress(OSError):
                old.unlink()


# -- background check ----------------------------------------------------------------------


def check_cached(
    home: Path,
    source: Source,
    *,
    now: float | None = None,
    fetch: Callable[[Source], tuple[str, str]] | None = None,
) -> str | None:
    """The newer version on the branch ("6.0.0.dev41", or "6.0.0.dev40 (abc1234)" for new
    commits of the same version), or None. Asks the network at most once a day (the
    answer is kept in ``home/update-check.json``); errors count as "nothing new"."""
    now = time.time() if now is None else now
    path = home / STATE_FILE
    state: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(path.read_text(encoding="utf-8"))
    # An unreadable timestamp counts as "never fresh": the check refetches and the
    # corrupt state heals on write. float() inside the suppress — a string value used
    # to kill the daemon thread before fetch/write, disabling the check for good.
    checked = float("inf")
    if isinstance(state, dict):
        with contextlib.suppress(TypeError, ValueError):
            checked = float(state.get("checked", 0))
    mine = installed()
    # The answer is about the build that asked: after an update (any way) the day-old
    # answer compared the branch's old head with the new build ("update to d582163"
    # right after installing something newer), so a different build asks again.
    fresh = (
        isinstance(state, dict)
        and state.get("repo") == source.repo
        and state.get("branch") == source.branch
        and state.get("installed", "") == mine.commit
        and 0 <= now - checked < CHECK_EVERY_S
    )
    if not fresh:
        try:
            version, commit = (fetch or remote_head)(source)
        except Exception:
            version, commit = "", ""
        state = {
            "repo": source.repo,
            "branch": source.branch,
            "checked": now,
            "version": version,
            "commit": commit,
            "installed": mine.commit,
        }
        with contextlib.suppress(OSError):
            home.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state), encoding="utf-8")
        _cleanup_old_launchers()
    version = str(state.get("version") or "")
    commit = str(state.get("commit") or "")
    if version and newer(version, mine.version):
        return version
    if (
        version == mine.version
        and commit
        and mine.commit
        and commit != mine.commit
        and mine.branch == source.branch
    ):
        return f"{version} ({commit[:7]})"
    return None


def remote_head(source: Source, timeout: float = 8.0) -> tuple[str, str]:
    """(version, commit) at the tip of the branch, without a full clone: ``git ls-remote``
    for the commit, the raw version file on GitHub, and a blobless one-commit clone for
    every other host (they have no raw-file URL, so the version used to come back as ""
    and the daily check silently reported nothing forever)."""
    commit = ""
    if shutil.which("git"):
        proc = _run(
            ["git", "ls-remote", source.repo, f"refs/heads/{source.branch}"], timeout=timeout
        )
        if proc.returncode == 0 and proc.stdout.strip():
            commit = proc.stdout.split()[0]
    version = ""
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", source.repo)
    if match:
        import httpx

        ref = commit or source.branch
        url = (
            f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/"
            f"{ref}/src/karox6/__init__.py"
        )
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code == 200:
            found = _VERSION_LINE.search(response.text)
            version = found.group(1) if found else ""
    if not version and shutil.which("git"):
        tmp = Path(tempfile.mkdtemp(prefix="karox6-head-"))
        try:
            clone = tmp / "r"
            proc = _run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--filter=blob:none",
                    "--branch",
                    source.branch,
                    source.repo,
                    str(clone),
                ],
                timeout=timeout,
            )
            if proc.returncode == 0:
                head = _git(clone, "rev-parse", "HEAD", check=False).strip()
                if head:
                    commit = head  # ls-remote's sha or the moved tip — the fetched one
                show = _git(clone, "show", "HEAD:src/karox6/__init__.py", check=False)
                found = _VERSION_LINE.search(show)
                version = found.group(1) if found else ""
        finally:
            _remove_tree(tmp)
    return version, commit


def describe(result: Result) -> list[str]:
    """What happened, as lines for the terminal."""
    old = result.old.version + (f" ({result.old.commit[:7]})" if result.old.commit else "")
    new = f"{result.new_version} ({result.new_commit[:7]})"
    if result.status == "current":
        lines = [f"KaroX {old} is up to date."]
    elif result.status == "older":
        lines = [
            f"KaroX {old} is newer than the branch ({new}); nothing to do "
            "(`karox6 update --force` installs the branch's build anyway)."
        ]
    else:
        head = (
            f"updated: {old} -> {new}"
            if result.status == "updated"
            else f"update available: {old} -> {new}"
        )
        lines = [head]
        if result.changes:
            lines.append("changes:")
            lines += [f"  {line}" for line in result.changes]
    # Versions and commit subjects are bytes the remote repo wrote — strip terminal
    # controls before they reach a tty (`karox6 update`, the chat's /update).
    return [_plain_text(line) for line in lines]


# -- helpers -------------------------------------------------------------------------------


_ABORT = threading.Event()
_CHILDREN: set[subprocess.Popen[str]] = set()
_CHILDREN_LOCK = threading.Lock()


def abort() -> None:
    """Stop a running update now: kill its child process trees (git clone and its
    helpers, pip and its builds) and refuse new ones until the next ``run_update``
    re-arms. Without this a quit or Ctrl+C left the worker thread sitting on 600s/900s
    subprocess timeouts and hung the process."""
    from karox6.tools.proc import kill_tree

    _ABORT.set()
    with _CHILDREN_LOCK:
        children = list(_CHILDREN)
    for proc in children:
        kill_tree(proc.pid)  # every writer dies -> the blocked communicate() sees EOF


def _run(argv: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.pop("PYTHONPATH", None)  # the target interpreter's own packages, not a source tree
    env["PYTHONIOENCODING"] = "utf-8"
    if _ABORT.is_set():
        raise UpdateError("the update was stopped")
    from karox6.lifetime import join_karox_job
    from karox6.tools.proc import kill_tree

    try:
        join_karox_job()  # Windows: a KaroX that dies hard takes the child with it
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=sys.platform != "win32",  # killpg() needs a group leader
        )
    except OSError as exc:
        raise UpdateError(f"cannot run {argv[0]}: {exc}") from exc
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc.pid)
            proc.wait()
            raise UpdateError(
                f"`{' '.join(argv[:3])} ...` took longer than {timeout:.0f}s"
            ) from exc
        except KeyboardInterrupt:  # Ctrl+C: the child is in its own session, kill it
            kill_tree(proc.pid)
            proc.wait()
            raise
    finally:
        with _CHILDREN_LOCK:
            _CHILDREN.discard(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _git(cwd: Path, *args: str, what: str = "", check: bool = True, timeout: float = 120) -> str:
    proc = _run(["git", "-C", str(cwd), *args], timeout=timeout)
    if check and proc.returncode != 0:
        raise UpdateError(f"{what or 'git ' + args[0] + ' failed'}:\n{_tail(proc)}")
    return proc.stdout


def _git_ok(cwd: Path, *args: str) -> bool:
    return _run(["git", "-C", str(cwd), *args]).returncode == 0


def _tail(proc: subprocess.CompletedProcess[str], lines: int = 15) -> str:
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()
    # git/pip stderr lands in UpdateError text that is painted raw — no CSI/OSC through.
    return "\n".join(f"  {_plain_text(line)}" for line in text[-lines:]) or "  (no output)"


def _remove_tree(path: Path) -> None:
    def unlock(func: Callable[..., Any], target: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IWRITE)  # git's object files are read-only on Windows
            func(target)

    shutil.rmtree(path, onexc=unlock)


def _same_python(python: str | None) -> bool:
    """``python`` is this interpreter. Paths are compared as given: a venv's python is a
    symlink to the base interpreter on POSIX, so resolving would mix the two up."""
    return python is None or os.path.abspath(python) == os.path.abspath(sys.executable)
