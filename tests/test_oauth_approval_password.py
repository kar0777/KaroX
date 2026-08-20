"""OAuth approval-password CLI: secret never printed, clipboard-served.

Phase 1 fix: the bridge launcher used to print the approval password (which IS
the bridge credential) at startup. After rotation that line went stale, and it
was a plaintext leak regardless. The new ``karox bridge oauth approval-password
--saved NAME --copy --quiet`` command resolves the CURRENT value at copy time
and never prints it.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import unittest
from unittest import mock

import _path_setup
from karox import clipboard
from karox.bridge import BridgeCredentialStore


class _FakeCredentialBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    from karox import cli

    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class OauthApprovalPasswordTests(unittest.TestCase):
    """The approval-password command never leaks the value."""

    def setUp(self) -> None:
        self.backend = _FakeCredentialBackend()
        self.backend.store[("KaroX/bridge", "web-saved-test-session")] = "test-oauth-secret-value"
        self._recording = []

        def fake_write(text: str) -> bool:
            self._recording.append(text)
            return True

        self._patches = [
            mock.patch(
                "karox.bridge.BridgeCredentialStore",
                lambda backend=None: BridgeCredentialStore(backend=self.backend),
            ),
            mock.patch.object(clipboard, "write_text", fake_write),
            mock.patch.object(clipboard, "schedule_clear"),
            mock.patch(
                "karox.web_bridge_launcher.saved_web_bridge_session_candidates",
                return_value=("web-saved-test-session",),
            ),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_plain_show_never_prints_secret(self) -> None:
        code, out, err = _run_cli([
            "bridge", "oauth", "approval-password", "--saved", "test-profile", "--json"
        ])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertNotIn("secret", payload)
        self.assertNotIn("password", {k.lower() for k in payload})
        self.assertTrue(payload["fingerprint"].startswith("sha256:"))
        # The actual value must not appear anywhere in output.
        self.assertNotIn("test-oauth-secret-value", out)
        self.assertNotIn("test-oauth-secret-value", err)

    def test_copy_puts_value_on_clipboard_not_stdout(self) -> None:
        code, out, err = _run_cli([
            "bridge", "oauth", "approval-password", "--saved", "test-profile",
            "--copy", "--quiet", "--json"
        ])
        self.assertEqual(code, 0)
        self.assertEqual(self._recording, ["test-oauth-secret-value"])
        payload = json.loads(out)
        self.assertEqual(payload["clipboard"], "copied")
        self.assertNotIn("test-oauth-secret-value", out)
        self.assertNotIn("test-oauth-secret-value", err)

    def test_no_credential_returns_error_without_crash(self) -> None:
        self.backend.store.clear()
        code, out, err = _run_cli([
            "bridge", "oauth", "approval-password", "--saved", "missing", "--json"
        ])
        self.assertNotEqual(code, 0)
        self.assertNotIn("test-oauth-secret-value", out)


class LauncherDoesNotPrintPlaintextPassword(unittest.TestCase):
    """The launcher source must not print the approval password value."""

    def test_no_plaintext_password_in_launcher(self) -> None:
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "src" / "karox" / "web_bridge_launcher.py"
        text = source.read_text(encoding="utf-8")
        # The old line printed the value; the new code prints only a fingerprint.
        self.assertNotIn(
            'print(f"OAuth approval password: {secret}")',
            text,
            "launcher must not print the approval password value",
        )


_USER_SURFACE_MODULES = (
    "web_bridge_launcher.py",
    "tui.py",
    "tui_connections.py",
)
_TOKEN_STRIP = "\"'`.,;:()[]<>/"
# Words that follow a printed command inside the same literal but are prose,
# not part of the command.
_PROSE_TOKENS = frozenset({
    "a", "an", "and", "after", "again", "before", "be", "been", "close",
    "first", "for", "from", "if", "in", "into", "is", "it", "its", "keep",
    "new", "no", "not", "now", "of", "on", "open", "or", "paste", "press",
    "revoke", "run", "see", "start", "stop", "that", "the", "then", "this",
    "to", "until", "use", "using", "wait", "when", "while", "with",
    "without", "yes",
})


def _command_tokens(text: str, top_level: frozenset[str]) -> list[str]:
    tokens: list[str] = []
    for raw in text.split():
        token = raw.strip(_TOKEN_STRIP)
        if not token or not token.isascii():
            break
        if token.startswith("[") or token.endswith("]"):
            break
        if token in _PROSE_TOKENS:
            break
        if len(tokens) == 1 and token not in top_level:
            break
        tokens.append(token)
    return tokens


def _string_nodes(tree: ast.Module) -> list[str]:
    """Yield printable string values, skipping docstrings and JoinedStr parts.

    A JoinedStr is reported once with placeholders resolved; its inner
    Constants must not be visited again or the same text appears truncated.
    """
    skip: set[int] = set()
    values: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return  # docstring-shaped expression: prose by contract
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
            return
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for item in node.values:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    parts.append(item.value)
                else:
                    parts.append("profile")
            values.append("".join(parts))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    del skip
    return values


def _collect_printed_command_texts(top_level: frozenset[str]) -> list[str]:
    """Reconstruct every ``karox ...`` command embedded in user-facing strings."""
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[1] / "src" / "karox"
    commands: list[str] = []
    for name in _USER_SURFACE_MODULES:
        tree = ast.parse((src_root / name).read_text(encoding="utf-8"))
        for value in _string_nodes(tree):
            for match in re.finditer(r"\bkarox\s+[a-z]", value):
                tokens = _command_tokens(value[match.start():], top_level)
                if len(tokens) >= 3:
                    commands.append(" ".join(tokens))
    return commands


class PrintedCommandsActuallyParse(unittest.TestCase):
    """Every command KaroX prints to the user must be a real CLI command.

    Regression: the launcher printed ``karox bridge oauth approval-password
    copy --saved NAME --quiet`` while the real command is ``... --saved NAME
    --copy --quiet``; users got ``unrecognized arguments: copy``.
    """

    def test_every_printed_command_is_recognized_by_the_parser(self) -> None:
        import shlex

        from karox.cli import _parser

        parser = _parser()
        import argparse

        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        top_level = frozenset(subparsers.choices or ())
        commands = _collect_printed_command_texts(top_level)
        self.assertTrue(commands, "expected at least one printed karox command")
        failures: list[str] = []
        for command in commands:
            try:
                parser.parse_args(shlex.split(command)[1:])
            except SystemExit:
                failures.append(command)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
