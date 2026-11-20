"""First-run setup inside the chat: pick a provider, a model and paste the API key.

The choice goes to config.toml (provider, base_url, model) and the key to the protected
credentials file, so next time `karox6` alone opens the chat.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from karox6.credentials import save_key
from karox6.settings import save_settings

ReadLine = Callable[[str], Awaitable[str]]
Write = Callable[[str], None]


@dataclass(frozen=True)
class Preset:
    label: str
    provider: str
    base_url: str
    model: str  # "" = ask


PRESETS = (
    Preset(
        "ReformBoss - DeepSeek V4.1 Flash",
        "openai-chat",
        "https://api.reformboss.com/v1",
        "deepseek-v4.1-flash-1017",
    ),
    Preset(
        "DeepSeek (official API)", "openai-chat", "https://api.deepseek.com/v1", "deepseek-chat"
    ),
    Preset("OpenAI", "openai-chat", "https://api.openai.com/v1", ""),
    Preset("Anthropic", "anthropic", "https://api.anthropic.com", ""),
    Preset("Google Gemini", "gemini", "https://generativelanguage.googleapis.com", ""),
    Preset("Other OpenAI-compatible server", "openai-chat", "", ""),
)


@dataclass
class Choice:
    provider: str
    base_url: str
    model: str
    key: str | None


async def run_setup(
    read_line: ReadLine, read_secret: ReadLine, write: Write, home: Path
) -> Choice | None:
    """Ask, save and return the choice; None when the user leaves with an empty answer."""
    write("\nSet up KaroX: choose where the model runs.\n")
    for i, p in enumerate(PRESETS, 1):
        write(f"  {i}. {p.label}\n")
    answer = (await read_line("Number [1]: ")).strip() or "1"
    if not answer.isdigit() or not 1 <= int(answer) <= len(PRESETS):
        write("setup cancelled\n")
        return None
    preset = PRESETS[int(answer) - 1]
    base_url = preset.base_url
    if not base_url:
        base_url = (await read_line("Server URL (e.g. http://127.0.0.1:11434/v1): ")).strip()
        if not base_url:
            write("setup cancelled\n")
            return None
    model = preset.model
    typed = (await read_line(f"Model [{model}]: " if model else "Model: ")).strip()
    model = typed or model
    if not model:
        write("setup cancelled\n")
        return None
    key = (await read_secret("API key (hidden; Enter to skip): ")).strip() or None
    save_settings({"provider": preset.provider, "base_url": base_url, "model": model})
    if key:
        where = save_key(home, key)
        write(f"key saved (encrypted for your user) in {where}\n")
    write(f"saved: {preset.label}, model {model}. Next time just run: karox6\n\n")
    return Choice(preset.provider, base_url, model, key)
