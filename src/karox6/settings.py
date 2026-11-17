"""User settings: defaults for the command-line options, from ``<KAROX_HOME>/config.toml``.

Precedence: command-line flag > environment variable (``KAROX_MODEL``, ``KAROX_PROVIDER``,
``KAROX_BASE_URL``) > this file > built-in default. Top-level keys are the defaults;
``[profiles.NAME]`` tables overlay them when ``--profile NAME`` is given::

    model = "deepseek-chat"
    provider = "openai-chat"
    base_url = "https://api.deepseek.com/v1"
    api_key_env = "DEEPSEEK_API_KEY"   # the name of the variable, never the key

    [profiles.local]
    model = "qwen3-coder"
    base_url = "http://127.0.0.1:11434/v1"
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

# setting -> accepted types (keys are the argparse destinations)
SETTINGS: dict[str, tuple[type, ...]] = {
    "model": (str,),
    "provider": (str,),
    "base_url": (str,),
    "api_key_env": (str,),
    "context_window": (int,),
    "context_limit": (int,),
    "max_output_tokens": (int,),
    "max_steps": (int,),
    "wrap_up_steps": (int,),
    "max_seconds": (int, float),
    "max_cost": (int, float),
    "reasoning_effort": (str,),
    "temperature": (int, float),
    "price_input": (int, float),
    "price_output": (int, float),
    "price_cached": (int, float),
    "request_timeout": (int, float),
    "replay_reasoning": (str,),
    "apply_patch": (bool,),
    "subagents": (bool,),
    "code_mode": (bool,),
}
ENV = {"model": "KAROX_MODEL", "provider": "KAROX_PROVIDER", "base_url": "KAROX_BASE_URL"}


class SettingsError(ValueError):
    pass


def config_path() -> Path:
    env = os.environ.get("KAROX_CONFIG")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("KAROX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".karox6") / "config.toml"


def load_settings(profile: str | None = None, path: Path | None = None) -> dict[str, Any]:
    """Validated defaults from the config file (empty when there is none)."""
    path = path or config_path()
    if not path.is_file():
        if profile:
            raise SettingsError(f"profile {profile!r} requested but {path} does not exist")
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    profiles = data.pop("profiles", {})
    values = _validated(data, path)
    if profile:
        if not isinstance(profiles, dict) or not isinstance(profiles.get(profile), dict):
            raise SettingsError(f"no [profiles.{profile}] in {path}")
        values.update(_validated(profiles[profile], path, f"profiles.{profile}."))
    for key, env in ENV.items():
        if os.environ.get(env):
            values.pop(key, None)  # the environment wins over the file
    return values


def _validated(data: dict[str, Any], path: Path, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        types = SETTINGS.get(key)
        if types is None:
            known = ", ".join(sorted(SETTINGS))
            raise SettingsError(f"unknown setting {prefix}{key} in {path} (known: {known})")
        if (isinstance(value, bool) and bool not in types) or not isinstance(value, types):
            raise SettingsError(f"{prefix}{key} in {path} must be {types[0].__name__}")
        out[key] = value
    return out


def requested_profile(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1]
    return None
