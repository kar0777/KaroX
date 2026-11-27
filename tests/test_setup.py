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

    choice = asyncio.run(run_setup(read, secret, out.append, tmp_path / "home", lister=_no_list))
    assert choice.model == "deepseek-v4.1-flash-1017"
    saved = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert saved["base_url"] == "https://api.reformboss.com/v1"
    assert "sk-secret-1" not in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert load_key(tmp_path / "home", "reformboss") == "sk-secret-1"
    assert b"sk-secret-1" not in (tmp_path / "home" / "credentials-reformboss").read_bytes()


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


def _talk(answers, key="sk-a"):
    it = iter(answers)
    out: list[str] = []

    async def read(_prompt):
        return next(it)

    async def secret(_prompt):
        return key

    return read, secret, out


async def _two_models(kind, base_url, key):
    assert key  # the key is asked before the list
    return ["glm-5.3-flash", "glm-5.3-pro", "qwen3-coder"]


def test_every_provider_is_kept_with_its_own_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KAROX_CONFIG", str(tmp_path / "config.toml"))
    home = tmp_path / "home"
    read, secret, out = _talk(["7", "https://inference.dahl.global/v1", "glm 5.3", "2"])
    choice = asyncio.run(run_setup(read, secret, out.append, home, lister=_two_models))
    assert (choice.model, choice.name) == ("glm-5.3-pro", "dahl")
    assert "3. qwen3-coder" in "".join(out)

    read, secret, out = _talk(["3", "deepseek-reasoner"], key="sk-b")
    choice = asyncio.run(run_setup(read, secret, out.append, home, lister=_no_list))
    assert choice.model == "deepseek-reasoner"
    text = "".join(out)
    assert "1. dahl: glm-5.3-pro (inference.dahl.global)" in text  # remembered
    assert "no list (not here); type the model's name" in text

    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "deepseek-reasoner"  # the newest is the default
    assert config["saved_key"] == "deepseek"
    assert config["profiles"]["dahl"]["model"] == "glm-5.3-pro"
    assert load_key(home, "dahl") == "sk-a"
    assert load_key(home, "deepseek") == "sk-b"
    assert load_key(home) is None  # no key is shared between providers

    # back to the saved one: no URL and no key typed again
    read, secret, out = _talk(["1", ""], key="never asked")
    choice = asyncio.run(run_setup(read, secret, out.append, home, lister=_two_models))
    assert (choice.base_url, choice.model, choice.key) == (
        "https://inference.dahl.global/v1",
        "glm-5.3-pro",
        "sk-a",
    )
    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert (config["model"], config["saved_key"]) == ("glm-5.3-pro", "dahl")
    assert set(config["profiles"]) == {"dahl", "deepseek"}


async def _no_list(kind, base_url, key):
    from karox6.providers.catalog import CatalogError

    raise CatalogError("not here")


def test_the_saved_key_name_picks_the_key(tmp_path, monkeypatch):
    from karox6 import cli

    monkeypatch.delenv("KAROX_API_KEY", raising=False)
    save_key(tmp_path, "sk-old")
    save_key(tmp_path, "sk-dahl", "dahl")
    args = cli._parser({"saved_key": "dahl"}).parse_args(
        ["run", "t", "--model", "m", "--home", str(tmp_path)]
    )
    assert cli._provider(args).api_key == "sk-dahl"
    args = cli._parser({"saved_key": "nothing-saved"}).parse_args(
        ["run", "t", "--model", "m", "--home", str(tmp_path)]
    )
    assert cli._provider(args).api_key is None  # never another provider's key


def test_switching_providers_replaces_the_old_ones_keys(tmp_path):
    from karox6.settings import save_provider, saved_providers

    path = tmp_path / "config.toml"
    path.write_text('api_key_env = "OLD_KEY"\nmax_steps = 7\n', encoding="utf-8")
    save_provider("a", {"provider": "openai-chat", "base_url": "http://a/v1", "model": "m"}, path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "api_key_env" not in data  # the old provider's variable is not sent to this one
    assert data["max_steps"] == 7
    assert saved_providers(path) == {
        "a": {"provider": "openai-chat", "base_url": "http://a/v1", "model": "m"}
    }
