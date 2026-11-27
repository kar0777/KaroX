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

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

# setting -> accepted types (keys are the argparse destinations)
SETTINGS: dict[str, tuple[type, ...]] = {
    "model": (str,),
    "provider": (str,),
    "base_url": (str,),
    "api_key_env": (str,),
    "saved_key": (str,),  # which key the setup saved for this provider (never the key)
    "context_window": (int,),
    "context_limit": (int,),
    "max_output_tokens": (int,),
    "max_steps": (int,),
    "wrap_up_steps": (int,),
    "deadline_notice": (int,),
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
    "copy_notes": (bool,),
    "finish_gate": (bool,),
    "progress_guard": (bool,),
    "command_wait_cap": (int,),
    "update_check": (bool,),
    "model_catalog": (bool,),
    "update_repo": (str,),
    "update_branch": (str,),
    "theme": (str,),
}
ENV = {"model": "KAROX_MODEL", "provider": "KAROX_PROVIDER", "base_url": "KAROX_BASE_URL"}


class SettingsError(ValueError):
    pass


def default_home() -> Path:
    home = os.environ.get("KAROX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".karox6"


def config_path() -> Path:
    env = os.environ.get("KAROX_CONFIG")
    if env:
        return Path(env).expanduser()
    return default_home() / "config.toml"


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


def save_settings(
    updates: dict[str, Any], path: Path | None = None, *, replace: bool = False
) -> Path:
    """Set top-level keys in the config file (other keys and profiles stay as they are);
    ``replace`` writes ``updates`` as the whole file."""
    path = path or config_path()
    data: dict[str, Any] = {}
    if path.is_file() and not replace:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SettingsError(f"cannot read {path}: {exc}") from exc
    data.update(updates)
    profiles = data.pop("profiles", None)
    lines = [f"{k} = {_toml_value(v)}" for k, v in data.items()]
    if isinstance(profiles, dict):
        for name, table in profiles.items():
            lines += ["", f"[profiles.{_toml_key(name)}]"]
            lines += [f"{k} = {_toml_value(v)}" for k, v in table.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    try:
        old = path.read_text(encoding="utf-8")
    except OSError:
        old = None
    if old == text:
        return path
    if old is not None:  # what the user had (comments included) stays one step back
        path.with_name(path.name + ".bak").write_text(old, encoding="utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# What a saved provider is: switching to one replaces exactly these top-level keys.
PROVIDER_KEYS = ("provider", "base_url", "model", "saved_key", "api_key_env")


def saved_providers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ``[profiles.NAME]`` tables that name a provider (what /setup and /provider list)."""
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {
        name: table
        for name, table in profiles.items()
        if isinstance(table, dict) and isinstance(table.get("model"), str) and table.get("model")
    }


def save_provider(name: str, values: dict[str, Any], path: Path | None = None) -> Path:
    """Keep ``values`` as ``[profiles.NAME]`` and make it the default (the top-level keys):
    the next start uses it, and the other saved providers stay for /provider."""
    path = path or config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SettingsError(f"cannot read {path}: {exc}") from exc
    values = {k: v for k, v in values.items() if k in PROVIDER_KEYS and v not in (None, "")}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    old = profiles.get(name)
    table = dict(old) if isinstance(old, dict) else {}
    for key in PROVIDER_KEYS:
        table.pop(key, None)
    table.update(values)
    profiles[name] = table
    for key in PROVIDER_KEYS:
        data.pop(key, None)
    data.pop("profiles", None)
    data = {**values, **data, "profiles": profiles}
    return save_settings(data, path, replace=True)


def _toml_key(name: str) -> str:
    """A bare TOML key when it can be one, else a quoted one ("работа", "my.profile")."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    import json

    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a TOML basic string
