#!/usr/bin/env python3
"""Reject stale user-facing KaroX product and connector instructions.

Compatibility identifiers such as the ``karox-vnext`` launcher and versioned
storage directories may remain during migration. Human-facing CLI help and
hosted-client setup instructions must use KaroX 5 and current product UI paths.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = {
    "src/karox/web_bridge_launcher.py": (
        "Settings → Plugins",
        "Настройки → Плагины",
        "Sign in with KaroX",
        "Войти через KaroX",
    ),
    "tests/test_web_bridge_launcher.py": (
        "Настройки → Плагины",
        'self.assertIn("Войти через KaroX", text)',
    ),
    "src/karox/cli.py": (
        "KaroX vNext foundation",
        "manage durable vNext sessions",
        "aggregate vNext diagnostics",
    ),
}

REQUIRED_AFTER_FIX = {
    "src/karox/web_bridge_launcher.py": (
        "Settings → Apps",
        "Apps → Create",
        "Настройки → Приложения",
        "Приложения → Создать",
        "Settings → Connectors → Add custom connector",
    ),
    "tests/test_web_bridge_launcher.py": (
        "Settings → Apps",
        "Apps → Create",
        "Settings → Connectors → Add custom connector",
    ),
    "src/karox/cli.py": (
        "KaroX 5 hybrid runtime",
        "manage durable KaroX sessions",
        "aggregate KaroX diagnostics",
    ),
}


def collect_problems(root: Path = ROOT) -> list[str]:
    problems: list[str] = []

    for relative, forbidden_values in FORBIDDEN.items():
        path = root / relative
        if not path.is_file():
            problems.append(f"user-facing copy source is missing: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for value in forbidden_values:
            if value in text:
                problems.append(f"{relative} still contains stale copy {value!r}")

    for relative, required_values in REQUIRED_AFTER_FIX.items():
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for value in required_values:
            if value not in text:
                problems.append(f"{relative} is missing current copy {value!r}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    problems = collect_problems(ROOT)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    elif problems:
        print("user-facing KaroX copy contract failed:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("user-facing KaroX 5 and connector instructions are current")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
