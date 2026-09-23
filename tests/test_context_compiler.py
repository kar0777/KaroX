"""Context Compiler acceptance: quality precedes context reduction.

The adversarial cases here are the contract from the Part 2 mandate: naive
compaction would collapse near-duplicates, hide changed source behind old
reads, or dedupe across unrelated tools. Every one of those must stay in the
request. Only exact duplicates and provably superseded results may be
replaced, and always by a lossless marker that points at retained content.
"""

from __future__ import annotations

import json
import unittest

from _support import SRC  # noqa: F401
from karox.context_compiler import (
    REFERENCE_THRESHOLD_CHARS,
    CompiledContext,
    ContextCompiler,
    ContextItem,
    Verdict,
)


def _tool(
    index: int,
    call_id: str,
    content: str,
    name: str = "repo_read_file",
    arguments: str = '{"path": "a.txt"}',
) -> ContextItem:
    return ContextItem(
        index=index,
        role="tool",
        content=content,
        tool_call_id=call_id,
        tool_name=name,
        tool_arguments=arguments,
    )


def _dialogue(index: int, role: str, content: str) -> ContextItem:
    return ContextItem(index=index, role=role, content=content)


BIG = "x" * (REFERENCE_THRESHOLD_CHARS + 200)


class DialogueTests(unittest.TestCase):
    def test_dialogue_turns_are_always_included(self) -> None:
        compiled = ContextCompiler().compile(
            [
                _dialogue(0, "system", "prompt"),
                _dialogue(1, "user", BIG),
                _dialogue(2, "assistant", BIG),
            ]
        )
        self.assertEqual(
            [d.verdict for d in compiled.decisions],
            [Verdict.INCLUDE, Verdict.INCLUDE, Verdict.INCLUDE],
        )
        self.assertEqual(compiled.decisions[0].reason, "stable_prefix")
        self.assertEqual(compiled.stats.chars_saved, 0)


class DuplicateTests(unittest.TestCase):
    def test_exact_duplicate_is_referenced_losslessly(self) -> None:
        compiled = ContextCompiler().compile(
            [_tool(0, "c1", BIG), _tool(1, "c2", BIG)]
        )
        first, second = compiled.decisions
        self.assertIs(first.verdict, Verdict.INCLUDE)
        self.assertIs(second.verdict, Verdict.REFERENCE)
        self.assertEqual(second.reason, "duplicate_of:c1")
        marker = json.loads(second.replacement or "{}")
        self.assertEqual(marker["karox"], "identical_tool_result_reused")
        self.assertEqual(marker["same_as_tool_call_id"], "c1")
        self.assertEqual(marker["next"], "act_or_change_query")
        self.assertEqual(second.chars_saved, len(BIG) - len(second.replacement or ""))

    def test_near_duplicate_is_never_collapsed(self) -> None:
        almost = BIG[:-1] + "y"
        compiled = ContextCompiler().compile(
            [_tool(0, "c1", BIG), _tool(1, "c2", almost, arguments='{"path": "b.txt"}')]
        )
        self.assertTrue(
            all(d.verdict is Verdict.INCLUDE for d in compiled.decisions)
        )

    def test_small_duplicate_stays_inline(self) -> None:
        small = "tiny result"
        compiled = ContextCompiler().compile(
            [_tool(0, "c1", small), _tool(1, "c2", small)]
        )
        self.assertTrue(
            all(d.verdict is Verdict.INCLUDE for d in compiled.decisions)
        )
        self.assertEqual(
            compiled.decisions[1].reason, "below_reference_threshold"
        )

    def test_duplicate_across_different_tools_still_references(self) -> None:
        compiled = ContextCompiler().compile(
            [
                _tool(0, "c1", BIG, name="repo_read_file"),
                _tool(1, "c2", BIG, name="repo_search", arguments='{"q": "x"}'),
            ]
        )
        self.assertIs(compiled.decisions[1].verdict, Verdict.REFERENCE)

    def test_replacement_never_larger_than_original(self) -> None:
        content = "z" * 20
        compiled = ContextCompiler(reference_threshold_chars=10).compile(
            [_tool(0, "c1", content), _tool(1, "c2", content)]
        )
        self.assertIs(compiled.decisions[1].verdict, Verdict.INCLUDE)
        self.assertEqual(compiled.decisions[1].reason, "replacement_not_smaller")


