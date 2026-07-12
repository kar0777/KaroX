#!/usr/bin/env python3
"""Localized persistent Notion connection wizard for KaroX.

The wizard is the single user-facing setup flow on Windows, macOS, and Linux.
It detects the language selected in KaroX, starts Tailscale when possible,
waits for a stable *.ts.net hostname, and prints exact Notion MCP steps.
"""
from __future__ import annotations

import argparse
import json
import locale
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from notion_profile import (
    ensure_profile,
    profile_path,
    public_view,
    rotate_key,
    set_url,
)
from tailscale_readiness import find_tailscale, query_status, wait_until_ready

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _default_config_dir() -> Path:
    override = os.environ.get("KAROX_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "RepoPilotBridge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RepoPilotBridge"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "RepoPilotBridge"


def selected_language() -> str:
    override = os.environ.get("KAROX_LANGUAGE", "").strip().lower()
    if override in {"ru", "en"}:
        return override

    settings_path = _default_config_dir() / "settings.json"
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        value = str(payload.get("language", "")).strip().lower()
        if value in {"ru", "русский", "russian"}:
            return "ru"
        if value in {"en", "english"}:
            return "en"
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass

    language = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
    return "ru" if language.startswith("ru") else "en"


LANGUAGE = selected_language()


def tr(en: str, ru: str) -> str:
    return ru if LANGUAGE == "ru" else en


def print_header() -> None:
    print()
    print(tr("KaroX <-> Notion persistent connection", "KaroX <-> Notion: постоянное подключение"))
    print("-" * 56)


def _run_quiet(command: list[str], timeout: float = 15.0) -> int:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def start_tailscale_app() -> bool:
    """Best-effort start of the Tailscale service and desktop application."""
    started = False
    if os.name == "nt":
        # The service may already be running; sc.exe is safe to call repeatedly.
        _run_quiet(["sc.exe", "start", "Tailscale"], timeout=10)
        candidates: list[Path] = []
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(Path(base) / "Tailscale" / "tailscale-ipn.exe")
        for candidate in candidates:
            if candidate.is_file():
                try:
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    subprocess.Popen(
                        [str(candidate)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                    )
                    started = True
                except OSError:
                    pass
                break
    elif sys.platform == "darwin":
        if shutil.which("open"):
            started = _run_quiet(["open", "-g", "-a", "Tailscale"], timeout=10) == 0
    else:
        if shutil.which("systemctl"):
            if _run_quiet(["systemctl", "--user", "start", "tailscaled"], timeout=10) == 0:
                started = True
            elif _run_quiet(["systemctl", "start", "tailscaled"], timeout=10) == 0:
                started = True
    if started:
        time.sleep(2)
    return started


def install_tailscale_windows() -> str | None:
    winget = shutil.which("winget")
    if not winget:
        return None
    print(tr(
        "Tailscale is not installed. Installing it with winget...",
        "Tailscale не установлен. Устанавливаю его через winget...",
    ))
    result = subprocess.run(
        [
            winget,
            "install",
            "-e",
            "--id",
            "Tailscale.Tailscale",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    time.sleep(2)
    return find_tailscale()


def show_connection(view: dict[str, Any], *, show_token: bool, include_steps: bool) -> None:
    print_header()
    print("MCP URL :", view.get("mcpUrl", ""))
    print("Auth    : Bearer token")
    token = view.get("apiKey", "") if show_token else view.get("tokenHint", "")
    print("Token   :", token)
    print()

    if not include_steps:
        return

    if LANGUAGE == "ru":
        print("Что сделать в Notion:")
        print("  1. Откройте агента KaroX Developer -> Settings -> Tools and access.")
        print("  2. Нажмите Add connection или откройте существующее подключение KaroX.")
        print("  3. Выберите Custom MCP и вставьте MCP URL, показанный выше.")
        print("  4. Authentication: Bearer token.")
        print("  5. Prefix: Bearer.")
        print("  6. В поле Token вставьте только ключ, показанный выше, без слова Bearer.")
        print("  7. Нажмите Connect, затем Save.")
        print()
        print("Важно:")
        print("  - Не вставляйте ключ в обычный чат Notion.")
        print("  - Tailscale должен быть запущен и иметь статус Connected.")
        print("  - Во время работы Notion с файлами держите сессию `karox notion` открытой.")
        print()
        print("После сохранения подключения запустите: karox notion")
    else:
        print("What to do in Notion:")
        print("  1. Open KaroX Developer -> Settings -> Tools and access.")
        print("  2. Click Add connection or open the existing KaroX connection.")
        print("  3. Choose Custom MCP and paste the MCP URL shown above.")
        print("  4. Authentication: Bearer token.")
        print("  5. Prefix: Bearer.")
        print("  6. Paste only the key shown above into Token, without the word Bearer.")
        print("  7. Click Connect, then Save.")
        print()
        print("Important:")
        print("  - Never paste the key into a normal Notion chat.")
        print("  - Tailscale must be running and show Connected.")
        print("  - Keep the `karox notion` session open while Notion works with files.")
        print()
        print("After saving the connection, run: karox notion")
    print()


def _save_ready_url(state: dict[str, Any]) -> dict[str, Any]:
    path = profile_path()
    profile = set_url(path, str(state["baseUrl"]))
    return public_view(profile, path, include_key=True)


def setup() -> int:
    ensure_profile(profile_path())
    print_header()
    print(tr(
        "Tailscale must be running to give Notion a permanent URL.",
        "Для постоянного адреса Notion приложение Tailscale должно быть запущено.",
    ))
    print(tr(
        "KaroX will try to start it automatically. Sign in and wait for Connected if the app opens.",
        "KaroX попробует запустить его автоматически. Если откроется приложение, войдите и дождитесь статуса Connected.",
    ))
    print()

    executable = find_tailscale()
    if not executable and os.name == "nt":
        executable = install_tailscale_windows()
    if not executable:
        print(tr(
            "Tailscale was not found. Install it, open it, sign in, wait for Connected, and run: karox notion setup",
            "Tailscale не найден. Установите и запустите его, войдите, дождитесь Connected и снова выполните: karox notion setup",
        ), file=sys.stderr)
        return 1

    start_tailscale_app()
    state = wait_until_ready(5, 1)
    up_exit = 0
    if not state.get("ready"):
        print(tr(
            "Tailscale is not connected yet. Open the Tailscale app and complete sign-in.",
            "Tailscale ещё не подключён. Откройте приложение Tailscale и завершите вход.",
        ))
        auth_url = str(state.get("authUrl", ""))
        if auth_url:
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
        try:
            up_exit = subprocess.run([executable, "up"], check=False).returncode
        except OSError:
            up_exit = 1
        print(tr(
            "Waiting up to 120 seconds for Connected and the stable ts.net hostname...",
            "Жду до 120 секунд, пока появятся статус Connected и постоянный адрес ts.net...",
        ))
        state = wait_until_ready(120, 2)

    if not state.get("ready") or not state.get("baseUrl"):
        details = []
        if state.get("backendState"):
            details.append(f"state={state['backendState']}")
        if state.get("error"):
            details.append(str(state["error"]))
        if up_exit:
            details.append(f"tailscale up exit={up_exit}")
        if state.get("authUrl"):
            details.append(f"login={state['authUrl']}")
        print(tr(
            "Tailscale setup did not finish.",
            "Настройка Tailscale не завершилась.",
        ) + (" " + " | ".join(details) if details else ""), file=sys.stderr)
        print(tr(
            "Start Tailscale manually, sign in, wait until it says Connected, then run: karox notion setup",
            "Запустите Tailscale вручную, войдите, дождитесь статуса Connected и снова выполните: karox notion setup",
        ), file=sys.stderr)
        return 1

    view = _save_ready_url(state)
    show_connection(view, show_token=True, include_steps=True)
    return 0


def ensure_ready() -> int:
    state = query_status()
    if not state.get("ready") or not state.get("baseUrl"):
        return setup()
    _save_ready_url(state)
    return 0


def connection(show_token: bool) -> int:
    profile = ensure_profile(profile_path())
    view = public_view(profile, profile_path(), include_key=show_token)
    show_connection(view, show_token=show_token, include_steps=True)
    return 0


def status() -> int:
    state = query_status()
    if state.get("ready") and state.get("baseUrl"):
        view = _save_ready_url(state)
    else:
        profile = ensure_profile(profile_path())
        view = public_view(profile, profile_path(), include_key=False)
    show_connection(view, show_token=False, include_steps=False)
    print("Tailscale:", state.get("backendState", "") or tr("not running", "не запущен"), "|", state.get("dnsName", ""))
    if not state.get("ready"):
        print(tr(
            "Start Tailscale, sign in, wait for Connected, then run: karox notion setup",
            "Запустите Tailscale, войдите, дождитесь Connected и выполните: karox notion setup",
        ))
    return 0 if state.get("ready") else 1


def rotate() -> int:
    profile = rotate_key(profile_path())
    view = public_view(profile, profile_path(), include_key=True)
    show_connection(view, show_token=True, include_steps=True)
    print(tr(
        "The key changed. Replace it in Notion before reconnecting.",
        "Ключ изменён. Замените его в Notion перед повторным подключением.",
    ))
    return 0


def reset() -> int:
    path = profile_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    print(tr(
        "Persistent Notion connection profile removed.",
        "Профиль постоянного подключения Notion удалён.",
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Localized KaroX Notion setup wizard.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    sub.add_parser("ensure")
    connection_parser = sub.add_parser("connection")
    connection_parser.add_argument("--show-token", action="store_true")
    sub.add_parser("status")
    sub.add_parser("rotate")
    sub.add_parser("reset")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "setup":
        return setup()
    if args.command == "ensure":
        return ensure_ready()
    if args.command == "connection":
        return connection(bool(args.show_token))
    if args.command == "status":
        return status()
    if args.command == "rotate":
        return rotate()
    if args.command == "reset":
        return reset()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
