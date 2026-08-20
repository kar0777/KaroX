from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC  # noqa: F401

from karox.core import CoreRuntime


class GuardedProcessStdioTests(unittest.TestCase):
    def test_guarded_child_gets_valid_devnull_stdin_instead_of_inheriting_bridge_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = object.__new__(CoreRuntime)
            runtime.repository = Path(temp)
            process = mock.Mock()
            process.pid = 4242
            process.wait.return_value = 0

            with (
                mock.patch("karox.core._resolve_executable", side_effect=lambda argv: list(argv)),
                mock.patch("karox.core._new_process_group_kwargs", return_value={}),
                mock.patch("karox.core.ProcessTree") as process_tree,
                mock.patch("karox.core.subprocess.Popen", return_value=process) as popen,
            ):
                result = runtime._run(["python", "-c", "pass"], 1.0)

        self.assertEqual(result["exit_code"], 0)
        self.assertIs(popen.call_args.kwargs.get("stdin"), subprocess.DEVNULL)
        process_tree.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
