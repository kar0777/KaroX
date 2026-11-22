import asyncio

import pytest

from karox6.context.testout import (
    CONDENSE_THRESHOLD_CHARS,
    CONDENSED_HARD_CHARS,
    CONDENSED_TARGET_CHARS,
    condense_test_output,
)
from karox6.tools.shell import ShellTool


def pytest_log(
    *, files=40, per_file=12, failures=(), errors=(), warnings=0, verbose=False, tb_lines=6
):
    out = [
        "============================= test session starts =============================",
        "platform win32 -- Python 3.12.10, pytest-8.3.4, pluggy-1.5.0",
        "rootdir: /work/project",
        "configfile: pyproject.toml",
        "plugins: anyio-4.8.0, cov-6.0.0",
        f"collected {files * per_file} items",
        "",
    ]
    for f in range(files):
        if verbose:
            out += [
                f"tests/test_mod{f}.py::test_case_{k} PASSED{' ' * 20}[ {k * 100 // per_file:>2}%]"
                for k in range(per_file)
            ]
        else:
            out.append(
                f"tests/test_mod{f}.py {'.' * per_file}{' ' * 8}[{(f + 1) * 100 // files:>3}%]"
            )
    if errors:
        out += [
            "",
            "=================================== ERRORS ====================================",
        ]
        for name, msg in errors:
            out += [
                f"_{'_' * 10} ERROR collecting {name} {'_' * 10}",
                "ImportError while importing test module '/work/project/" + name + "'.",
                "Hint: make sure your test modules/packages have valid Python names.",
                "Traceback:",
                f"{name}:3: in <module>",
                "    import missing_dependency",
                f"E   {msg}",
            ]
    if failures:
        out += [
            "",
            "=================================== FAILURES ===================================",
        ]
        for name, loc, msg in failures:
            out += [f"{'_' * 12} {name} {'_' * 12}", ""]
            out += [f"    # setup line {k}" for k in range(tb_lines)]
            out += [
                f"    def {name}():",
                f">       assert compute() == {msg!r}",
                f"E       AssertionError: {msg}",
                "E       assert 1 == 2",
                "",
                f"{loc}: AssertionError",
                "----------------------------- Captured stdout call -----------------------------",
                *[f"debug output {k}" for k in range(40)],
            ]
    if warnings:
        out += [
            "",
            "============================== warnings summary ===============================",
        ]
        for k in range(warnings):
            out += [
                f"tests/test_mod{k}.py::test_case_0",
                f"  /work/project/lib/mod{k}.py:10: DeprecationWarning: thing {k} is deprecated",
                "    warnings.warn('deprecated', DeprecationWarning)",
                "",
            ]
        out.append("-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html")
    if failures or errors:
        out += ["=========================== short test summary info ============================"]
        out += [f"FAILED {loc.split(':')[0]}::{name} - {msg}" for name, loc, msg in failures]
        out += [f"ERROR {name} - {msg}" for name, msg in errors]
    bad = len(failures) + len(errors)
    total = files * per_file
    tail = f"{bad} failed, " if failures else ""
    tail += f"{total - len(failures)} passed" + (f", {warnings} warnings" if warnings else "")
    tail += f", {len(errors)} errors" if errors else ""
    out.append(f"{'=' * 10} {tail} in 12.34s {'=' * 10}")
    return "\n".join(out) + "\n"


def test_short_logs_and_unknown_formats_are_left_alone():
    assert condense_test_output("x" * (CONDENSE_THRESHOLD_CHARS - 1)) is None
    assert condense_test_output("PASSED\n" * 2000) is None
    assert condense_test_output("a line of ordinary build output\n" * 500) is None
    short = pytest_log(files=2, per_file=3)
    assert len(short) < CONDENSE_THRESHOLD_CHARS
    assert condense_test_output(short) is None


def test_pytest_passing_run_keeps_header_and_summary():
    raw = pytest_log(files=120, per_file=20, warnings=0)
    assert len(raw) > CONDENSE_THRESHOLD_CHARS
    result = condense_test_output(raw)
    assert result is not None
    assert len(result.text) < 1_000
    assert "collected 2400 items" in result.text
    assert "2400 passed in 12.34s" in result.text
    assert "tests/test_mod0.py ...." not in result.text
    assert "dropped 120 progress lines" in result.text


