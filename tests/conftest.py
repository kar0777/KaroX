import pytest

from karox6.tools.base import ToolContext
from karox6.tools.files import FileTracker
from karox6.tools.jobs import JobManager
from karox6.tools.shell import detect_shell


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(
        workspace=ws,
        session_dir=tmp_path / "session",
        files=FileTracker(ws),
        jobs=JobManager(),
        shell=detect_shell(),
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """No test reads the user's real ~/.karox6 (config, saved API key)."""
    home = tmp_path_factory.mktemp("karox_home")
    monkeypatch.setenv("KAROX_HOME", str(home))
    monkeypatch.setenv("KAROX_MODEL_CATALOG", "0")  # no test downloads models.dev
