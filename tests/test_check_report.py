"""Check-log parsing: every failure keeps its file, line and test id.

The fixtures below are trimmed copies of real runner output captured on this
machine (pytest, unittest, ruff, mypy, node:test), plus Jest/Vitest, go test and
cargo shapes that follow their documented reporters. The parser must never
invent a failure from a green log and must never lose the location a repair
needs.
"""

from __future__ import annotations

from pathlib import Path

from karox.check_report import (
    MAX_IMPORT_BYTES,
    analyze,
    fingerprints,
    render_failure_map,
    render_targets,
    select_test_targets,
    rerun_targets,
)

PYTEST_LOG = """\
.FFF.F.                                                                  [100%]
================================== FAILURES ===================================
__________________________________ test_math __________________________________

    def test_math():
>       assert 2 + 2 == 5
E       assert (2 + 2) == 5

test_sample.py:9: AssertionError
__________________________________ test_text __________________________________

    def test_text():
>       assert "alpha" == "beta"
E       AssertionError: assert 'alpha' == 'beta'

test_sample.py:13: AssertionError
_________________________________ test_error __________________________________

    def test_error():
>       helper()

test_sample.py:21:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def helper():
>       raise ValueError("boom")
E       ValueError: boom

test_sample.py:17: ValueError
_______________________________ test_params[2] _______________________________

value = 2

    @pytest.mark.parametrize("value", [1, 2, 3])
    def test_params(value):
>       assert value % 2 == 1, f"even value {value}"
E       AssertionError: even value 2

test_sample.py:26: AssertionError
=========================== short test summary info ===========================
FAILED test_sample.py::test_math - assert (2 + 2) == 5
FAILED test_sample.py::test_text - AssertionError: assert 'alpha' == 'beta'
FAILED test_sample.py::test_error - ValueError: boom
FAILED test_sample.py::test_params[2] - AssertionError: even value 2
4 failed, 3 passed in 0.15s
"""

PYTEST_SOURCE_LOG = """\
================================== FAILURES ===================================
__________________________________ test_add ___________________________________

    def test_add():
>       calc.add(2, 2)

tests/test_calc.py:5:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def add(a, b):
>       raise ValueError("boom")
E       ValueError: boom

calc.py:2: ValueError
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_add - ValueError: boom
1 failed in 0.10s
"""

UNITTEST_LOG = """\
test_fails (test_unittest_sample.Sample.test_fails) ... FAIL
test_ok (test_unittest_sample.Sample.test_ok) ... ok
test_raises (test_unittest_sample.Sample.test_raises) ... ERROR

======================================================================
ERROR: test_raises (test_unittest_sample.Sample.test_raises)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\\\\work\\\\test_unittest_sample.py", line 12, in test_raises
    int("not a number")
ValueError: invalid literal for int() with base 10: 'not a number'

======================================================================
FAIL: test_fails (test_unittest_sample.Sample.test_fails)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\\\\work\\\\test_unittest_sample.py", line 9, in test_fails
    self.assertEqual(2 + 2, 5, "math is broken")
AssertionError: 4 != 5 : math is broken

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=1, errors=1)
"""

RUFF_LOG = """\
bad_code.py:1:8: F401 [*] `os` imported but unused
bad_code.py:3:5: F841 Local variable `x` is assigned to but never used
Found 2 errors.
[*] 1 fixable with the `--fix` option (1 hidden fix can be enabled with the `--unsafe-fixes` option).
"""

MYPY_LOG = """\
typed_sample.py:2: error: Incompatible return value type (got "int", expected "str")  [return-value]
Found 1 error in 1 file (checked 1 source file)
"""

NODE_LOG = """\
✔ addition works (0.6099ms)
✖ subtraction fails (1.5254ms)
ℹ tests 2
ℹ pass 1
ℹ fail 1

✖ failing tests:

test at sample.test.mjs:8:1
✖ subtraction fails (1.5254ms)
  AssertionError [ERR_ASSERTION]: 3 == 4
      at TestContext.<anonymous> (file:///D:/work/sample.test.mjs:9:10)
"""

JEST_LOG = """\
 FAIL  tests/format.test.js
  suite
    ✕ formats money (12 ms)

  ● suite › formats money

    expect(received).toBe(expected) // Object.is equality

    Expected: "4.00"
    Received: "4"

      12 |   expect(formatMoney(4)).toBe("4.00");
         |                          ^
      13 | });

      at Object.<anonymous> (tests/format.test.js:12:26)
"""

