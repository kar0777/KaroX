from __future__ import annotations

import subprocess
import sys

import pytest

from karox6 import __version__
from karox6.cli import main
from karox6.sysinfo import rss_mb


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"karox {__version__}"


def test_module_entry_point_runs() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "karox6", "--version"], capture_output=True, text=True, check=True
    )
    assert __version__ in out.stdout


def test_debug_rss_reports_positive_number(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["debug", "rss"]) == 0
    assert float(capsys.readouterr().out.strip()) > 0


def test_rss_mb_is_sane() -> None:
    assert 1 < rss_mb() < 4096
