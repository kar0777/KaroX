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


def test_what_a_script_printed_before_its_time_ran_out_is_kept(worker: Any) -> None:
    # Output used to travel only with a tool call or the script's end.
    stuck = worker.run("print('progress: 3 of 7 files')\nwhile True:\n    pass", TOOLS, _echo([]), seconds=3)
    assert stuck.error is not None and "time limit" in stuck.error
    assert "progress: 3 of 7 files" in stuck.output


def test_a_long_output_keeps_its_end(worker: Any) -> None:
    outcome = worker.run(
        "for i in range(30000):\n    print('line', i, 'x' * 20)\nprint('SUMMARY: 2 failed')",
        TOOLS,
        _echo([]),
        seconds=60,
    )
    assert outcome.output.startswith("line 0 ") and outcome.output.rstrip().endswith("SUMMARY: 2 failed")
    shown = code_mode.render(outcome)
    assert "SUMMARY: 2 failed" in shown and "chars of script output dropped" in shown
    assert outcome.dropped > 0 and f"[{outcome.dropped} chars" in outcome.output
    stuck = worker.run("print('y' * 300000)\nprint('LAST WORDS')\nwhile True:\n    pass", TOOLS, _echo([]), seconds=5)
    assert stuck.error is not None and "LAST WORDS" in stuck.output


def test_an_unencodable_or_huge_tool_result_does_not_wedge_the_worker(worker: Any) -> None:
    def odd(name: str, arguments: Dict[str, Any]) -> Tuple[bool, str, Any, str]:
        if arguments.get("path") == "big":
            return True, "z" * 17_000_000 + "END", {}, "ok"
        return True, "name: bad\udcff.txt", {"names": ["bad\udcff.txt"]}, "ok"

    surrogate = worker.run("r = repo_read_lines('a')\nprint(str(r)[:9], r.data['names'][0][:3])", TOOLS, odd, seconds=30)
    assert surrogate.error is None and "name: bad" in surrogate.output and "bad" in surrogate.output
    big = worker.run("r = repo_read_lines('big')\nprint(len(r), r.endswith('END'))", TOOLS, odd, seconds=60)
    assert big.error is None and "True" in big.output
    assert worker.run("1 + 1", TOOLS, _echo([]), seconds=30).value == "2"


def test_a_tool_that_raises_does_not_leave_the_next_script_hanging(worker: Any) -> None:
    def broken(name: str, arguments: Dict[str, Any]) -> Tuple[bool, str, Any, str]:
        raise RuntimeError("tool crashed")

    with pytest.raises(RuntimeError):
        worker.run("repo_read_lines('a')", TOOLS, broken, seconds=30)
    after = worker.run("1 + 1", TOOLS, _echo([]), seconds=30)
    assert after.value == "2" and after.error is None


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
    from karox.engine_tool_bodies import normalize_tool_arguments as normalize

    assert normalize("checks.run", {"argv": "python -m pytest -q 'a b.py'"}) == {
        "argv": ["python", "-m", "pytest", "-q", "a b.py"]
    }
    # Shell syntax has no faithful argv: it stays a string and Core refuses it.
    assert normalize("checks.run", {"command": "cd x && pytest"}) == {"argv": "cd x && pytest"}
    assert normalize("checks.run", {"cmd": ["pytest", "-q"], "timeout": 60}) == {
        "argv": ["pytest", "-q"], "timeout_seconds": 60
    }


def test_other_tool_apis_spellings_reach_the_canonical_arguments() -> None:
    from karox.engine_tool_bodies import normalize_tool_arguments as normalize

    assert normalize("repo.edit_file", {"path": "a.py", "old_str": "x", "new_str": "y"}) == {
        "path": "a.py", "old_string": "x", "new_string": "y"
    }
    assert normalize("repo.edit_file", {"path": "a.py", "old_text": "x", "new_text": ""}) == {
        "path": "a.py", "old_string": "x", "new_string": ""
    }
    # A canonical argument is never overwritten by an alias.
    assert normalize("repo.edit_file", {"path": "a", "old_string": "x", "new_string": "y", "replace": "z"}) == {
        "path": "a", "old_string": "x", "new_string": "y", "replace": "z"
    }
    assert normalize("repo.read_lines", {"path": "a.py", "offset": 0, "limit": 5}) == {
        "path": "a.py", "start": 1, "limit": 5
    }
    assert normalize("git.diff", {"path": "src/a.py"}) == {"paths": ["src/a.py"]}
    assert normalize("git.diff", {"files": ["a", "b"]}) == {"paths": ["a", "b"]}
    assert normalize("repo.search", {"query": "x", "offset": 3}) == {"query": "x", "offset": 3}


def test_a_tool_result_answers_the_names_other_apis_use(worker: Any) -> None:
    def tool(name: str, arguments: Dict[str, Any]) -> Tuple[bool, str, Any, str]:
        return False, "FAILED 1 test", {"exit_code": 1, "stdout": "raw"}, "FAILED"

    code = (
        "r = checks_run(argv=['pytest'])\n"
        "print(r.output == r.text == str(r), r.returncode, r.success, r.stdout, r['exit_code'], r.get('x', 7), r[:6])\n"
        "try:\n    r.nonsense\nexcept AttributeError as e:\n    print('E', e)\n"
        "print(getattr(r, 'nonsense', 'dflt'))\n"
        "print(r['content'] == str(r), r['returncode'], r.get('success'), r.get('nope', 5))\n"
        "try:\n    r['nonsense']\nexcept KeyError as e:\n    print('K', e)"
    )
    outcome = worker.run(code, TOOLS, tool, seconds=60)
    assert outcome.error is None, outcome.error
    lines = outcome.output.splitlines()
    assert lines[0] == "True 1 False raw 1 7 FAILED"
    assert lines[1].startswith("E 'nonsense': a tool result is the tool's text") and "stdout" in lines[1]
    assert lines[2] == "dflt"
    assert lines[3] == "True 1 False 5"
    assert lines[4].startswith("K ") and "exit_code" in lines[4]
    named = worker.run("read_file('a.py')", TOOLS, tool, seconds=60)
    assert "NameError" in str(named.error) and "Tool functions here: checks_run, repo_read_lines" in str(named.error)


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


