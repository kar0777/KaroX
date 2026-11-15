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
