from __future__ import annotations

import unittest
from unittest.mock import patch

import _path_setup
from karox.entrypoint import main


class EllipsisEntrypointTests(unittest.TestCase):
    def test_bare_agent_routes_to_ellipsis(self) -> None:
        with patch("karox.ellipsis_cli.main", return_value=0) as ellipsis:
            self.assertEqual(main(["agent"]), 0)
        ellipsis.assert_called_once_with(["ellipsis"])

    def test_agent_options_route_to_ellipsis(self) -> None:
        with patch("karox.ellipsis_cli.main", return_value=0) as ellipsis:
            self.assertEqual(
                main(["agent", "--repository", "D:/project", "--task", "fix"]),
                0,
            )
        ellipsis.assert_called_once_with(
            ["ellipsis", "--repository", "D:/project", "--task", "fix"]
        )

    def test_lifecycle_subcommand_routes_without_extra_wrapper(self) -> None:
        with patch("karox.ellipsis_cli.main", return_value=0) as ellipsis:
            self.assertEqual(main(["agent", "status", "session-1"]), 0)
        ellipsis.assert_called_once_with(["status", "session-1"])

    def test_legacy_agent_run_is_preserved(self) -> None:
        with patch("karox.cli.main", return_value=0) as legacy:
            self.assertEqual(main(["agent", "run", "--provider", "openai"]), 0)
        legacy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