VITEST_LOG = """\
 ❯ tests/cart.test.ts (2 tests | 1 failed) 23ms
   × cart > adds tax
     → expected 3 to be 4

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  tests/cart.test.ts > cart > adds tax
AssertionError: expected 3 to be 4
"""

GO_LOG = """\
--- FAIL: TestTotal (0.00s)
    cart_test.go:14: total = 3, want 4
FAIL
exit status 1
FAIL	example.com/shop	0.003s
"""

CARGO_TEST_LOG = """\
running 2 tests
test tests::adds ... ok
test tests::subtracts_wrong ... FAILED

failures:

---- tests::subtracts_wrong stdout ----
thread 'tests::subtracts_wrong' panicked at src/lib.rs:7:9:
assertion `left == right` failed
  left: 3
 right: 4
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
"""

CARGO_BUILD_LOG = """\
error[E0308]: mismatched types
 --> src/main.rs:12:18
   |
12 |     let total: i64 = 3.5;
   |                        ^ expected `i64`, found floating-point number
"""

TSC_LOG = """\
src/app.ts(12,7): error TS2322: Type 'string' is not assignable to type 'number'.
"""


def _failure(report_payload: dict[str, object], index: int = 0) -> dict[str, object]:
    failures = report_payload["failures"]
    assert isinstance(failures, list)
    row = failures[index]
    assert isinstance(row, dict)
    return row


def test_pytest_keeps_every_failure_with_its_deepest_frame() -> None:
    payload = analyze(PYTEST_LOG)

    assert payload["total"] == 4
    assert payload["runners"] == ["pytest"]
    paths = [_failure(payload, index)["test_id"] for index in range(4)]
    assert paths == [
        "test_sample.py::test_math",
        "test_sample.py::test_text",
        "test_sample.py::test_error",
        "test_sample.py::test_params[2]",
    ]
    assert [_failure(payload, index)["line"] for index in range(4)] == [9, 13, 17, 26]
    assert _failure(payload, 2)["message"] == "ValueError: boom"
    assert _failure(payload, 3)["message"] == "AssertionError: even value 2"


def test_pytest_names_the_frame_that_raised_not_the_test_file() -> None:
    """The deepest frame may be in the module under test, not in the test."""

    payload = analyze(PYTEST_SOURCE_LOG)

    row = _failure(payload)
    assert row["path"] == "calc.py"
    assert row["line"] == 2
    assert row["test_id"] == "tests/test_calc.py::test_add"
    assert row["message"] == "ValueError: boom"


def test_pytest_without_a_short_summary_keeps_the_same_frame() -> None:
    log = PYTEST_SOURCE_LOG.split(
        "=========================== short test summary info"
    )[0]

    row = _failure(analyze(log))

    assert (row["path"], row["line"]) == ("calc.py", 2)


def test_test_targets_rerun_the_test_not_the_raising_source() -> None:
    payload = analyze(PYTEST_SOURCE_LOG)

    assert rerun_targets(payload) == ["tests/test_calc.py::test_add"]


def test_pytest_parametrised_node_id_with_spaces_is_not_cut() -> None:
    payload = analyze(
        "FAILED tests/test_cart.py::test_total[a - b] - AssertionError: 3 != 4\n"
        "1 failed in 0.10s\n"
    )

    row = _failure(payload)
    assert row["test_id"] == "tests/test_cart.py::test_total[a - b]"
    assert row["message"] == "AssertionError: 3 != 4"


def test_repeated_failure_is_counted_not_listed_twice() -> None:
    payload = analyze(
        "FAILED tests/test_a.py::test_one - AssertionError: 1 != 2\n"
        "FAILED tests/test_a.py::test_one - AssertionError: 1 != 2\n"
    )

    assert payload["total"] == 1
    assert payload["repeats"] == 1
    assert _failure(payload)["count"] == 2


def test_unittest_names_the_deepest_frame_and_the_assertion() -> None:
    payload = analyze(UNITTEST_LOG, root="D:/work")

    assert payload["total"] == 2
    first = _failure(payload)
    assert first["runner"] == "unittest"
    assert first["path"] == "test_unittest_sample.py"
    assert first["line"] == 12
    assert first["message"].startswith("ValueError: invalid literal")
    second = _failure(payload, 1)
    assert second["line"] == 9
    assert second["message"] == "AssertionError: 4 != 5 : math is broken"


