"""`karox6 update` on a temporary git repository and a temporary venv (no network).

The repository holds a tiny stand-in for KaroX (a ``karox6`` package with a ``cli.main``)
and an in-tree build backend, so pip builds its wheel without downloading a build tool.
The venv comes from ``python -m venv`` (pip from ensurepip, offline). Everything the
update does to an installation is done to that venv; the user's own is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from karox6 import update

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BACKEND = '''"""In-tree PEP 517 backend of the test project: a plain zip of src/karox6."""
import base64, hashlib, re, zipfile
from pathlib import Path


def _version():
    text = Path("src/karox6/__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    version = _version()
    name = f"karox6-{version}-py3-none-any.whl"
    info = f"karox6-{version}.dist-info"
    files = {}
    for path in sorted(Path("src").rglob("*.py")):
        files[path.relative_to("src").as_posix()] = path.read_bytes()
    files[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\\nName: karox6\\nVersion: {version}\\n"
    ).encode()
    files[f"{info}/WHEEL"] = (
        b"Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    )
    files[f"{info}/entry_points.txt"] = b"[console_scripts]\\nkarox6 = karox6.cli:main\\n"
    record = []
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as archive:
        for arc, data in files.items():
            archive.writestr(arc, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record.append(f"{arc},sha256={digest.decode()},{len(data)}")
        record.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\\n".join(record) + "\\n")
    return name
'''

CLI = """import sys, time
from karox6 import __version__


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["sleep"]:
        print("sleeping", flush=True)
        time.sleep(float(argv[1]))
        return 0
    print(f"karox {__version__}")
    return 0
"""

PYPROJECT = """[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]

[project]
name = "karox6"
dynamic = ["version"]
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout


