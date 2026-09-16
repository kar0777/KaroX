"""Cross-platform smoke for a freshly built KaroX wheel.

The script creates an isolated virtual environment, installs only the newest
wheel from ``dist/`` (plus its declared dependencies through pip), and exercises
the user-facing entry points that must work on Windows, macOS, and Linux.
Runtime/config directories are redirected into the temporary directory so CI
never reads or mutates a developer's real KaroX state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    env: dict[str, str],
    cwd: Path = ROOT,
    timeout: float = 180.0,
) -> None:
    completed = subprocess.run(
        [str(item) for item in argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        sys.stderr.write(f"command failed ({completed.returncode}): {' '.join(map(str, argv))}\n")
        if completed.stdout:
            sys.stderr.write(completed.stdout[-8000:])
        if completed.stderr:
            sys.stderr.write(completed.stderr[-8000:])
        raise SystemExit(completed.returncode or 1)


def main() -> int:
    wheels = sorted(DIST.glob("karox_runtime-*.whl"), key=lambda path: path.stat().st_mtime_ns)
    if not wheels:
        raise SystemExit("no KaroX wheel found in dist/; run `python -m build --wheel` first")
    wheel = wheels[-1]

    with tempfile.TemporaryDirectory(prefix="karox-installed-wheel-") as temporary:
        root = Path(temporary)
        environment_dir = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_dir)
        scripts_dir = environment_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        karox = scripts_dir / ("karox.exe" if os.name == "nt" else "karox")

        env = os.environ.copy()
        config = root / "config"
        runtime = root / "runtime"
        config.mkdir()
        runtime.mkdir()
        env.update(
            {
                "KAROX_CONFIG_DIR": str(config),
                "KAROX_VNEXT_CONFIG_DIR": str(config),
                "KAROX_RUNTIME_DIR": str(runtime),
                "KAROX_VNEXT_RUNTIME_DIR": str(runtime),
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
            }
        )
        env.pop("PYTHONPATH", None)

        _run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel.resolve()),
            ],
            env=env,
            timeout=300.0,
        )
        _run([python, "-c", "import karox; print(karox.__version__)"], env=env)
        _run([karox, "--version"], env=env)
        _run([karox, "--help"], env=env)
        _run([karox, "quickstart", "--repository", str(ROOT)], env=env)
        _run([karox, "doctor", "--json"], env=env)

    print(f"installed-wheel smoke passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
