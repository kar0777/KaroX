from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.cli import _parser, main


WARNING_FRAGMENT = "Use only fake/test/non-important accounts"


class _FakeBrowserCredentialStore:
    def __init__(self) -> None:
        self.saved: dict[str, tuple[str, str]] = {}

    def set(self, name: str, *, username: str, password: str) -> dict[str, str]:
        self.saved[name] = (username, password)
        return {
            "reference": f"os-keyring:browser/{name}",
            "fingerprint": "sha256:0123456789ab",
            "backend": "os-keyring",
        }

    def delete(self, name: str) -> dict[str, str]:
        self.saved.pop(name, None)
        return {"reference": f"os-keyring:browser/{name}", "status": "deleted"}

    def doctor(self) -> dict[str, str]:
        return {
            "status": "ok",
            "backend": "os-keyring",
            "protection": "os-protected",
            "scope": "browser",
        }


def test_browser_credential_parser_has_no_password_argv_option() -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["browser-credential", "set", "test-login", "--password", "must-not-exist"]
        )


def test_browser_credential_stdin_set_never_echoes_secret(capsys) -> None:
    store = _FakeBrowserCredentialStore()
    secret = "local-only-cli-secret"
    stdin = io.StringIO(f"robot@example.invalid\n{secret}\n")
    with (
        patch("karox.cli.BrowserCredentialStore", return_value=store),
        patch.object(sys, "stdin", stdin),
    ):
        code = main(["browser-credential", "set", "test-login", "--stdin", "--json"])

    captured = capsys.readouterr()
    assert code == 0
    assert store.saved["test-login"] == ("robot@example.invalid", secret)
    assert secret not in captured.out
    assert secret not in captured.err
    payload = json.loads(captured.out)
    assert payload["reference"] == "os-keyring:browser/test-login"
    assert payload["backend"] == "os-keyring"
    assert WARNING_FRAGMENT in payload["warning"]


def test_browser_credential_interactive_password_uses_getpass_and_warns(capsys) -> None:
    store = _FakeBrowserCredentialStore()
    with (
        patch("karox.cli.BrowserCredentialStore", return_value=store),
        patch("builtins.input", return_value="robot@example.invalid"),
        patch("karox.cli.getpass.getpass", return_value="hidden-pass") as getpass_mock,
    ):
        code = main(["browser-credential", "set", "test-login"])

    captured = capsys.readouterr()
    assert code == 0
    getpass_mock.assert_called_once()
    assert store.saved["test-login"] == ("robot@example.invalid", "hidden-pass")
    assert "hidden-pass" not in captured.out
    assert "hidden-pass" not in captured.err
    assert WARNING_FRAGMENT in captured.err


def test_browser_credential_doctor_and_delete_are_secret_free(capsys) -> None:
    store = _FakeBrowserCredentialStore()
    store.saved["test-login"] = ("robot@example.invalid", "never-print-me")
    with patch("karox.cli.BrowserCredentialStore", return_value=store):
        assert main(["browser-credential", "doctor", "--json"]) == 0
        doctor = json.loads(capsys.readouterr().out)
        assert doctor["protection"] == "os-protected"
        assert WARNING_FRAGMENT in doctor["warning"]

        assert main(["browser-credential", "delete", "test-login", "--json"]) == 0
        deleted = json.loads(capsys.readouterr().out)

    assert deleted == {
        "reference": "os-keyring:browser/test-login",
        "status": "deleted",
    }
    assert "never-print-me" not in repr(deleted)