def _commit(repo: Path, message: str, *, version: str | None = None, cli: str = CLI) -> str:
    pkg = repo / "src" / "karox6"
    pkg.mkdir(parents=True, exist_ok=True)
    if version is not None:
        (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (pkg / "cli.py").write_text(cli, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A git repository with the stand-in project, and a venv to update."""
    root = tmp_path_factory.mktemp("update")
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "karox6/main")
    (repo / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (repo / "backend.py").write_text(BACKEND, encoding="utf-8")
    _commit(repo, "first", version="1.0.dev1")
    venv = root / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    return repo, str(python)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIP_NO_INDEX", "1")  # pip must never reach for the network here
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.delenv("PIP_REQUIRE_VIRTUALENV", raising=False)


def test_versions_sort_as_pep_440() -> None:
    ordered = ["0", "6.0.0.dev9", "6.0.0.dev34", "6.0.0.dev40", "6.0.0a1", "6.0.0rc2", "6.0.0"]
    ordered += ["6.0.0.post1", "6.0.1", "6.1"]
    keys = [update.version_key(v) for v in ordered]
    assert keys == sorted(keys)
    assert update.newer("6.0.0.dev40", "6.0.0.dev34")
    assert not update.newer("6.0.0.dev34", "6.0.0.dev34")
    assert update.version_key("6.0") == update.version_key("6.0.0")


def test_update_installs_lists_changes_and_keeps_a_working_install_on_failure(
    project: tuple[Path, str], tmp_path: Path
) -> None:
    repo, python = project
    source = update.Source(repo=str(repo), branch="karox6/main")
    lines: list[str] = []

    # 1. Nothing installed yet: the first update builds and installs 1.0.dev1.
    first = update.run_update(source, lines.append, python=python, workdir=tmp_path)
    assert first.status == "updated"
    now = update.installed(python, ask=True)
    assert now.version == "1.0.dev1"
    assert now.commit == first.new_commit
    assert now.branch == "karox6/main"
    assert any(line.startswith("building KaroX 1.0.dev1") for line in lines)

    # 2. Again: up to date, nothing is built.
    lines.clear()
    again = update.run_update(source, lines.append, python=python, workdir=tmp_path)
    assert again.status == "current"
    assert not any("building" in line for line in lines)
    assert update.describe(again) == [f"KaroX 1.0.dev1 ({first.new_commit[:7]}) is up to date."]

    # 3. Two new commits and a new version: listed, newest first, then installed.
    _commit(repo, "feat: a thing")
    head = _commit(repo, "chore: 1.0.dev2", version="1.0.dev2")
    check = update.run_update(source, lines.append, python=python, check_only=True)
    assert check.status == "available"
    assert [c.split(" ", 1)[1] for c in check.changes] == ["chore: 1.0.dev2", "feat: a thing"]
    assert update.installed(python, ask=True).version == "1.0.dev1"  # a check changes nothing
    done = update.run_update(source, lines.append, python=python, workdir=tmp_path)
    assert done.status == "updated"
    text = "\n".join(update.describe(done))
    assert f"updated: 1.0.dev1 ({first.new_commit[:7]}) -> 1.0.dev2 ({head[:7]})" in text
    assert "feat: a thing" in text
    assert update.installed(python, ask=True).version == "1.0.dev2"

    # 4. A build that does not start is refused before the installation is touched.
    _commit(repo, "broken", version="1.0.dev3", cli="def main(:\n")
    with pytest.raises(update.UpdateError, match="does not start"):
        update.run_update(source, lines.append, python=python, workdir=tmp_path)
    assert update.installed(python, ask=True).version == "1.0.dev2"
    _commit(repo, "fixed", version="1.0.dev4")

    # 5. A missing branch is a sentence, not a traceback.
    with pytest.raises(update.UpdateError, match="cannot fetch nope"):
        update.run_update(update.Source(str(repo), "nope"), lines.append, python=python)
    assert not list(tmp_path.glob("karox6-update-*"))  # temporary clones are removed


@pytest.mark.skipif(sys.platform != "win32", reason="a running .exe is locked only on Windows")
def test_update_replaces_a_running_launcher_on_windows(
    project: tuple[Path, str], tmp_path: Path
) -> None:
    repo, python = project
    source = update.Source(repo=str(repo), branch="karox6/main")
    update.run_update(source, lambda _l: None, python=python, force=True, workdir=tmp_path)
    exe = Path(python).parent / "karox6.exe"
    assert exe.is_file()
    running = subprocess.Popen(
        [str(exe), "sleep", "60"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True
    )
    try:
        assert running.stdout is not None
        assert running.stdout.readline().strip() == "sleeping"
        with pytest.raises(PermissionError):
            exe.unlink()  # what pip would trip over
        version = f"1.0.dev{int(time.time())}"
        _commit(repo, "while running", version=version)
        result = update.run_update(source, lambda _l: None, python=python, workdir=tmp_path)
        assert result.status == "updated"
        assert update.installed(python, ask=True).version == version
        out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
        assert out.strip() == f"karox {version}"  # the new launcher works
        assert list(exe.parent.glob("karox6.exe.*.old"))  # the running one, moved aside
    finally:
        running.kill()
        running.wait()
    update._cleanup_old_launchers(python)  # a later start removes it
    assert not list(exe.parent.glob("karox6.exe.*.old"))


def test_background_check_asks_once_a_day_and_never_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update, "installed", lambda *a, **k: update.Installed("6.0.0.dev40"))
    asked: list[str] = []

    def fetch(source: update.Source) -> tuple[str, str]:
        asked.append(source.branch)
        return "6.0.0.dev41", "a" * 40

    source = update.Source("https://example.test/r.git", "karox6/main")
    assert update.check_cached(tmp_path, source, now=1000.0, fetch=fetch) == "6.0.0.dev41"
    assert update.check_cached(tmp_path, source, now=2000.0, fetch=fetch) == "6.0.0.dev41"
    assert asked == ["karox6/main"]  # the second answer came from the saved state
    state = json.loads((tmp_path / update.STATE_FILE).read_text(encoding="utf-8"))
    assert state["version"] == "6.0.0.dev41"

    def boom(_source: update.Source) -> tuple[str, str]:
        raise OSError("offline")

    later = 1000.0 + update.CHECK_EVERY_S + 1
    assert update.check_cached(tmp_path, source, now=later, fetch=boom) is None
    older = lambda _s: ("6.0.0.dev39", "b" * 40)  # noqa: E731
    assert update.check_cached(tmp_path, source, now=later * 2, fetch=older) is None


def test_same_version_new_commits_on_the_same_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = update.Installed("6.0.0.dev40", "c" * 40, "karox6/main")
    monkeypatch.setattr(update, "installed", lambda *a, **k: mine)
    source = update.Source("https://example.test/r.git", "karox6/main")
    same = lambda _s: ("6.0.0.dev40", "d" * 40)  # noqa: E731
    found = update.check_cached(tmp_path, source, now=5.0, fetch=same)
    assert found == "6.0.0.dev40 (ddddddd)"


def test_update_command_reports_errors_as_sentences(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from karox6.cli import main

    missing = tmp_path / "no-such-repo"
    code = main(["update", "--check", "--repo", str(missing), "--branch", "karox6/main"])
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("karox: update failed: cannot fetch karox6/main from")
    assert "Traceback" not in err


def test_the_check_stays_off_in_a_source_tree() -> None:
    assert update.from_checkout()  # the tests run KaroX from src/


def test_install_script_is_idempotent(project: tuple[Path, str], tmp_path: Path) -> None:
    """scripts/install_latest.(ps1|sh): a new venv, then "up to date" on the second run."""
    repo, _python = project
    root = Path(__file__).resolve().parents[1] / "scripts"
    venv = tmp_path / "fresh venv"
    if sys.platform == "win32":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            pytest.skip("no PowerShell")
        argv = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        argv += [str(root / "install_latest.ps1"), "-Venv", str(venv), "-Repo", str(repo)]
        argv += ["-Branch", "karox6/main", "-Python", sys.executable]
    else:
        argv = ["sh", str(root / "install_latest.sh"), "--venv", str(venv), "--repo", str(repo)]
        argv += ["--branch", "karox6/main", "--python", sys.executable]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    first = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert "KaroX: none -> " in first.stdout
    assert f"({head[:7]})" in first.stdout
    second = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert f"({head[:7]}) is up to date." in second.stdout
