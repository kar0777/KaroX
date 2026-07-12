#!/usr/bin/env python3
"""Prepare the built-in KaroX Notion provider without requiring CLI setup commands."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from typing import Any

from notion_profile import ensure_profile, profile_path, set_url
from notion_setup_wizard import (
    install_tailscale_windows,
    selected_language,
    start_tailscale_app,
)
from tailscale_readiness import find_tailscale, wait_until_ready

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


LANGUAGE = selected_language()


def tr(en: str, ru: str) -> str:
    return ru if LANGUAGE == "ru" else en


def _details(state: dict[str, Any], up_exit: int) -> str:
    parts: list[str] = []
    if state.get("backendState"):
        parts.append(f"state={state['backendState']}")
    if state.get("error"):
        parts.append(str(state["error"]))
    if up_exit:
        parts.append(f"tailscale up exit={up_exit}")
    if state.get("authUrl"):
        parts.append(f"login={state['authUrl']}")
    return " | ".join(parts)


def prepare() -> int:
    ensure_profile(profile_path())
    print()
    print(tr("Built-in Notion provider", "Встроенный провайдер Notion"))
    print("-" * 48)
    print(tr(
        "KaroX is preparing Tailscale Funnel automatically.",
        "KaroX автоматически подготавливает Tailscale Funnel.",
    ))

    executable = find_tailscale()
    if not executable and os.name == "nt":
        executable = install_tailscale_windows()
    if not executable:
        print(tr(
            "Tailscale is required for the stable Notion address but was not found.",
            "Для постоянного адреса Notion нужен Tailscale, но он не найден.",
        ), file=sys.stderr)
        print(tr(
            "Install Tailscale, open it once, sign in, then choose Notion in KaroX again.",
            "Установите Tailscale, один раз откройте его, войдите и снова выберите Notion в KaroX.",
        ), file=sys.stderr)
        return 1

    start_tailscale_app()
    state = wait_until_ready(6, 1)
    up_exit = 0

    if not state.get("ready"):
        print(tr(
            "Tailscale is not connected yet. Complete sign-in in the opened app or browser.",
            "Tailscale ещё не подключён. Завершите вход в открывшемся приложении или браузере.",
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
            "Waiting for Connected and the permanent ts.net address...",
            "Жду статус Connected и постоянный адрес ts.net...",
        ))
        state = wait_until_ready(120, 2)

    if not state.get("ready") or not state.get("baseUrl"):
        details = _details(state, up_exit)
        print(tr(
            "The built-in Notion provider could not start.",
            "Не удалось запустить встроенный провайдер Notion.",
        ) + ((" " + details) if details else ""), file=sys.stderr)
        print(tr(
            "Open Tailscale, sign in, wait for Connected, then return to KaroX and choose Notion again.",
            "Откройте Tailscale, войдите, дождитесь Connected, затем вернитесь в KaroX и снова выберите Notion.",
        ), file=sys.stderr)
        return 1

    set_url(profile_path(), str(state["baseUrl"]))
    print(tr(
        "Notion provider is ready. KaroX will now start the MCP session.",
        "Провайдер Notion готов. Сейчас KaroX запустит MCP-сессию.",
    ))
    time.sleep(0.3)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare the native KaroX Notion provider.")
    parser.add_argument("command", choices=("prepare",), nargs="?", default="prepare")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        return prepare()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
