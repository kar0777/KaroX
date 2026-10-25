"""The small slice of persisted UI preference the compact client owns.

It intentionally reads and writes the same ``vnext/ui.json`` the classic shell
uses, so switching between the two never resets Effort, Mode or language.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..paths import config_dir


def preferences_path() -> Path:
    return config_dir() / "vnext" / "ui.json"


def load_preferences() -> dict[str, Any]:
    try:
        value = json.loads(preferences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preferences(**changes: object) -> None:
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = load_preferences()
    value.update(changes)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)