def test_ruff_keeps_code_and_position_without_the_fix_marker() -> None:
    payload = analyze(RUFF_LOG)

    assert payload["total"] == 2
    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["column"], row["code"]) == (
        "ruff",
        "bad_code.py",
        1,
        8,
        "F401",
    )
    assert row["message"] == "`os` imported but unused"


def test_mypy_keeps_the_error_code() -> None:
    payload = analyze(MYPY_LOG)

    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["code"]) == (
        "mypy",
        "typed_sample.py",
        2,
        "return-value",
    )


def test_tsc_reads_path_line_column_and_code() -> None:
    payload = analyze(TSC_LOG)

    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["column"], row["code"]) == (
        "tsc",
        "src/app.ts",
        12,
        7,
        "TS2322",
    )


def test_node_test_uses_the_failing_test_block_not_the_summary() -> None:
    payload = analyze(NODE_LOG)

    assert payload["total"] == 1
    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["column"]) == (
        "node-test",
        "sample.test.mjs",
        8,
        1,
    )
    assert row["test_id"] == "subtraction fails"
    assert row["message"] == "AssertionError [ERR_ASSERTION]: 3 == 4"


def test_jest_bullet_names_the_test_and_the_stack_line() -> None:
    payload = analyze(JEST_LOG)

    assert payload["total"] == 1
    row = _failure(payload)
    assert row["runner"] == "jest"
    assert row["path"] == "tests/format.test.js"
    assert row["line"] == 12
    assert row["test_id"] == "suite › formats money"
    assert row["message"].startswith("expect(received).toBe(expected)")


def test_vitest_reporter_and_bullet_are_one_failure() -> None:
    payload = analyze(VITEST_LOG)

    assert payload["total"] == 1
    row = _failure(payload)
    assert row["runner"] == "vitest"
    assert row["path"] == "tests/cart.test.ts"
    assert row["test_id"] == "cart > adds tax"
    assert "expected 3 to be 4" in str(row["message"])


def test_go_test_reads_the_failing_line() -> None:
    payload = analyze(GO_LOG)

    assert payload["total"] == 1
    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["test_id"]) == (
        "go-test",
        "cart_test.go",
        14,
        "TestTotal",
    )
    assert row["message"] == "total = 3, want 4"


def test_cargo_test_reads_the_panic_location() -> None:
    payload = analyze(CARGO_TEST_LOG)

    assert payload["total"] == 1
    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["column"]) == (
        "cargo-test",
        "src/lib.rs",
        7,
        9,
    )
    assert row["message"] == "assertion `left == right` failed"


def test_cargo_compile_error_is_a_failure_before_any_test_runs() -> None:
    payload = analyze(CARGO_BUILD_LOG)

    row = _failure(payload)
    assert (row["runner"], row["path"], row["line"], row["code"]) == (
        "cargo-build",
        "src/main.rs",
        12,
        "E0308",
    )


def test_cargo_failure_summary_is_not_a_second_build_failure() -> None:
    """`error: test failed` is the run summary, not another compile error."""

    payload = analyze(
        CARGO_TEST_LOG + "\nerror: test failed, to rerun pass `--lib`\n"
    )

    assert payload["total"] == 1
    assert payload["failures"][0]["runner"] == "cargo-test"


def test_go_panic_inside_a_failed_test_is_not_counted_twice() -> None:
    payload = analyze(
        "--- FAIL: TestBoom (0.00s)\n"
        "    x_test.go:12: panic: boom\n"
        "panic: boom\n"
    )

    assert payload["total"] == 1
    assert payload["failures"][0]["message"] == "panic: boom"


def test_a_green_log_produces_no_map() -> None:
    payload = analyze("4 passed in 0.12s\n", "")

    assert payload["total"] == 0
    assert payload["failures"] == []
    assert render_failure_map(payload) == ""


def test_rendered_map_stays_compact_and_names_hidden_failures() -> None:
    log = "\n".join(
        f"FAILED tests/test_many.py::test_{index} - AssertionError: {index}"
        for index in range(12)
    )
    payload = analyze(log)

    rendered = render_failure_map(payload)
    assert payload["total"] == 12
    assert "tests/test_many.py:test_0" not in rendered  # no invented line
    assert "1. tests/test_many.py · test_0 — AssertionError: 0" in rendered
    assert "… 4 more not listed" in rendered
    assert len(rendered) < 1_600


