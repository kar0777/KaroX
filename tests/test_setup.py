import asyncio
import tomllib

from karox6.credentials import load_key, save_key
from karox6.settings import save_settings
from karox6.setup import run_setup


def test_setup_saves_choice_and_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KAROX_CONFIG", str(tmp_path / "config.toml"))
    answers = iter(["1", ""])  # ReformBoss preset, default model
    out: list[str] = []

    async def read(_prompt):
        return next(answers)

    async def secret(_prompt):
        return "sk-secret-1"

    choice = asyncio.run(run_setup(read, secret, out.append, tmp_path / "home"))
    assert choice.model == "deepseek-v4.1-flash-1017"
    saved = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert saved["base_url"] == "https://api.reformboss.com/v1"
    assert "sk-secret-1" not in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert load_key(tmp_path / "home") == "sk-secret-1"
    assert b"sk-secret-1" not in (tmp_path / "home" / "credentials").read_bytes()


def test_save_settings_keeps_other_keys_and_profiles(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('max_steps = 5\n\n[profiles.local]\nmodel = "q"\n', encoding="utf-8")
    save_settings({"model": "m", "base_url": 'http://x/"v1"'}, path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["max_steps"] == 5
    assert data["model"] == "m"
    assert data["base_url"] == 'http://x/"v1"'
    assert data["profiles"]["local"]["model"] == "q"


def test_saved_key_is_used_when_no_variable_holds_one(tmp_path, monkeypatch):
    from karox6 import cli

    monkeypatch.delenv("KAROX_API_KEY", raising=False)
    save_key(tmp_path, "sk-from-setup")
    args = cli._parser().parse_args(["run", "t", "--model", "m", "--home", str(tmp_path)])
    assert cli._provider(args).api_key == "sk-from-setup"
    monkeypatch.setenv("KAROX_API_KEY", "sk-env")
    args = cli._parser().parse_args(["run", "t", "--model", "m", "--home", str(tmp_path)])
    assert cli._provider(args).api_key == "sk-env"  # the environment wins
