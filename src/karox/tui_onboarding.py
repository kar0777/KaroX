"""Keyboard-first guide around existing provider and bridge workflows.

Tailscale diagnostics are read-only and rendered from allowlisted status codes,
never from raw subprocess output (which can contain one-time login URLs).
The optional Tailscale installer offer is consent-gated twice and only ever
uses the checksum-pinned official pkgs.tailscale.com asset; guided login and
OS elevation stay with the user.
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .onboarding import OnboardingProgress
from .tailscale import query_tailscale_status
from .tailscale_bootstrap import (
    TailscaleBootstrapError,
    ensure_tailscale_downloads,
    installer_launch_command,
    linux_system_install_hint,
    tailscale_asset,
)


def label(language: str, ru: str, en: str) -> str:
    return ru if language == "ru" else en


class GuideScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Back", priority=True),
        Binding("up", "previous", show=False, priority=True),
        Binding("down", "next", show=False, priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("space", "activate", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    GuideScreen { align: center middle; background: #0e0c08 92%; }
    .guide-dialog { width: 72; max-width: 100%; height: 90%; max-height: 34;
      border: round #c6a56b; padding: 0 1; background: #1a1712; }
    .guide-title { height: auto; color: #e6c773; text-style: bold; }
    .guide-body { height: 1fr; }
    .guide-body Static { height: auto; margin-bottom: 1; }
    .guide-body Button { width: 100%; min-width: 0; height: auto; min-height: 3;
      text-align: left; }
    .guide-footer { height: auto; color: #c6bca8; }
    """

    def __init__(self, language: str = "en") -> None:
        super().__init__()
        self.language = language

    def text(self, ru: str, en: str) -> str:
        return label(self.language, ru, en)

    def footer(self) -> Static:
        return Static(self.text(
            "Tab/↑↓ · Enter/Space · F1 помощь · Esc назад",
            "Tab/↑↓ · Enter/Space · F1 help · Esc back",
        ), classes="guide-footer", markup=False)

    def on_mount(self) -> None:
        self.query(Button).first().focus()

    def action_previous(self) -> None:
        self.focus_previous()

    def action_next(self) -> None:
        self.focus_next()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_activate(self) -> None:
        focused = self.app.focused
        if isinstance(focused, Button) and not focused.disabled:
            focused.press()

    def action_help(self) -> None:
        self.app.push_screen(NavigationHelpScreen(self.language))

    # Subclasses that handle their own Button.Pressed events declare which
    # button ids the base dismiss-all handler may still close the screen for.
    # None (the default) keeps the historical "any button dismisses" contract.
    dismiss_button_ids: ClassVar[set[str] | None] = None

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        if self.dismiss_button_ids is not None and event.button.id not in self.dismiss_button_ids:
            # A dedicated subclass handler owns this button; never dismiss.
            return
        self.dismiss(event.button.id)


class NavigationHelpScreen(GuideScreen):
    def compose(self) -> ComposeResult:
        with Vertical(classes="guide-dialog"):
            yield Static(self.text("Клавиатура", "Keyboard help"), classes="guide-title")
            with VerticalScroll(classes="guide-body"):
                yield Static(self.text(
                    "Tab / Shift+Tab — следующий / предыдущий элемент.\n"
                    "Enter / Space — кнопка или переключатель Bypass в фокусе.\n"
                    "Esc — назад без изменений.\n"
                    "В форме моста: F6 — помощь Tailscale, F10 — запуск.\n"
                    "На странице сервиса: B — фокус Bypass (не меняет режим), "
                    "F5 — проверка, F2 — настройки, Ctrl+W — проект.\n"
                    "В чате: F4 — продолжить настройку, Ctrl+S — подключения.\n"
                    "Bypass не отменяет границы удалённого push и доступа к секретам.",
                    "Tab / Shift+Tab — next / previous control.\n"
                    "Enter / Space — activate the focused button or Bypass switch.\n"
                    "Esc — back without changes.\n"
                    "Bridge form: F6 — Tailscale help, F10 — start.\n"
                    "Service page: B — focus Bypass (no mode change), "
                    "F5 — verify, F2 — settings, Ctrl+W — project.\n"
                    "Chat: F4 — resume setup, Ctrl+S — connections.\n"
                    "Bypass does not remove remote push or secret-access boundaries.",
                ), markup=False)
                yield Button(self.text("Назад", "Back"), id="help-back")
            yield self.footer()

    def action_help(self) -> None:
        pass  # Never stack help on itself.


