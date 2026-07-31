#!/usr/bin/env python3
"""Apply reviewed KaroX 5 connection and CLI wording fixes atomically.

The connected KaroX profile used during this review may expose full-file writes
without a bounded edit operation or process execution. This helper keeps the
large runtime edit mechanical, preflights every reviewed replacement, supports a
partly applied reviewed state, and is idempotent after the complete fix.

It intentionally does not rename the ``karox-vnext`` compatibility launcher or
versioned storage paths. Those require a separate compatibility/migration
decision, not a copy edit.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "src" / "karox" / "web_bridge_launcher.py"
CLI = ROOT / "src" / "karox" / "cli.py"
TEST = ROOT / "tests" / "test_web_bridge_launcher.py"

OLD_SOURCE = '''    if selected == "ru":
        if profile == "chatgpt-web":
            return (
                "Как подключить мост к ChatGPT:",
                "  1. Откройте в ChatGPT: Настройки → Плагины.",
                "  2. Создайте приложение в режиме разработчика и вставьте MCP URL из строки выше.",
                "  3. Сохраните приложение, нажмите «Подключить», затем «Войти через KaroX».",
                "  4. На странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
                "Важно: пароль вводится только на странице KaroX, не в настройках приложения.",
            )
        return (
            "Как подключить мост к Claude:",
            "  1. Откройте Claude: Settings → Connectors → Add custom connector.",
            "  2. Вставьте MCP URL из строки выше и запустите подключение.",
            "  3. На странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
            "Важно: пароль вводится только на странице KaroX, не в настройках коннектора.",
        )
    if profile == "chatgpt-web":
        return (
            "How to connect the bridge to ChatGPT:",
            "  1. Open ChatGPT Settings → Plugins.",
            "  2. Create a developer-mode app and paste the MCP URL shown above.",
            "  3. Save it, click Connect, then click Sign in with KaroX.",
            "  4. On the KaroX page, paste the approval password shown above and click Authorize.",
            "Important: enter the password only on the KaroX page, not in the app settings.",
        )
'''

NEW_SOURCE = '''    if selected == "ru":
        if profile == "chatgpt-web":
            return (
                "Как подключить мост к ChatGPT:",
                "  1. Нужен ChatGPT Web с доступом к developer mode и custom MCP apps.",
                "  2. В ChatGPT откройте Настройки → Приложения и включите developer mode в расширенных настройках, если он доступен.",
                "  3. Создайте custom app через Приложения → Создать и вставьте MCP URL из строки выше.",
                "  4. Нажмите сканирование инструментов и завершите OAuth через KaroX.",
                "  5. На странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
                "Важно: пароль вводится только на странице KaroX, не в настройках приложения.",
            )
        return (
            "Как подключить мост к Claude:",
            "  1. Нужен тариф Claude с поддержкой custom connectors.",
            "  2. Откройте Claude: Settings → Connectors → Add custom connector.",
            "  3. Вставьте MCP URL из строки выше и добавьте connector.",
            "  4. На странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
            "Важно: пароль вводится только на странице KaroX, не в настройках коннектора.",
        )
    if profile == "chatgpt-web":
        return (
            "How to connect the bridge to ChatGPT:",
            "  1. Use ChatGPT Web with access to developer mode and custom MCP apps.",
            "  2. Open Settings → Apps and enable developer mode in Advanced Settings when available.",
            "  3. Create a custom app from Apps → Create and paste the MCP URL shown above.",
            "  4. Scan tools and complete the OAuth prompt through KaroX.",
            "  5. On the KaroX page, paste the approval password shown above and click Authorize.",
            "Important: enter the password only on the KaroX page, not in the app settings.",
        )
'''

OLD_TEST = '''class WebBridgeInstructionTests(unittest.TestCase):
    def test_russian_chatgpt_steps_name_the_ui_and_password_destination(self) -> None:
        text = "\\n".join(
            web_bridge_connection_instructions("chatgpt-web", language="ru")
        )
        self.assertIn("Настройки → Плагины", text)
        self.assertIn("MCP URL", text)
        self.assertIn("Войти через KaroX", text)
        self.assertIn("только на странице KaroX", text)
'''

NEW_TEST = '''class WebBridgeInstructionTests(unittest.TestCase):
    def test_connection_steps_name_current_ui_and_password_destination(self) -> None:
        for language, settings, create in (
            ("ru", "Настройки → Приложения", "Приложения → Создать"),
            ("en", "Settings → Apps", "Apps → Create"),
        ):
            with self.subTest(profile="chatgpt-web", language=language):
                text = "\\n".join(
                    web_bridge_connection_instructions(
                        "chatgpt-web", language=language
                    )
                )
                self.assertIn(settings, text)
                self.assertIn(create, text)
                self.assertIn("MCP URL", text)
                self.assertNotIn("Plugins", text)
                self.assertNotIn("Плагины", text)

        russian = "\\n".join(
            web_bridge_connection_instructions("chatgpt-web", language="ru")
        )
        self.assertIn("только на странице KaroX", russian)

        claude = "\\n".join(
            web_bridge_connection_instructions("claude-web", language="en")
        )
        self.assertIn("Settings → Connectors → Add custom connector", claude)
        self.assertIn("MCP URL", claude)
        self.assertIn("only on the KaroX page", claude)
'''

CLI_REPLACEMENTS = (
    (
        '"""Command-line entry point for the KaroX vNext foundation."""',
        '"""Command-line entry point for the KaroX 5 hybrid runtime."""',
    ),
    (
        'help="manage durable vNext sessions"',
        'help="manage durable KaroX sessions"',
    ),
    (
        'help="run aggregate vNext diagnostics"',
        'help="run aggregate KaroX diagnostics"',
    ),
)


@dataclass(frozen=True)
class FilePlan:
    path: Path
    original: str
    updated: str

    @property
    def changed(self) -> bool:
        return self.updated != self.original


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _plan_replacements(
    path: Path,
    replacements: tuple[tuple[str, str], ...],
) -> FilePlan:
    original = _read_text(path)
    updated = original

    for old, new in replacements:
        old_count = updated.count(old)
        new_count = updated.count(new)
        if old_count == 1 and new_count == 0:
            updated = updated.replace(old, new, 1)
        elif old_count == 0 and new_count == 1:
            continue
        else:
            raise SystemExit(
                f"{path}: expected one old or one new reviewed block; "
                f"found old={old_count}, new={new_count}"
            )

    return FilePlan(path=path, original=original, updated=updated)


def _restore(plans: list[FilePlan]) -> None:
    failures: list[str] = []
    for plan in reversed(plans):
        try:
            _atomic_write(plan.path, plan.original)
        except OSError as exc:
            failures.append(f"{plan.path}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit("rollback failed:\n  - " + "\n  - ".join(failures))


def _complete_fix_is_present() -> bool:
    """Recognize the reviewed end state after formatters have rewritten layout.

    The original migration path intentionally uses exact large-block replacements,
    but Black may wrap those blocks after the fix is applied. A later preflight
    must therefore validate semantic markers instead of requiring the formatted
    file to remain byte-identical to ``NEW_SOURCE`` or ``NEW_TEST``.
    """
    launcher = _read_text(LAUNCHER)
    test = _read_text(TEST)
    cli = _read_text(CLI)

    launcher_markers = (
        "Настройки → Приложения",
        "Приложения → Создать",
        "Settings → Apps",
        "Apps → Create",
        "Нужен тариф Claude с поддержкой custom connectors.",
    )
    test_markers = (
        "test_connection_steps_name_current_ui_and_password_destination",
        'self.assertNotIn("Plugins", text)',
        'self.assertNotIn("Плагины", text)',
    )
    cli_markers = (
        '"""Command-line entry point for the KaroX 5 hybrid runtime."""',
        'help="manage durable KaroX sessions"',
        'help="run aggregate KaroX diagnostics"',
    )
    stale_launcher_markers = (
        "Settings → Plugins",
        "Настройки → Плагины",
    )
    stale_cli_markers = (
        '"""Command-line entry point for the KaroX vNext foundation."""',
        'help="manage durable vNext sessions"',
        'help="run aggregate vNext diagnostics"',
    )

    return (
        all(marker in launcher for marker in launcher_markers)
        and all(marker in test for marker in test_markers)
        and all(marker in cli for marker in cli_markers)
        and not any(marker in launcher for marker in stale_launcher_markers)
        and not any(marker in cli for marker in stale_cli_markers)
    )


def main() -> int:
    if _complete_fix_is_present():
        print("KaroX 5 connection and CLI wording fix is already applied")
        return 0

    plans = [
        _plan_replacements(LAUNCHER, ((OLD_SOURCE, NEW_SOURCE),)),
        _plan_replacements(TEST, ((OLD_TEST, NEW_TEST),)),
        _plan_replacements(CLI, CLI_REPLACEMENTS),
    ]
    changed = [plan for plan in plans if plan.changed]
    if not changed:
        print("KaroX 5 connection and CLI wording fix is already applied")
        return 0

    written: list[FilePlan] = []
    try:
        for plan in changed:
            _atomic_write(plan.path, plan.updated)
            written.append(plan)
        for plan in changed:
            if _read_text(plan.path) != plan.updated:
                raise RuntimeError(f"postcondition failed for {plan.path}")
    except BaseException:
        _restore(written)
        raise

    print("updated ChatGPT/Claude connection instructions, CLI help, and regression test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
