"""What the model reads of a long pytest run.

A verbose run of 300 passing and 3 failing tests was 28 KB; the head-and-tail
cut kept PASSED lines and dropped every assertion diff, which sat in the
middle. The condensed view keeps each failure's failing line, its ``E`` lines
and its locator, and drops progress and source context.
"""

from __future__ import annotations

from karox.test_output import condense_test_output

FAILURE_BLOCK = """\
________________________ test_dict_diff _________________________

    def test_dict_diff():
        data = {"name": "alpha", "nested": {"a": 1, "b": 2}}
        setup_one = 1
        setup_two = 2
        result = {"name": "alpha", "nested": {"a": 1, "b": 3}}
>       assert result == data
E       AssertionError: assert {'name': 'alpha', 'nested': {'a': 1, 'b': 3}} == {'name': 'alpha', 'nested': {'a': 1, 'b': 2}}
E         Differing items:
E         {'nested': {'a': 1, 'b': 3}} != {'nested': {'a': 1, 'b': 2}}

test_demo.py:9: AssertionError
----------------------------- Captured stdout call -----------------------------
debug value 42
"""


def _verbose_log(passing: int = 300) -> str:
    lines = ["============================= test session starts =============================",
             "collected 301 items", ""]
    lines += [f"test_demo.py::test_many[{i}] PASSED [ {i // 3:2d}%]" for i in range(passing)]
    lines += ["test_demo.py::test_dict_diff FAILED [100%]", "",
              "=================================== FAILURES ===================================",
              FAILURE_BLOCK,
              "=========================== short test summary info ============================",
              "FAILED test_demo.py::test_dict_diff - AssertionError: assert {'name': ...",
              f"======================== 1 failed, {passing} passed in 0.37s ========================"]
    return "\n".join(lines)


def test_a_verbose_run_keeps_the_assertion_diff_and_drops_passed_lines() -> None:
    log = _verbose_log()
    condensed = condense_test_output(log, max_chars=6_500)
    assert condensed is not None
    text = str(condensed["text"])
    assert "PASSED" not in text
    assert ">       assert result == data" in text
    assert "{'nested': {'a': 1, 'b': 3}} != {'nested': {'a': 1, 'b': 2}}" in text
    assert "test_demo.py:9: AssertionError" in text
    assert "debug value 42" in text
    assert "1 failed, 300 passed" in text
    # Source context of the test body is dropped; the two lines above the
    # failing line stay.
    assert "setup_one = 1" not in text
    assert "result = {" in text
    assert len(text) < 2_000


def test_e_lines_are_capped_per_failure_block() -> None:
    block = FAILURE_BLOCK.replace(
        "E         Differing items:\n", "".join(f"E         line {i}\n" for i in range(100))
    )
    log = _verbose_log().replace(FAILURE_BLOCK, block)
    text = str(condense_test_output(log, max_chars=20_000)["text"])  # type: ignore[index]
    assert "E         line 0" in text
    assert "E         line 99" not in text
    assert "more E lines]" in text


def test_same_named_tests_in_two_classes_keep_their_own_failure_blocks() -> None:
    from karox.check_parsers import _pytest

    log = "\n".join([
        "=================================== FAILURES ===================================",
        "____________________________ TestAlpha.test_same _____________________________",
        "",
        ">       assert 1 == 2",
        "E       assert 1 == 2",
        "",
        "t.py:3: AssertionError",
        "____________________________ TestBeta.test_same ______________________________",
        "",
        ">       assert 'b' == 'c'",
        "E       AssertionError: beta-specific",
        "",
        "t.py:7: AssertionError",
        "=========================== short test summary info ============================",
        "FAILED t.py::TestAlpha::test_same - assert 1 == 2",
        "FAILED t.py::TestBeta::test_same - AssertionError: beta-specific",
    ])
    lines = {item["test_id"]: item["line"] for item in _pytest(log)}
    assert lines == {"t.py::TestAlpha::test_same": 3, "t.py::TestBeta::test_same": 7}