class StaleTests(unittest.TestCase):
    def test_superseded_read_is_marked_stale(self) -> None:
        before = "a" * (REFERENCE_THRESHOLD_CHARS + 50)
        after = "b" * (REFERENCE_THRESHOLD_CHARS + 50)
        compiled = ContextCompiler().compile(
            [_tool(0, "c1", before), _tool(1, "c2", after)]
        )
        first, second = compiled.decisions
        self.assertIs(first.verdict, Verdict.ELIDE_STALE)
        self.assertEqual(first.reason, "superseded_by:c2")
        marker = json.loads(first.replacement or "{}")
        self.assertEqual(marker["karox"], "superseded_tool_result")
        self.assertEqual(marker["superseded_by_tool_call_id"], "c2")
        self.assertIs(second.verdict, Verdict.INCLUDE)

    def test_latest_content_is_never_marked_stale(self) -> None:
        contents = ["a" * 1200, "b" * 1200, "c" * 1200]
        compiled = ContextCompiler().compile(
            [_tool(i, f"c{i}", contents[i]) for i in range(3)]
        )
        self.assertIs(compiled.decisions[2].verdict, Verdict.INCLUDE)
        self.assertIs(compiled.decisions[0].verdict, Verdict.ELIDE_STALE)
        self.assertIs(compiled.decisions[1].verdict, Verdict.ELIDE_STALE)

    def test_different_arguments_never_supersede_each_other(self) -> None:
        one = "a" * 1200
        two = "b" * 1200
        compiled = ContextCompiler().compile(
            [
                _tool(0, "c1", one, arguments='{"path": "a.txt"}'),
                _tool(1, "c2", two, arguments='{"path": "b.txt"}'),
            ]
        )
        self.assertTrue(
            all(d.verdict is Verdict.INCLUDE for d in compiled.decisions)
        )

    def test_equivalent_json_object_arguments_supersede_large_results(self) -> None:
        """Object key order must not retain an obsolete large read."""
        before = "a" * 24_000
        after = "b" * 24_000
        compiled = ContextCompiler().compile(
            [
                _tool(
                    0,
                    "c1",
                    before,
                    arguments='{"path":"a.txt","start":1,"count":500}',
                ),
                _tool(
                    1,
                    "c2",
                    after,
                    arguments='{"count":500,"path":"a.txt","start":1}',
                ),
            ]
        )
        self.assertIs(compiled.decisions[0].verdict, Verdict.ELIDE_STALE)
        self.assertIs(compiled.decisions[1].verdict, Verdict.INCLUDE)
        self.assertGreater(compiled.stats.chars_saved, 23_000)

    def test_malformed_arguments_remain_distinct(self) -> None:
        compiled = ContextCompiler().compile(
            [
                _tool(0, "c1", "a" * 1200, arguments="{not-json"),
                _tool(1, "c2", "b" * 1200, arguments="{not-json }"),
            ]
        )
        self.assertTrue(
            all(decision.verdict is Verdict.INCLUDE for decision in compiled.decisions)
        )

    def test_nonfinite_arguments_cannot_replace_successful_evidence(self) -> None:
        for first, second in (
            ('{"start":1e400}', '{"start":Infinity}'),
            ('{"value":NaN,"path":"a"}', '{"path":"a","value":NaN}'),
        ):
            with self.subTest(first=first):
                compiled = ContextCompiler().compile([
                    _tool(0, "c1", "read evidence" * 200, arguments=first),
                    _tool(1, "c2", "invalid arguments", arguments=second),
                ])
                self.assertIs(compiled.decisions[0].verdict, Verdict.INCLUDE)

    def test_unknown_tool_identity_is_never_marked_stale(self) -> None:
        one = ContextItem(index=0, role="tool", content="a" * 1200, tool_call_id="c1")
        two = ContextItem(index=1, role="tool", content="b" * 1200, tool_call_id="c2")
        compiled = ContextCompiler().compile([one, two])
        self.assertTrue(
            all(d.verdict is Verdict.INCLUDE for d in compiled.decisions)
        )

    def test_identical_repeat_prefers_reference_over_stale(self) -> None:
        compiled = ContextCompiler().compile(
            [_tool(0, "c1", BIG), _tool(1, "c2", BIG)]
        )
        self.assertIs(compiled.decisions[1].verdict, Verdict.REFERENCE)


class DeterminismAndAccountingTests(unittest.TestCase):
    def _items(self) -> list[ContextItem]:
        return [
            _dialogue(0, "system", "prompt"),
            _dialogue(1, "user", "task"),
            _tool(2, "c1", BIG),
            _tool(3, "c2", BIG),
            _tool(4, "c3", "a" * 1200, arguments='{"path": "c.txt"}'),
            _tool(5, "c4", "b" * 1200, arguments='{"path": "c.txt"}'),
        ]

    def test_identical_inputs_compile_identically(self) -> None:
        one = ContextCompiler().compile(self._items())
        two = ContextCompiler().compile(self._items())
        self.assertEqual(one.decisions, two.decisions)
        self.assertEqual(one.stats, two.stats)

    def test_every_item_gets_exactly_one_decision_in_order(self) -> None:
        compiled = ContextCompiler().compile(self._items())
        self.assertEqual(
            [d.index for d in compiled.decisions], [0, 1, 2, 3, 4, 5]
        )

    def test_stats_account_for_every_char(self) -> None:
        compiled = ContextCompiler().compile(self._items())
        stats = compiled.stats
        self.assertEqual(stats.items_total, 6)
        self.assertEqual(stats.referenced, 1)
        self.assertEqual(stats.elided_stale, 1)
        self.assertEqual(
            stats.chars_in - stats.chars_out,
            sum(d.chars_saved for d in compiled.decisions),
        )
        self.assertEqual(stats.chars_saved, stats.chars_in - stats.chars_out)

    def test_explain_reports_only_non_include_decisions(self) -> None:
        compiled = ContextCompiler().compile(self._items())
        lines = compiled.explain()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("reference" in line for line in lines))
        self.assertTrue(any("elide_stale" in line for line in lines))

    def test_compiled_context_type_is_stable(self) -> None:
        compiled = ContextCompiler().compile([])
        self.assertIsInstance(compiled, CompiledContext)
        self.assertEqual(compiled.stats.items_total, 0)


if __name__ == "__main__":
    unittest.main()
