"""Setup inside the chat: pick a provider (a saved one or a new one), a model and the key.

Every provider set up here is kept as ``[profiles.NAME]`` in config.toml (provider,
base_url, model, the name of its saved key) and becomes the default, so `karox6` alone
opens the chat with it, and `/provider` or `/setup` switch back to an earlier one without
typing anything again. Each provider's key goes to its own protected credentials file. The
model is picked from the provider's own list when the server has one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from karox6.credentials import load_key, save_key
from karox6.providers.catalog import CatalogError, chat_models, columns, list_models, pick
from karox6.settings import save_provider, saved_providers

ReadLine = Callable[[str], Awaitable[str]]
Write = Callable[[str], None]
Lister = Callable[[str, str, str | None], Awaitable[list[str]]]
Picker = Callable[[str, list[str], str | None], Awaitable[str | None]]

SHOW_ALL = 300  # a list this long is printed whole; a longer one is filtered by typing


@dataclass(frozen=True)
class Preset:
    label: str
    name: str  # the saved provider's name ("" = from the URL)
    provider: str
    base_url: str
    model: str  # "" = ask


PRESETS = (
    Preset(
        "ReformBoss - DeepSeek V4.1 Flash",
        "reformboss",
        "openai-chat",
        "https://api.reformboss.com/v1",
        "deepseek-v4.1-flash-1017",
    ),
    Preset(
        "DeepSeek (official API)",
        "deepseek",
        "openai-chat",
        "https://api.deepseek.com/v1",
        "deepseek-chat",
    ),
    Preset("OpenAI", "openai", "openai-chat", "https://api.openai.com/v1", ""),
    Preset("OpenRouter", "openrouter", "openai-chat", "https://openrouter.ai/api/v1", ""),
    Preset("Mistral", "mistral", "openai-chat", "https://api.mistral.ai/v1", ""),
    Preset("Anthropic", "anthropic", "anthropic", "https://api.anthropic.com", ""),
    Preset("Google Gemini", "gemini", "gemini", "https://generativelanguage.googleapis.com", ""),
    Preset("Other OpenAI-compatible server", "", "openai-chat", "", ""),
)


@dataclass(frozen=True)
class _Talk:
    """How the setup asks and answers."""

    read_line: ReadLine
    read_secret: ReadLine
    write: Write
    home: Path
    lister: Lister | None
    picker: Picker | None = None  # the full-screen list picker, when there is one


@dataclass
class Choice:
    provider: str
    base_url: str
    model: str
    key: str | None
    name: str = ""  # the saved key's name (credentials-<name>); "" = the unnamed one


async def run_setup(
    read_line: ReadLine,
    read_secret: ReadLine,
    write: Write,
    home: Path,
    *,
    lister: Lister | None = None,
    picker: Picker | None = None,
) -> Choice | None:
    """Ask, save and return the choice; None when the user leaves with an empty answer."""
    saved = saved_providers()
    entries: list[tuple[str, Any]] = []
    labels: list[str] = []
    for name, table in saved.items():
        entries.append(("saved", name))
        labels.append(f"{name}: {table['model']} ({_host(table)})")
    for p in PRESETS:
        entries.append(("preset", p))
        labels.append(f"+ {p.label}")
    if picker is not None:
        picked = await picker("Provider", labels, labels[0] if saved else None)
        if picked is None or picked not in labels:
            write("setup cancelled\n")
            return None
        kind, item = entries[labels.index(picked)]
    else:
        write("\nSet up KaroX: choose where the model runs.\n")
        if saved:
            write("Saved:\n")
        for i, label in enumerate(labels, 1):
            if saved and i == len(saved) + 1:
                write("New:\n")
            write(f"  {i}. {label.removeprefix('+ ')}\n")
        answer = (await read_line("Number [1]: ")).strip() or "1"
        if not answer.isdigit() or not 1 <= int(answer) <= len(entries):
            write("setup cancelled\n")
            return None
        kind, item = entries[int(answer) - 1]
    talk = _Talk(read_line, read_secret, write, home, lister, picker)
    if kind == "saved":
        return await _saved(talk, item, saved[item])
    return await _new(talk, item)


async def _saved(talk: _Talk, name: str, table: dict[str, Any]) -> Choice | None:
    home, write = talk.home, talk.write
    provider = str(table.get("provider") or "openai-chat")
    base_url = str(table.get("base_url") or "")
    # A provider saved by an older setup has no key name: its key is the unnamed one.
    key_name = str(table.get("saved_key") or "")
    key = load_key(home, key_name)
    if key is None and not table.get("api_key_env"):
        key = (await talk.read_secret("API key (hidden; Enter to skip): ")).strip() or None
        if key:
            key_name = key_name or name
            save_key(home, key, key_name)
    model = await _model(talk, provider, base_url, key, str(table["model"]))
    if model is None:
        return None
    save_provider(name, {**table, "model": model, "saved_key": key_name})
    write(f"saved: {name}, model {model}. Next time just run: karox6\n\n")
    return Choice(provider, base_url, model, key, key_name)


async def _new(talk: _Talk, preset: Preset) -> Choice | None:
    home, write = talk.home, talk.write
    base_url = preset.base_url
    if not base_url:
        base_url = (await talk.read_line("Server URL (e.g. http://127.0.0.1:11434/v1): ")).strip()
        if not base_url:
            write("setup cancelled\n")
            return None
    name = preset.name
    if not name:  # the user's own server: they say what to call it
        guess = _name_for(base_url)
        typed = (await talk.read_line(f"Name for this provider [{guess}]: ")).strip()
        name = _clean_name(typed) or guess
    old = load_key(home, name)
    ask = "API key (hidden; Enter keeps the saved one): "
    if not old:
        ask = "API key (hidden; Enter to skip): "
    key = (await talk.read_secret(ask)).strip() or old
    model = await _model(talk, preset.provider, base_url, key, preset.model)
    if model is None:
        return None
    if key and key != old:
        where = save_key(home, key, name)
        write(f"key saved (encrypted for your user) in {where}\n")
    # Always its own key name, also with no key yet: the unnamed key (another provider's,
    # from an older setup) must never be sent here.
    values = {"provider": preset.provider, "base_url": base_url, "model": model}
    save_provider(name, {**values, "saved_key": name})
    write(f"saved: {name}, model {model}. Next time just run: karox6\n\n")
    return Choice(preset.provider, base_url, model, key, name)


async def _model(
    talk: _Talk, provider: str, base_url: str, key: str | None, default: str
) -> str | None:
    """The model: picked from the provider's list when it has one, else typed."""
    write = talk.write
    models: list[str] = []
    write("asking the provider for its models...\n")
    try:
        models = await (talk.lister or list_models)(provider, base_url, key)
    except CatalogError as exc:
        write(f"  no list ({exc}); type the model's name\n")
    if models:
        chat = chat_models(models) or models
        hidden = len(models) - len(chat)
        models = chat
        if talk.picker is not None:
            picked = await talk.picker("Model", models, default or None)
            if picked is None:
                write("setup cancelled\n")
            return picked
        _show(write, models, default)
        note = f" ({hidden} embedding/speech/OCR models not shown)" if hidden else ""
        write(f"  {len(models)} models{note}: a number, a name or part of one\n")
    for _ in range(10):
        prompt = f"Model [{default}]: " if default else "Model: "
        typed = (await talk.read_line(prompt)).strip()
        if not typed:
            if default:
                return default
            write("setup cancelled\n")
            return None
        if not models:
            return typed
        chosen, matches = pick(models, typed)
        if chosen:
            return chosen
        if not matches:
            write(f"  {typed} is not in the provider's list; using it as typed\n")
            return typed
        if len(matches) == 1:
            return matches[0]
        models = matches
        _show(write, models, default)
    write("setup cancelled\n")
    return None


def _show(write: Write, models: list[str], default: str) -> None:
    for line in columns(models[:SHOW_ALL], mark=default):
        write(line + "\n")
    if len(models) > SHOW_ALL:
        write(f"  ... {len(models) - SHOW_ALL} more; type part of a name\n")


def _clean_name(text: str) -> str:
    """A provider name that works as a profile and a file name: letters, digits, - and _."""
    return "-".join("".join(c if c.isalnum() or c in "-_" else " " for c in text).split()).lower()


def _host(table: dict[str, Any]) -> str:
    url = str(table.get("base_url") or "")
    return urlsplit(url).netloc or url or str(table.get("provider") or "")


def _name_for(base_url: str) -> str:
    """A saved provider's name from its URL: inference.dahl.global -> dahl,
    http://localhost:11434/v1 -> localhost-11434."""
    parts = urlsplit(base_url if "//" in base_url else f"//{base_url}")
    host = (parts.hostname or "server").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return f"localhost-{parts.port}" if parts.port else "localhost"
    labels = [x for x in host.split(".") if x not in ("api", "www", "inference")]
    if not labels:
        return "server"
    return labels[-2] if len(labels) >= 2 else labels[0]