class FirstRunScreen(GuideScreen):
    def __init__(self, language: str, progress: OnboardingProgress) -> None:
        super().__init__(language)
        self.progress = progress

    def compose(self) -> ComposeResult:
        step = self.progress.step
        titles = {
            "provider": self.text("2/5 · API-провайдер", "2/5 · API provider"),
            "bridge": self.text("3/5 · Внешний клиент (необязательно)", "3/5 · External client (optional)"),
            "tunnel": self.text("4/5 · Публичный HTTPS", "4/5 · Public HTTPS"),
            "review": self.text("5/5 · Завершение", "5/5 · Finish setup"),
        }
        with Vertical(classes="guide-dialog"):
            yield Static(titles[step], classes="guide-title")
            with VerticalScroll(classes="guide-body"):
                if step == "provider":
                    yield Static(self.text(
                        "Язык выбран. Подключите API через обычную проверку модели. "
                        "Ключ хранится только в OS keyring. Можно пропустить для веб-клиента.",
                        "Language selected. Connect an API through the existing model verification. "
                        "The key stays in the OS keyring. You may skip this for a web client.",
                    ), markup=False)
                    yield Button(self.text("Подключить API", "Connect API"), id="setup-provider")
                    yield Button(self.text("Пропустить API", "Skip API"), id="setup-skip-provider")
                elif step == "bridge":
                    yield Static(self.text(
                        "Подписка ChatGPT/Claude не является API-ключом. "
                        "Настройка моста разрешит выбранному клиенту доступ к проекту. "
                        "Ничего не запускается до F10 в форме моста.",
                        "A ChatGPT/Claude subscription is not an API key. "
                        "A bridge lets the chosen client access this project. "
                        "Nothing starts until F10 in the bridge form.",
                    ), markup=False)
                    yield Button("ChatGPT Web", id="setup-chatgpt-web")
                    yield Button("Claude Web", id="setup-claude-web")
                    yield Button(self.text("Без моста", "Skip bridge"), id="setup-skip-bridge")
                elif step == "tunnel":
                    yield Static(self.text(
                        "Cloudflare — быстрый HTTPS; URL может смениться при перезапуске. "
                        "cloudflared загрузится с проверкой контрольной суммы. Tailscale Funnel — стабильный *.ts.net; "
                        "нужны установка, вход и разрешение Funnel. Локальный URL не подходит веб-клиентам.",
                        "Cloudflare — quick HTTPS; URL may change on restart. "
                        "cloudflared downloads automatically with checksum verification. Tailscale Funnel — stable *.ts.net; "
                        "requires installation, sign-in and Funnel permission. A local URL cannot serve web clients.",
                    ), markup=False)
                    yield Button("Cloudflare", id="setup-cloudflare")
                    yield Button(self.text("Tailscale — проверка и помощь", "Tailscale — check and setup"), id="setup-tailscale")
                else:
                    blocked = self.progress.bridge_outcome not in {"skipped", "configured"}
                    yield Static(self.text(
                        "Запрошенный мост должен пройти проверку. Если запуск или проверка не удались, "
                        "вернитесь и повторите или явно пропустите мост. Пропуск закрывает руководство, "
                        "но не означает успешную настройку. Esc сохраняет шаг; F4 продолжит его.",
                        "A requested bridge must pass verification. If launch or verification failed, "
                        "go back and retry or explicitly skip the bridge. Skipping dismisses the guide, "
                        "not successful configuration. Esc keeps progress; F4 resumes it.",
                    ), markup=False)
                    yield Button(self.text("Завершить руководство", "Finish guide"), id="setup-finish", disabled=blocked)
                if step != "provider":
                    yield Button(self.text("Предыдущий шаг", "Previous step"), id="setup-back")
                yield Button(self.text("Позже · F4 продолжить", "Later · F4 to resume"), id="setup-later")
            yield self.footer()