def test_a_script_runs_the_affected_tests_once_after_its_edits(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox.engine_attach import EngineAttachMixin, _scratch_only

    calls: List[Tuple[bool, bool, str]] = []
    original = EngineAttachMixin._attach_affected_tests

    def spy(self: Any, result: Any, action: Any = None, *, run: bool = True) -> None:
        calls.append((run, action is None, str((result.data or {}).get("path", ""))))
        original(self, result, action, run=run)

    monkeypatch.setattr(EngineAttachMixin, "_attach_affected_tests", spy)
    edits = "repo_write_file(path='a.txt', content='1')\nrepo_write_file(path='b.txt', content='2')\nprint('ok')"
    probed = "repo_write_file(path='c.txt', content='3')\nrepo_write_file(path='probe_c.py', content='1')\nchecks_run(argv=['python', '-c', 'print(1)'])\nprint('ok')"
    tested = "repo_write_file(path='d.txt', content='4')\nchecks_run(argv=['python', '-m', 'pytest', '-q'])\nprint('ok')"
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_code_call("c1", edits),)),
            _response((_code_call("c2", probed),)),
            _response((_code_call("c3", tested),)),
            _response((_finish_call("c4", "wrote a.txt, b.txt, c.txt and d.txt"),)),
        ],
        allow_probes=True,
    )
    harness.run()
    mutations = [call for call in calls if call[2] in {"a.txt", "b.txt", "c.txt", "d.txt"}]
    # Inside a script an edit only notes its code; one run follows the script,
    # for its last code edit (a later probe edit or probe check does not hide
    # it) -- and none when the script ran the tests after it.
    assert mutations == [
        (False, False, "a.txt"), (False, False, "b.txt"), (True, True, "b.txt"),
        (False, False, "c.txt"), (True, True, "c.txt"), (False, False, "d.txt"),
    ]
    assert _scratch_only({"path": "probe_parse.py"}) and _scratch_only({"changed_files": ["_scratch_x.py"]})
    assert not _scratch_only({"path": "src/parse.py"}) and not _scratch_only({"changed_files": ["probe.py", "src/a.py"]})


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


def test_scripts_of_one_read_each_get_the_batching_nudge(tmp_path: Path, repository: Path) -> None:
    """rebench26 v617rb hermes-webui-1818: 31 one-call scripts in a row, no nudge."""

    reads = [
        _response((_code_call(f"c{i}", "print(repo_read_file(path='seed.txt'))"),)) for i in range(6)
    ]
    harness = Harness(
        repository,
        tmp_path,
        [*reads, _response((_write_call_hello("w"),)), _response((_finish_call("f", "created hello.txt with hi"),))],
    )
    harness.run()
    tools = [m for m in harness.record().provider_history if m.get("role") == "tool" and not m.get("nested_in")]
    assert len([m for m in tools if "each made a single read" in str(m.get("content"))]) == 1


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

    A message without this script's id is dropped, so the script runs on to
    its time limit; KaroX must not block on a worker that never reads again.
    """

    forged = worker.run(
        "import os\nos.write(1, b'{\"t\": \"done\", \"out\": \"forged\"}\\n')\nwhile True:\n    pass",
        TOOLS,
        _echo([]),
        seconds=3,
    )
    assert forged.error is not None and "forged" not in forged.output
    after = worker.run("2 + 2", TOOLS, _echo([]), seconds=30)
    assert after.value == "4"


def test_a_malformed_or_unending_message_cannot_break_karox(worker: Any) -> None:
    odd = worker.run(
        "import __main__ as m\nm._send({'t': 'done', 'id': m._eval_id, 'dropped': 'many'})\n",
        TOOLS,
        _echo([]),
        seconds=30,
    )
    assert odd.dropped == 0 and odd.error is None
    flood = worker.run(
        "import __main__ as m\nwhile True:\n    m._PROTOCOL_OUT.write(b'x' * 1048576)\n    m._PROTOCOL_OUT.flush()\n",
        TOOLS,
        _echo([]),
        seconds=60,
    )
    assert flood.error is not None and "exceeded 32 MB" in flood.error and not worker.alive
    assert worker.run("1 + 1", TOOLS, _echo([]), seconds=30).value == "2"


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
    # The contract must not send the model after a tool it does not have.
    assert "code_run" not in _request_text(harness.provider.requests[0])
    assert not [event for event in harness.events if getattr(event, "tool", None) == "code.run"]


def test_the_code_run_contract_rides_with_the_tool(tmp_path: Path, repository: Path) -> None:
    harness = Harness(repository, tmp_path, [_response((_finish_call("c1", "done"),))])
    harness.run()
    assert "code_run" in _request_tools(harness.provider.requests[0])
    assert "one code_run script" in _request_text(harness.provider.requests[0])