def test_fingerprints_change_when_the_failure_changes() -> None:
    first = analyze("FAILED tests/test_a.py::test_one - AssertionError: 1 != 2\n")
    same = analyze("FAILED tests/test_a.py::test_one - AssertionError: 1 != 2\n")
    moved = analyze("FAILED tests/test_a.py::test_one - AssertionError: 2 != 2\n")

    assert fingerprints(first) == fingerprints(same)
    assert fingerprints(first) != fingerprints(moved)


def test_fingerprints_see_failures_beyond_the_rendered_cap() -> None:
    """A difference in the ninth failure is progress, not an identical set."""

    log = "\n".join(
        f"FAILED tests/test_many.py::test_{index} - AssertionError: {index}"
        for index in range(9)
    )
    same = analyze(log)
    changed = analyze(
        log.replace("test_8 - AssertionError: 8", "test_8 - AssertionError: 9")
    )

    assert len(same["failures"]) == 8
    assert fingerprints(same) != fingerprints(changed)


def test_absolute_paths_become_repository_relative() -> None:
    payload = analyze(UNITTEST_LOG, root="D:/work")

    assert all(
        not str(_failure(payload, index)["path"]).startswith("D:")
        for index in range(int(payload["total"]))
    )


def _fixture_repository(root: Path) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "pkg" / "beta.py").write_text("VALUE = 2\n", encoding="utf-8")
    tests = root / "tests"
    (tests / "deep").mkdir(parents=True)
    (tests / "test_alpha.py").write_text("import pkg.alpha\n", encoding="utf-8")
    (tests / "deep" / "test_feature.py").write_text(
        "from pkg import beta\n", encoding="utf-8"
    )
    (tests / "test_unrelated.py").write_text("import json\n", encoding="utf-8")


def test_impacted_targets_use_names_and_imports(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)

    selected = select_test_targets(
        tmp_path, ["src/pkg/alpha.py", "src/pkg/beta.py"], limit=8
    )

    assert selected == ["tests/deep/test_feature.py", "tests/test_alpha.py"]


def test_impacted_targets_ignore_non_python_and_missing_tests(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)

    assert select_test_targets(tmp_path, ["README.md", "src/pkg/alpha.txt"]) == []
    assert select_test_targets(tmp_path / "nowhere", ["src/pkg/alpha.py"]) == []


def test_impacted_targets_are_bounded(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)

    selected = select_test_targets(
        tmp_path, ["src/pkg/alpha.py", "src/pkg/beta.py"], limit=1
    )

    assert len(selected) == 1


def test_import_scan_skips_an_oversized_test_file(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    huge = tmp_path / "tests" / "test_huge.py"
    huge.write_text(
        "from pkg.beta import VALUE\n" + "#" * (MAX_IMPORT_BYTES + 1),
        encoding="utf-8",
    )

    selected = select_test_targets(tmp_path, ["src/pkg/beta.py"])

    assert "tests/test_huge.py" not in selected


def test_render_targets_is_copy_ready() -> None:
    hint = render_targets(["tests/test_alpha.py"])

    assert hint.startswith("Targeted rerun: tests.run suite=focused targets=[")
    assert '"tests/test_alpha.py"' in hint
    assert render_targets([]) == ""


def test_crlf_pytest_log_keeps_the_section_lines() -> None:
    """Windows runners print CRLF; a line-anchored parser must still match."""

    payload = analyze(PYTEST_LOG.replace("\n", "\r\n"))

    assert payload["total"] == 4
    assert [_failure(payload, index)["line"] for index in range(4)] == [9, 13, 17, 26]


def test_checks_body_puts_the_map_before_the_log() -> None:
    from karox.engine_tool_env import _checks_body

    payload = analyze(PYTEST_LOG)
    body = _checks_body({"stdout": PYTEST_LOG, "stderr": "", "failure_map": payload})

    assert body.index("KaroX failure map") < body.index("FAILURES")
    assert "test_sample.py:9" in body


def test_checks_body_shows_a_stderr_only_report() -> None:
    """Jest and cargo print their failures to stderr; stdout alone looks green."""

    from karox.engine_tool_env import _checks_body

    body = _checks_body(
        {
            "stdout": "",
            "stderr": "FAIL tests/cart.test.ts > cart > adds tax\n",
            "failure_map": analyze(
                "", "FAIL tests/cart.test.ts > cart > adds tax\nAssertionError: 3 != 4\n"
            ),
        }
    )

    assert "KaroX failure map" in body
    assert "tests/cart.test.ts" in body
    assert "adds tax" in body
    assert "FAIL tests/cart.test.ts" in body