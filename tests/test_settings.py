import pytest

from karox6 import cli
from karox6.settings import SettingsError, load_settings, requested_profile

CONFIG = """
model = "base-model"
base_url = "https://example.test/v1"
api_key_env = "MY_KEY"
context_window = 64000
subagents = true

[profiles.local]
model = "local-model"
base_url = "http://127.0.0.1:11434/v1"
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KAROX_HOME", str(tmp_path))
    for env in ("KAROX_MODEL", "KAROX_PROVIDER", "KAROX_BASE_URL", "KAROX_CONFIG"):
        monkeypatch.delenv(env, raising=False)
    return tmp_path


def parse(argv):
    settings = load_settings(requested_profile(argv))
    return cli._parser(settings).parse_args(argv)


def test_defaults_from_file(home):
    (home / "config.toml").write_text(CONFIG)
    args = parse(["run", "task"])
    assert args.model == "base-model"
    assert args.base_url == "https://example.test/v1"
    assert args.api_key_env == "MY_KEY"
    assert args.context_window == 64000
    assert args.subagents is True
    assert args.provider is None  # not in the file: main() picks it from the model


def test_flag_and_env_beat_file(home, monkeypatch):
    (home / "config.toml").write_text(CONFIG)
    assert parse(["chat", "--model", "flag-model"]).model == "flag-model"
    monkeypatch.setenv("KAROX_MODEL", "env-model")
    args = parse(["chat"])
    assert args.model == "env-model"
    assert args.base_url == "https://example.test/v1"


def test_profile_overlay(home):
    (home / "config.toml").write_text(CONFIG)
    args = parse(["mcp", "--profile", "local"])
    assert args.model == "local-model"
    assert args.base_url == "http://127.0.0.1:11434/v1"
    assert args.api_key_env == "MY_KEY"  # inherited from the top level
    assert parse(["run", "--profile=local", "x"]).model == "local-model"


def test_no_file_is_fine(home):
    assert load_settings() == {}
    assert parse(["run", "x"]).model is None


@pytest.mark.parametrize(
    ("text", "profile", "message"),
    [
        ('modle = "x"\n', None, "unknown setting modle"),
        ("context_window = 'big'\n", None, "must be int"),
        ("subagents = 1\n", None, "must be bool"),
        ("model = \n", None, "cannot read"),
        ('model = "x"\n', "nope", "no [profiles.nope]"),
    ],
)
def test_errors(home, text, profile, message):
    (home / "config.toml").write_text(text)
    with pytest.raises(SettingsError, match=message.replace("[", r"\[")):
        load_settings(profile)


def test_cli_reports_bad_config(home, capsys):
    (home / "config.toml").write_text('modle = "x"\n')
    assert cli.main(["run", "x"]) == 2
    assert "unknown setting modle" in capsys.readouterr().err