def test_pytest_verbose_passed_lines_are_dropped():
    raw = pytest_log(files=40, per_file=10, verbose=True)
    result = condense_test_output(raw)
    assert result is not None
    assert "PASSED" not in result.text
    assert "400 passed" in result.text


def test_pytest_failures_keep_assertion_and_location():
    failures = [
        ("test_alpha", "tests/test_alpha.py:41", "expected 3 got 4"),
        ("test_beta", "tests/test_beta.py:87", "wrong status"),
    ]
    raw = pytest_log(files=150, failures=failures, warnings=12)
    result = condense_test_output(raw)
    assert result is not None
    text = result.text
    assert len(text) <= CONDENSED_TARGET_CHARS
    for name, loc, msg in failures:
        assert f" {name} " in text
        assert f"E       AssertionError: {msg}" in text
        assert f"{loc}: AssertionError" in text
        assert f"FAILED {loc.split(':')[0]}::{name} - {msg}" in text
    assert "2 failed, 1798 passed, 12 warnings in 12.34s" in text
    assert "9 warnings" in text
    assert "DeprecationWarning: thing 0" in text
    assert "DeprecationWarning: thing 11" not in text
    assert "debug output 39" in text
    assert text.count("debug output") <= 2 * 11


def test_pytest_huge_failure_traceback_is_cut_but_assertion_kept():
    failures = [("test_deep", "tests/test_deep.py:9", "deep failure")]
    raw = pytest_log(failures=failures, tb_lines=2_500)
    assert len(raw) > 50_000
    result = condense_test_output(raw)
    assert result is not None
    assert len(result.text) <= CONDENSED_TARGET_CHARS
    assert "E       AssertionError: deep failure" in result.text
    assert "tests/test_deep.py:9: AssertionError" in result.text
    assert ">       assert compute() == 'deep failure'" in result.text


def test_pytest_collection_errors_are_kept():
    errors = [
        ("tests/test_broken.py", "ModuleNotFoundError: No module named 'missing_dependency'"),
        ("tests/test_other.py", "SyntaxError: invalid syntax"),
    ]
    raw = pytest_log(files=150, errors=errors)
    result = condense_test_output(raw)
    assert result is not None
    for name, msg in errors:
        assert f"ERROR collecting {name}" in result.text
        assert f"E   {msg}" in result.text
        assert f"{name}:3: in <module>" in result.text
    assert "2 errors" in result.text


def test_pytest_many_failures_stay_bounded_and_keep_every_name():
    failures = [
        (f"test_case_{k}", f"tests/test_many.py:{k + 10}", "same broken thing") for k in range(300)
    ]
    raw = pytest_log(files=10, per_file=40, failures=failures)
    assert len(raw) > 300_000
    result = condense_test_output(raw)
    assert result is not None
    assert len(result.text) <= CONDENSED_HARD_CHARS
    assert "same broken thing" in result.text
    assert "300 failed" in result.text
    assert "FAILED tests/test_many.py::test_case_0 - same broken thing" in result.text


def test_pytest_exit_marker_survives():
    failures = [("test_a", "tests/test_a.py:3", "boom")]
    raw = pytest_log(files=150, failures=failures) + "[exit code: 1]\n"
    result = condense_test_output(raw)
    assert result is not None
    assert result.text.rstrip().endswith("[exit code: 1]")


def test_pytest_quiet_mode_without_session_header():
    raw = pytest_log(files=150, per_file=15)
    raw = "\n".join(raw.splitlines()[7:-1]) + "\n1 failed, 2249 passed in 3.21s\n"
    result = condense_test_output(raw)
    assert result is not None
    assert "1 failed, 2249 passed in 3.21s" in result.text
    assert len(result.text) < 600


