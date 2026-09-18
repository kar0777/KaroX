from __future__ import annotations

import contextlib
import unittest
from _unittest_compat import enter_context


class ContextCleanupCompatibilityTests(unittest.TestCase):
    def test_context_value_and_lifo_cleanup_match_unittest_contract(self) -> None:
        events: list[str] = []

        @contextlib.contextmanager
        def resource(name: str):
            events.append("enter " + name)
            try:
                yield name
            finally:
                events.append("exit " + name)

        case = unittest.TestCase()
        self.assertEqual(enter_context(case, resource("a")), "a")
        self.assertEqual(enter_context(case, resource("b")), "b")
        self.assertTrue(case.doCleanups())
        self.assertEqual(events, ["enter a", "enter b", "exit b", "exit a"])

    def test_failed_entry_does_not_register_exit(self) -> None:
        events: list[str] = []

        @contextlib.contextmanager
        def broken():
            events.append("entry")
            raise ValueError("entry failed")
            yield

        case = unittest.TestCase()
        with self.assertRaisesRegex(ValueError, "entry failed"):
            enter_context(case, broken())
        self.assertTrue(case.doCleanups())
        self.assertEqual(events, ["entry"])

    def test_cleanup_runs_after_a_test_failure(self) -> None:
        events: list[str] = []

        @contextlib.contextmanager
        def resource():
            try:
                yield
            finally:
                events.append("closed")

        class FailingCase(unittest.TestCase):
            def runTest(self):
                enter_context(self, resource())
                self.fail("intentional sentinel")

        result = unittest.TestResult()
        FailingCase().run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(events, ["closed"])

    def test_cleanup_failure_is_not_swallowed(self) -> None:
        @contextlib.contextmanager
        def resource():
            yield
            raise RuntimeError("cleanup sentinel")

        class CleanupFailureCase(unittest.TestCase):
            def runTest(self):
                enter_context(self, resource())

        result = unittest.TestResult()
        CleanupFailureCase().run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("cleanup sentinel", result.errors[0][1])