class TailscaleSetupScreen(GuideScreen):
    """Tailscale readiness guide with an optional consent-gated installer offer.

    Every button here is handled by this screen's own handler: the base
    dismiss-all must never close the screen (an install press is not a choice).
    """
    dismiss_button_ids: ClassVar[set[str]] = set()
    BINDINGS = [Binding("f5", "check", "Check", priority=True)]

    def __init__(self, language: str = "en") -> None:
        super().__init__(language)
        self._checking = False
        self._ready = False
        self._installing = False
        self._install_consented = False
        self._tailscale_path: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="guide-dialog"):
            yield Static("Tailscale Funnel", classes="guide-title")
            with VerticalScroll(classes="guide-body"):
                yield Static(self.text(
                    "Вариант 1 — KaroX предложит установить официальный пакет Tailscale "
                    "(двойное подтверждение, SHA-256, источник pkgs.tailscale.com). "
                    "Вход в Tailscale и подтверждение UAC/GUI всегда остаются за вами.\n"
                    "Вариант 2 — установить и войти вручную: https://tailscale.com/download\n"
                    "3. Разрешите Funnel в политике tailnet, если это требуется: "
                    "https://tailscale.com/kb/1223/funnel\n"
                    "F5 — проверить (только чтение).",
                    "Option 1 — KaroX offers the official Tailscale package (double "
                    "consent, SHA-256, source pkgs.tailscale.com). Tailscale sign-in "
                    "and OS elevation stay with you.\n"
                    "Option 2 — install and sign in manually: https://tailscale.com/download\n"
                    "3. Allow Funnel in your tailnet policy if required: "
                    "https://tailscale.com/kb/1223/funnel\n"
                    "F5 — check (read-only).",
                ), markup=False)
                yield Static(self.text("Не проверено", "Not checked"), id="tailscale-status", markup=False)
                yield Button(self.text("F5  Проверить (только чтение)", "F5  Check (read-only)"), id="tailscale-check")
                yield Button(self.text("Разрешить Tailscale", "Allow Tailscale"), id="tailscale-use", disabled=True)
                yield Button(self.text("Выбрать Cloudflare", "Use Cloudflare instead"), id="tailscale-cloudflare")
                yield Button(
                    self.text("Установить сейчас (официальный пакет)", "Install now (official package)"),
                    id="tailscale-install",
                )
            yield self.footer()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "tailscale-install":
            self._begin_install()
        elif event.button.id == "tailscale-check":
            self.action_check()
        elif event.button.id == "tailscale-use":
            if self._ready:
                self.dismiss("tailscale")
        elif event.button.id == "tailscale-cloudflare":
            self.dismiss("cloudflare")

    # -- consent-gated official installer offer ------------------------------

    def _begin_install(self) -> None:
        if self._installing or self._checking:
            return
        install_button = self.query_one("#tailscale-install", Button)
        try:
            asset = tailscale_asset()
        except TailscaleBootstrapError as exc:
            self.query_one("#tailscale-status", Static).update(self.text(str(exc), str(exc)))
            return
        if not self._install_consented:
            self._install_consented = True
            self.query_one("#tailscale-status", Static).update(self.text(
                "Будет скачан и запущен официальный пакет:\n"
                f"{asset.name}\nисточник: https://pkgs.tailscale.com/stable/{asset.name}\n"
                f"SHA-256: {asset.sha256}\n"
                "Нажмите кнопку ещё раз для подтверждения.",
                "The official package will be downloaded and launched:\n"
                f"{asset.name}\nsource: https://pkgs.tailscale.com/stable/{asset.name}\n"
                f"SHA-256: {asset.sha256}\n"
                "Press the button again to confirm.",
            ))
            install_button.label = self.text("Подтверждаю — скачать и запустить", "Confirm — download and run")
            return
        self._installing = True
        install_button.disabled = True
        self.query_one("#tailscale-status", Static).update(self.text(
            f"Загрузка {asset.name} с pkgs.tailscale.com (проверка SHA-256)…",
            f"Downloading {asset.name} from pkgs.tailscale.com (SHA-256 check)…",
        ))
        self.run_worker(self._install_worker, thread=True, group="tailscale-install", exclusive=True)

    def _install_worker(self) -> None:
        try:
            result = ensure_tailscale_downloads()
            kind = tailscale_asset().kind
        except TailscaleBootstrapError as exc:
            with contextlib.suppress(Exception):
                self.app.call_from_thread(self._install_done, None, str(exc))
            return
        if kind in {"exe", "pkg"}:
            try:
                command = installer_launch_command(Path(result["installer"]))
                if command[0] == "open":
                    subprocess.run(command, check=False, timeout=30)
                else:
                    subprocess.Popen(command)
            except OSError as exc:
                with contextlib.suppress(Exception):
                    self.app.call_from_thread(
                        self._install_done, None,
                        "Не удалось запустить установщик: " + type(exc).__name__,
                    )
                return
            with contextlib.suppress(Exception):
                self.app.call_from_thread(self._install_done, "installer", None)
            return
        # Linux: user-owned binaries are unpacked; the system daemon needs the
        # official distro commands, printed for one-copy-paste.
        codename = "trixie"
        with contextlib.suppress(OSError):
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if line.startswith("VERSION_CODENAME="):
                    codename = line.split("=", 1)[1].strip().strip('"') or codename
                    break
        with contextlib.suppress(Exception):
            self.app.call_from_thread(
                self._install_done,
                {
                    "tailscale": result.get("tailscale", ""),
                    "tailscaled": result.get("tailscaled", ""),
                    "hint": linux_system_install_hint(codename),
                },
                None,
            )

    def _install_done(self, result: object, error: str | None) -> None:
        self._installing = False
        install_button = self.query_one("#tailscale-install", Button)
        install_button.disabled = False
        install_button.label = self.text(
            "Установить сейчас (официальный пакет)", "Install now (official package)"
        )
        self._install_consented = False
        status = self.query_one("#tailscale-status", Static)
        if error:
            status.update(self.text("Ошибка установки: " + error, "Install error: " + error))
            return
        if isinstance(result, dict) and "tailscale" in result:
            self._tailscale_path = result["tailscale"]
            status.update(self.text(
                "Бинарники Tailscale распакованы (без root):\n"
                f"tailscale: {result['tailscale']}\n"
                f"tailscaled: {result['tailscaled']}\n\n"
                "Для системного демона выполните в терминале (официальный способ):\n"
                + result["hint"]
                + "\n\nЗатем F5 — проверка.",
                "Tailscale binaries unpacked (no root):\n"
                f"tailscale: {result['tailscale']}\n"
                f"tailscaled: {result['tailscaled']}\n\n"
                "For the system daemon run in a terminal (official path):\n"
                + result["hint"]
                + "\n\nThen F5 — check.",
            ))
            return
        status.update(self.text(
            "Установщик запущен. Завершите установку (UAC/GUI) и при необходимости "
            "войдите в приложении Tailscale, затем F5 — проверка.",
            "Installer launched. Finish the install (UAC/GUI) and sign in through "
            "the Tailscale app if needed, then F5 — check.",
        ))

    def action_check(self) -> None:
        if self._checking:
            return
        self._checking = True
        self._ready = False
        self.query_one("#tailscale-use", Button).disabled = True
        self.query_one("#tailscale-status", Static).update(self.text("Проверка…", "Checking…"))
        self.run_worker(self._probe, thread=True, group="tailscale-readiness", exclusive=True)

    def _probe(self) -> None:
        try:
            status = query_tailscale_status(executable=self._tailscale_path)
            ready = status.get("ready") is True
            code = str(status.get("code", "unknown"))
        except Exception:
            ready, code = False, "unknown"
        # The user can leave while a slow daemon is answering.
        with contextlib.suppress(Exception):
            self.app.call_from_thread(self._checked, ready, code)

    def _checked(self, ready: bool, code: str) -> None:
        if not self.is_mounted:
            return
        self._checking = False
        self._ready = ready
        messages = {
            "not_installed": self.text("Установите Tailscale вручную (шаг 1).", "Install Tailscale manually (step 1)."),
            "login_required": self.text("Войдите в приложении Tailscale (шаг 2).", "Sign in through the Tailscale app (step 2)."),
            "machine_approval_required": self.text("Нужно одобрение администратора tailnet.", "Ask your tailnet admin to approve this device."),
        }
        message = self.text("Tailscale подключён. Funnel проверяется при запуске.", "Tailscale is online. Funnel is checked at launch.") if ready else messages.get(code, self.text(
            "Tailscale не готов. Проверьте приложение/службу и повторите F5; либо выберите Cloudflare.",
            "Tailscale is not ready. Check its app/service and retry F5, or use Cloudflare.",
        ))
        self.query_one("#tailscale-status", Static).update(message)
        self.query_one("#tailscale-use", Button).disabled = not ready
