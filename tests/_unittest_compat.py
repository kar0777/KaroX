"""Portable context cleanup for the supported Python 3.10+ test matrix.

TestCase.enterContext was added in Python 3.11. Keep its cleanup semantics
without monkey-patching unittest or skipping tests on the minimum Python.
"""
from __future__ import annotations

import unittest
from typing import ContextManager, TypeVar

_T = TypeVar("_T")


def enter_context(case: unittest.TestCase, context: ContextManager[_T]) -> _T:
    """Enter a context and register its exit only after a successful entry."""
    cls = type(context)
    result = cls.__enter__(context)
    case.addCleanup(cls.__exit__, context, None, None, None)
    return result
