"""Regressions for the release-console ResourceWarning families.

Every KaroX-owned resource -- child-process pipes, the shared transcript
store's SQLite connection -- has an explicit owner that closes it. These tests
funnel ``ResourceWarning`` into a hard failure locally (record + ``gc.collect``
inside a ``catch_warnings`` block), so a reintroduced leak fails the one test
that owns it instead of scrolling past the release log as GC noise attributed
to whatever code happened to be running when the collector fired.
"""

from __future__ import annotations

import contextlib
import gc
import io
import logging
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import web_bridge_launcher
from karox.transcript_shadow import close_transcript_store, get_transcript_store


@contextlib.contextmanager
def _resource_warnings_are_failures():
    """Record ResourceWarnings for the block, including a forced GC pass."""
    # Flush garbage left by earlier tests before recording. Python 3.13+
    # reports ResourceWarning for third-party sqlite connections when they are
    # eventually collected; without this pre-pass, an unrelated object created
    # by a previous test can make this block fail. The post-block collection
    # still turns resources leaked by this block into a deterministic failure.
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught
        gc.collect()


def _leaks(caught) -> list[str]:
    return [
        str(item.message)
        for item in caught
        if issubclass(item.category, ResourceWarning)
    ]


class MirroredChildOutputTests(unittest.TestCase):
    """The drain thread owns the child's pipe wrapper and closes it at EOF."""

    def test_the_pipe_wrapper_is_closed_when_the_child_exits(self) -> None:
        with _resource_warnings_are_failures() as caught:
            process = subprocess.Popen(
                [sys.executable, "-c", "print('mirrored line')"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            # The mirror echoes to this console by design; keep the test quiet.
            with contextlib.redirect_stdout(io.StringIO()):
                mirrored = web_bridge_launcher._mirror_child_output(
                    process, name="regression"
                )
                process.wait(timeout=30)
                mirrored.reader.join(timeout=10)
            self.assertFalse(mirrored.reader.is_alive())
            stream = process.stdout
            self.assertTrue(stream is None or stream.closed)
            del mirrored
            del process
        self.assertEqual(_leaks(caught), [])


class StopProcessTests(unittest.TestCase):
    """_stop_process leaves neither a running child nor an open pipe."""

    def test_a_running_child_is_stopped_and_its_pipes_are_released(self) -> None:
        with _resource_warnings_are_failures() as caught:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            web_bridge_launcher._stop_process(process)
            self.assertIsNotNone(process.poll())
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    self.assertTrue(stream.closed)
            del process
        self.assertEqual(_leaks(caught), [])

    def test_an_already_finished_child_still_gets_its_pipes_released(self) -> None:
        with _resource_warnings_are_failures() as caught:
            process = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            process.wait(timeout=30)
            web_bridge_launcher._stop_process(process)
            stream = process.stdout
            self.assertTrue(stream is None or stream.closed)
            del process
        self.assertEqual(_leaks(caught), [])


class SharedTranscriptStoreTests(unittest.TestCase):
    """One SQLite connection per runtime, with an explicit close."""

    def setUp(self) -> None:
        self.addCleanup(close_transcript_store)
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def test_repeated_lookups_share_one_store_and_close_releases_it(self) -> None:
        root = Path(self._temporary.name) / "runtime-a"
        with patch(
            "karox.transcript_shadow.runtime_dir", return_value=root
        ):
            with _resource_warnings_are_failures() as caught:
                first = get_transcript_store()
                second = get_transcript_store()
                self.assertIs(first, second)
                close_transcript_store()
                del first
                del second
            self.assertEqual(_leaks(caught), [])

    def test_a_runtime_path_change_closes_the_previous_store(self) -> None:
        root_a = Path(self._temporary.name) / "runtime-a"
        root_b = Path(self._temporary.name) / "runtime-b"
        with _resource_warnings_are_failures() as caught:
            with patch(
                "karox.transcript_shadow.runtime_dir", return_value=root_a
            ):
                stale = get_transcript_store()
            with patch(
                "karox.transcript_shadow.runtime_dir", return_value=root_b
            ):
                fresh = get_transcript_store()
            self.assertIsNot(stale, fresh)
            close_transcript_store()
            del stale
            del fresh
        self.assertEqual(_leaks(caught), [])


class AliasCatalogWarningFilterTests(unittest.TestCase):
    """The SDK 'not listed' warning is dropped only for routable aliases."""

    def _record(self, name: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="mcp.server.lowlevel.server",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Tool '%s' not listed, no validation will be performed",
            args=(name,),
            exc_info=None,
        )

    def test_dotted_aliases_are_quiet_and_unknown_tools_still_warn(self) -> None:
        from karox.proxy_server import (
            _ROUTABLE_ALIAS_NAMES,
            _AliasCatalogLogFilter,
        )

        scoped = _AliasCatalogLogFilter()
        self.assertIn("karox.bridge.diagnostics", _ROUTABLE_ALIAS_NAMES)
        self.assertFalse(scoped.filter(self._record("karox.bridge.diagnostics")))
        self.assertTrue(scoped.filter(self._record("not.a.karox.tool")))
        # Unrelated records pass through untouched.
        unrelated = logging.LogRecord(
            name="mcp.server.lowlevel.server",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="something else entirely",
            args=(),
            exc_info=None,
        )
        self.assertTrue(scoped.filter(unrelated))

    def test_the_filter_is_installed_on_the_sdk_server_logger(self) -> None:
        from karox.proxy_server import _AliasCatalogLogFilter

        target = logging.getLogger("mcp.server.lowlevel.server")
        self.assertTrue(
            any(isinstance(item, _AliasCatalogLogFilter) for item in target.filters)
        )


if __name__ == "__main__":
    unittest.main()