def unittest_log(passing=300, fails=2):
    out = ["." * 70 for _ in range(passing // 70 + 1)]
    out[0] = "." * 20 + "F" + "." * 30 + "E" + "." * 18
    for k in range(fails):
        out += [
            "=" * 70,
            f"{'FAIL' if k % 2 == 0 else 'ERROR'}: test_item_{k} (tests.test_things.ThingTests)",
            "-" * 70,
            "Traceback (most recent call last):",
            f'  File "/work/tests/test_things.py", line 30, in test_item_{k}',
            "    self.assertEqual(compute(), 5)",
            "AssertionError: 4 != 5" if k % 2 == 0 else "KeyError: 'missing'",
            "",
        ]
    out += [
        "-" * 70,
        f"Ran {passing + fails} tests in 1.234s",
        "",
        f"FAILED (failures=1, errors={fails - 1})",
    ]
    return "\n".join(out) + "\n"


def test_unittest_progress_dropped_failures_kept():
    raw = unittest_log(passing=5_000, fails=2)
    assert len(raw) > CONDENSE_THRESHOLD_CHARS
    result = condense_test_output(raw)
    assert result is not None
    text = result.text
    assert "FAIL: test_item_0 (tests.test_things.ThingTests)" in text
    assert "ERROR: test_item_1 (tests.test_things.ThingTests)" in text
    assert "AssertionError: 4 != 5" in text
    assert "KeyError: 'missing'" in text
    assert 'File "/work/tests/test_things.py", line 30, in test_item_0' in text
    assert "Ran 5002 tests in 1.234s" in text
    assert "FAILED (failures=1, errors=1)" in text
    assert "......" not in text
    assert len(text) < len(raw) / 3


def test_unittest_verbose_ok_lines_dropped():
    lines = [f"test_case_{k} (tests.test_x.X) ... ok" for k in range(400)]
    lines += ["-" * 70, "Ran 400 tests in 0.5s", "", "OK"]
    result = condense_test_output("\n".join(lines) + "\n")
    assert result is not None
    assert "Ran 400 tests in 0.5s" in result.text
    assert result.text.rstrip().endswith("OK")
    assert "test_case_5" not in result.text


def jest_log(passing=40, console=True):
    out = [f"PASS src/mod{k}.test.js (1.{k} s)" for k in range(passing)]
    block = [
        "  ● Cart › totals › adds tax",
        "",
        "    expect(received).toBe(expected) // Object.is equality",
        "",
        "    Expected: 108",
        "    Received: 100",
        "",
        "      10 |   it('adds tax', () => {",
        "      11 |     const cart = new Cart();",
        "    > 12 |     expect(cart.total()).toBe(108);",
        "         |                          ^",
        "      13 |   });",
        "",
        "      at Object.<anonymous> (src/cart.test.js:12:26)",
        "",
    ]
    out += ["FAIL src/cart.test.js", *block]
    if console:
        out += [
            "  console.log",
            "    " + "noisy debug output " * 20,
            "",
            "      at log (src/util.js:3:9)",
            "",
        ]
    out += [
        "Summary of all failing tests",
        "FAIL src/cart.test.js",
        *block,
        f"Test Suites: 1 failed, {passing} passed, {passing + 1} total",
        f"Tests:       1 failed, {passing * 5} passed, {passing * 5 + 1} total",
        "Snapshots:   0 total",
        "Time:        12.3 s",
        "Ran all test suites.",
    ]
    return "\n".join(out) + "\n"


def test_jest_failure_kept_once_and_noise_dropped():
    raw = jest_log(passing=120)
    assert len(raw) > CONDENSE_THRESHOLD_CHARS
    result = condense_test_output(raw)
    assert result is not None
    text = result.text
    assert text.count("● Cart › totals › adds tax") == 1
    assert text.count("FAIL src/cart.test.js") == 1
    assert "Expected: 108" in text
    assert "Received: 100" in text
    assert "at Object.<anonymous> (src/cart.test.js:12:26)" in text
    assert "Tests:       1 failed, 600 passed, 601 total" in text
    assert "Test Suites: 1 failed, 120 passed, 121 total" in text
    assert "PASS src/mod3" not in text
    assert "noisy debug output" not in text


def go_log(verbose=True, passing=150):
    out = []
    if verbose:
        for k in range(passing):
            out += [
                f"=== RUN   TestOk{k}",
                f"    ok_test.go:{k}: log line {k}",
                f"--- PASS: TestOk{k} (0.00s)",
            ]
        out += [
            "=== RUN   TestBroken",
            "    broken_test.go:42: expected 2, got 1",
            "--- FAIL: TestBroken (0.00s)",
        ]
    else:
        out += ["--- FAIL: TestBroken (0.00s)", "    broken_test.go:42: expected 2, got 1"]
        out += [f"    filler line {k} " * 3 for k in range(150)]
    out += ["FAIL", "exit status 1", "FAIL\texample.com/pkg\t0.012s"]
    out += [f"ok  \texample.com/pkg{k}\t0.0{k}s" for k in range(12)]
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("verbose", [True, False])
def test_go_test_failures_kept_passes_dropped(verbose):
    raw = go_log(verbose=verbose)
    assert len(raw) > CONDENSE_THRESHOLD_CHARS
    result = condense_test_output(raw)
    assert result is not None
    text = result.text
    assert "--- FAIL: TestBroken (0.00s)" in text
    assert "broken_test.go:42: expected 2, got 1" in text
    assert "FAIL\texample.com/pkg\t0.012s" in text
    assert "exit status 1" in text
    assert "TestOk5" not in text
    assert "log line 5" not in text
    assert "ok  \texample.com/pkg0" in text
    assert text.count("ok  \t") <= 5


def cargo_log(passing=200):
    out = ["   Compiling demo v0.1.0 (/work/demo)", "    Finished test profile in 1.2s"]
    out += [
        "     Running unittests src/lib.rs (target/debug/deps/demo-abc)",
        "",
        f"running {passing + 1} tests",
    ]
    out += [f"test tests::case_{k} ... ok" for k in range(passing)]
    out += [
        "test tests::broken ... FAILED",
        "",
        "failures:",
        "",
        "---- tests::broken stdout ----",
        "thread 'tests::broken' panicked at src/lib.rs:21:9:",
        "assertion `left == right` failed",
        "  left: 1",
        " right: 2",
        "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace",
        "",
        "",
        "failures:",
        "    tests::broken",
        "",
        f"test result: FAILED. {passing} passed; 1 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.01s",
        "",
        "error: test failed, to rerun pass `--lib`",
    ]
    return "\n".join(out) + "\n"


def test_cargo_test_failure_kept_passes_dropped():
    raw = cargo_log()
    assert len(raw) > CONDENSE_THRESHOLD_CHARS
    result = condense_test_output(raw)
    assert result is not None
    text = result.text
    assert "---- tests::broken stdout ----" in text
    assert "panicked at src/lib.rs:21:9:" in text
    assert "assertion `left == right` failed" in text
    assert "  left: 1" in text
    assert " right: 2" in text
    assert "    tests::broken" in text
    assert "test result: FAILED. 200 passed; 1 failed" in text
    assert "error: test failed, to rerun pass `--lib`" in text
    assert "tests::case_5" not in text
    assert "Compiling" not in text
    assert "RUST_BACKTRACE" not in text


def test_condensed_never_longer_than_original():
    # Recognised format but nothing worth dropping: left alone.
    body = [f"FAILED tests/test_x.py::test_{k} - boom {'x' * 40}" for k in range(60)]
    raw = "\n".join(
        ["=" * 20 + " short test summary info " + "=" * 20, *body, "60 failed in 1.00s"]
    )
    result = condense_test_output(raw)
    assert result is None or len(result.text) < len(raw)


def shell(ctx, command):
    return asyncio.run(ShellTool(ctx.shell).run({"command": command}, ctx))


def test_shell_tool_condenses_and_points_at_the_full_log(ctx):
    raw = pytest_log(
        files=150, failures=[("test_gamma", "tests/test_gamma.py:7", "bad value")], warnings=5
    )
    script = ctx.workspace / "fake_pytest.txt"
    script.write_text(raw, encoding="utf-8", newline="")
    result = shell(ctx, "cat fake_pytest.txt; exit 1")
    assert result.meta["condensed"] is True
    assert result.meta["exit_code"] == 1
    assert result.content.rstrip().endswith("[exit code: 1]")
    assert "E       AssertionError: bad value" in result.content
    assert "tests/test_gamma.py:7: AssertionError" in result.content
    assert len(result.content) < len(raw) / 3
    log = result.meta["spill_path"]
    assert log in result.content
    with open(log, encoding="utf-8", newline="") as handle:
        saved = handle.read()
    assert "tests/test_mod3.py ............" in saved
    assert saved.count("debug output") == 40


def test_shell_tool_leaves_other_long_output_as_before(ctx):
    result = shell(ctx, "for i in $(seq 1 3000); do echo line $i of ordinary output; done")
    assert "condensed" not in result.meta
    assert "line 1 of ordinary output" in result.content
    assert "Output truncated" in result.content
