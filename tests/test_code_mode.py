"""Code mode: a script that calls the tools, in one step.

The worker tests pin the sandbox: a script reaches nothing outside itself but
the tool calls it sends back. The kernel tests pin that those calls are real
ones (policy, persisted evidence, verification) and that a resumed transcript
does not replay them as calls the model never sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from karox import code_mode
from test_engine_kernel import (
    Harness,
    _finish_call,
    _request_text,
    _request_tools,
    _response,
    _cli_repo,
    _tool_call,
)

TOOLS = [{"name": "repo_read_lines", "required": ["path"]}, {"name": "checks_run", "required": ["argv"]}]


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    return _cli_repo(tmp_path)


@pytest.fixture()
def worker() -> Any:
    instance = code_mode.CodeModeWorker()
    yield instance
    instance.close()


def _echo(calls: List[Tuple[str, Dict[str, Any]]]) -> Any:
    def call(name: str, arguments: Dict[str, Any]) -> Tuple[bool, str, Any, str]:
        calls.append((name, arguments))
        return True, f"{name} said hello", {"exit_code": 0}, "ok"

    return call


def test_a_script_calls_tools_and_keeps_its_variables(worker: Any) -> None:
    calls: List[Tuple[str, Dict[str, Any]]] = []
    first = worker.run(
        "text = repo_read_lines('a.py', count=5)\nprint(text.ok, text.exit_code)\nlimit = 41",
        TOOLS,
        _echo(calls),
        seconds=30,
    )
    assert first.ok, first.error
    assert first.output == "True 0\n"
    assert calls == [("repo_read_lines", {"path": "a.py", "count": 5})]
    second = worker.run("limit + 1", TOOLS, _echo(calls), seconds=30)
    assert second.value == "42"
    rendered = code_mode.render(first)
    assert "[1 tool call]" in rendered and "1. repo_read_lines: ok" in rendered


@pytest.mark.parametrize(
    "code",
    [
        "open(__import__('os').path.expanduser('~/.gitconfig')).read()",
        "open('written.txt', 'w').write('x')",
        "import subprocess",
        "import ctypes",
        "import socket",
        "import os; os.system('echo hi')",
        "import os; os.listdir('.')",
        "import gc; gc.get_objects()",
        "import ntpath, os; ntpath.realpath = lambda p: os.__file__; open('/etc/passwd').read()",
        "import os; open(os.path.dirname(os.__file__) + '/../../secret.txt').read()",
        # The hook must not trust anything a script can rebind or subclass.
        "import __main__, os\nroot = os.path.dirname(os.__file__) + '/'\n"
        "__main__._key = lambda path, windows: root\nopen(OUTSIDE).read()",
        "import builtins\nbuiltins.isinstance = lambda *a: False\nbuiltins.any = lambda *a: False\n"
        "print(open(OUTSIDE).read()[:10])",
        "class S(str):\n    def startswith(self, *a):\n        return True\nopen(S(OUTSIDE)).read()",
        "class P:\n    def __fspath__(self):\n        return OUTSIDE\nopen(P()).read()",
        "class S(str):\n    pass\n__import__(S('socket'))",
        "try:\n    open(OUTSIDE)\nexcept PermissionError as e:\n    e.__traceback__.tb_next.tb_frame",
        "import sys; sys._getframe()",
        "def g():\n    yield 1\ng().gi_frame",
    ],
)
def test_a_script_reaches_nothing_but_its_tools(worker: Any, code: str) -> None:
    code = code.replace("OUTSIDE", repr(str(Path(__file__).resolve())))
    outcome = worker.run(code, TOOLS, _echo([]), seconds=30)
    assert outcome.error is not None
    assert any(word in outcome.error for word in ("PermissionError", "ImportError", "not available"))


def test_the_worker_gets_no_credentials(worker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAROX_TEST_SECRET_TOKEN", "sk-should-never-leak")
    outcome = worker.run("import os; sorted(os.environ)", TOOLS, _echo([]), seconds=30)
    assert outcome.ok, outcome.error
    assert "KAROX_TEST_SECRET_TOKEN" not in (outcome.value or "")


def test_standard_library_imports_work(worker: Any) -> None:
    outcome = worker.run(
        "import textwrap, urllib.parse, difflib, zipfile\nurllib.parse.quote('a b')",
        TOOLS,
        _echo([]),
        seconds=30,
    )
    assert outcome.ok, outcome.error
    assert outcome.value == "a%20b"


def test_a_runaway_script_is_stopped_and_the_next_one_says_so(worker: Any) -> None:
    stuck = worker.run("while True:\n    pass", TOOLS, _echo([]), seconds=2)
    assert stuck.error is not None and "time limit" in stuck.error
    after = worker.run("1 + 1", TOOLS, _echo([]), seconds=30)
    assert after.value == "2"
    assert after.restarted is True
    assert "variables from earlier scripts are gone" in code_mode.render(after)


def test_errors_show_the_script_line(worker: Any) -> None:
    outcome = worker.run("a = 1\nb = a / 0", TOOLS, _echo([]), seconds=30)
    assert outcome.error is not None
    assert "line 2: b = a / 0" in outcome.error
    assert "ZeroDivisionError" in outcome.error


def test_too_many_calls_stop_the_script(worker: Any) -> None:
    outcome = worker.run("for _ in range(100):\n    checks_run(['x'])", TOOLS, _echo([]), seconds=60)
    assert outcome.error is not None and "more than" in outcome.error
    assert len(outcome.calls) == code_mode.MAX_CALLS_PER_SCRIPT


def test_a_command_string_is_split_for_checks_run() -> None:
    assert code_mode.normalize_arguments("checks.run", {"argv": "python -m pytest -q 'a b.py'"}) == {
        "argv": ["python", "-m", "pytest", "-q", "a b.py"]
    }


SCRIPT = """
before = repo_read_file('seed.txt')
edit = repo_write_file(path='hello.txt', content='hi')
check = checks_run(argv=['python', '-c', 'print(open("hello.txt").read())'])
print('wrote', edit.ok, '| check exit', check.exit_code)
"""


def _code_call(call_id: str, code: str) -> Any:
    return _tool_call(call_id, "code_run", {"code": code})


def test_a_script_edit_and_check_count_as_real_calls(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_code_call("c1", SCRIPT),), content="one script"),
            _response((_finish_call("c2", "created hello.txt with hi"),)),
        ],
        allow_probes=True,
    )
    report = harness.run()
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"
    assert report.verified is True, report.reason
    assert "code_run" in _request_tools(harness.provider.requests[0])
    second = _request_text(harness.provider.requests[1])
    assert "wrote True | check exit 0" in second
    assert "[3 tool calls]" in second
    nested = [entry for entry in harness.record().provider_history if entry.get("nested_in") == "c1"]
    assert [entry.get("core_name") for entry in nested] == ["repo.read_file", "repo.write_file", "checks.run"]


def test_a_script_cannot_finish_the_run(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_code_call("c1", "finish(answer='done')"),)),
            _response((_write_call_hello("c2"),)),
            _response((_finish_call("c3", "created hello.txt with hi"),)),
        ],
    )
    report = harness.run()
    second = _request_text(harness.provider.requests[1])
    assert "NameError" in second or "cannot be called from a script" in second
    assert report.verified is True, report.reason


def _write_call_hello(call_id: str) -> Any:
    return _tool_call(call_id, "repo_write_file", {"path": "hello.txt", "content": "hi"})


def test_code_mode_can_be_switched_off(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_CODE_MODE", "0")
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call_hello("c1"),)),
            _response((_finish_call("c2", "created hello.txt with hi"),)),
        ],
    )
    harness.run()
    assert "code_run" not in _request_tools(harness.provider.requests[0])


def test_a_resumed_transcript_replays_the_script_not_its_calls(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_code_call("c1", "print(repo_read_file('seed.txt').ok)"),)),
            _response((_write_call_hello("c2"),)),
            _response((_finish_call("c3", "created hello.txt with hi"),)),
        ],
    )
    harness.run()
    messages, _ = harness.kernel.implementation._seed_history("s1", None, harness.record())
    tool_ids = [message.get("tool_call_id") for message in messages or () if message.get("role") == "tool"]
    assert "c1" in tool_ids
    assert not [call_id for call_id in tool_ids if "~" in str(call_id)]


def test_a_forged_protocol_message_cannot_hang_karox(worker: Any) -> None:
    """A script can write a fake "done" straight to fd 1 and keep running.

    KaroX may believe the fake; what it must not do is block on a worker that
    never reads again, whatever the size of the next message.
    """

    worker.run(
        "import os\nos.write(1, b'{\"t\": \"done\", \"out\": \"forged\"}\\n')\nwhile True:\n    pass",
        TOOLS,
        _echo([]),
        seconds=3,
    )
    big = worker.run("x = 1\n" * 60_000, TOOLS, _echo([]), seconds=3)
    assert big.error is not None
    after = worker.run("2 + 2", TOOLS, _echo([]), seconds=30)
    assert after.value == "4"


def test_a_session_that_cannot_write_and_check_gets_no_code_run(tmp_path: Path, repository: Path) -> None:
    from karox.models import Capability

    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_code_call("c1", "print(1)"),)),
            _response((_finish_call("c2", "read only"),)),
        ],
        grants={Capability.REPO_READ, Capability.GIT_READ, Capability.CHECKS_RUN, Capability.PROCESS_RUN},
    )
    harness.run()
    assert "code_run" not in _request_tools(harness.provider.requests[0])
    assert not [event for event in harness.events if getattr(event, "tool", None) == "code.run"]
