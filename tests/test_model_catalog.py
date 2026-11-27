"""The open model catalog (models.dev digest): pictures and windows per server and name."""

import json

from karox6 import model_catalog as mc
from karox6.models import model_info

CATALOG = {
    "deepseek": {
        "api": "https://api.deepseek.com",
        "models": {
            "deepseek-flash": {
                "modalities": {"input": ["text", "image"]},
                "limit": {"context": 1048576},
            },
            "deepseek-v4-flash": {
                "modalities": {"input": ["text", "image"]},
                "limit": {"context": 1048576},
            },
            "deepseek-v4-pro": {"modalities": {"input": ["text"]}, "limit": {"context": 1048576}},
        },
    },
    "openrouter": {
        "api": "https://openrouter.ai/api/v1",
        "models": {
            # the same name on another route without pictures
            "deepseek/deepseek-v4-flash": {"modalities": {"input": ["text"]}},
            "deepseek/deepseek-v4.1-flash-1017": {
                "modalities": {"input": ["text", "image"]},
                "limit": {"context": 262144},
            },
        },
    },
    "mistral": {
        "api": "https://api.mistral.ai/v1",
        "models": {
            "deepseek-v4-1-flash-1017": {
                "modalities": {"input": ["text", "image"]},
                "limit": {"context": 1048576},
            },
            "odd": {"modalities": "text", "limit": {"context": True}},
        },
    },
    "openai": {  # no API address: the known host
        "models": {
            "gpt-5": {"modalities": {"input": ["text", "image"]}, "limit": {"input": 272000}}
        }
    },
    "broken": "not a provider",
}


def _home(tmp_path, catalog=CATALOG):
    assert mc.refresh(tmp_path, download=lambda url: json.dumps(catalog).encode(), now=1000.0)
    return tmp_path


def test_name_key():
    assert mc.name_key("openrouter/deepseek/deepseek-v4.1-flash:free") == "deepseek-v4-1-flash"
    assert mc.name_key("DeepSeek_V4") == "deepseek-v4"


def test_the_server_answers_first_then_the_routes_that_agree(tmp_path):
    home = _home(tmp_path)
    assert mc.lookup(home, "deepseek-v4-flash", "https://api.deepseek.com/v1") == {
        "vision": True,
        "context_window": 1048576,
    }
    on_router = mc.lookup(home, "deepseek/deepseek-v4-flash", "https://openrouter.ai/api/v1")
    assert on_router == {"vision": False}
    # elsewhere the routes disagree about pictures: unknown (the window is still known)
    assert mc.lookup(home, "deepseek-v4-flash", "https://other.test/v1") == {
        "context_window": 1048576
    }
    # "no pictures" only from the server itself, never from other routes' entries
    assert mc.lookup(home, "deepseek-v4-pro", "https://other.test/v1") == {
        "context_window": 1048576
    }
    # a name every route agrees on is known on any server, spelt either way
    for name in ("deepseek-v4-1-flash-1017", "deepseek-v4.1-flash-1017"):
        assert mc.lookup(home, name, "https://gateway.test/v1")["vision"] is True
    assert mc.lookup(home, "gpt-5", "https://api.openai.com/v1") == {
        "vision": True,
        "context_window": 272000,
    }
    assert mc.lookup(home, "no-such-model", "https://api.deepseek.com") == {}
    assert mc.lookup(home, "odd", "https://api.mistral.ai/v1") == {}


def test_model_info_uses_the_catalog_below_what_the_user_said(tmp_path):
    home = _home(tmp_path)
    info = model_info("deepseek-v4-pro", home, "https://api.deepseek.com")
    assert (info.vision, info.source) == (False, "catalog")
    (home / "models.json").write_text(json.dumps({"models": {"deepseek-v4-pro": {"vision": True}}}))
    assert model_info("deepseek-v4-pro", home, "https://api.deepseek.com").vision is True


def test_downloaded_once_a_day_and_offline_is_not_retried_every_start(tmp_path):
    calls = []

    def download(url):
        calls.append(url)
        return json.dumps(CATALOG).encode()

    assert mc.refresh(tmp_path, download=download, now=1000.0)
    assert not mc.refresh(tmp_path, download=download, now=1000.0 + 3600)
    assert calls == [mc.URL]

    def offline(url):
        calls.append(url)
        raise OSError("offline")

    day = 1000.0 + mc.REFRESH_S + 1
    assert not mc.refresh(tmp_path, download=offline, now=day)
    assert mc.lookup(tmp_path, "deepseek-flash", "https://api.deepseek.com")  # old digest kept
    assert not mc.refresh(tmp_path, download=offline, now=day + 60)  # not again so soon
    assert len(calls) == 2
    assert mc.refresh(tmp_path, download=download, now=day + mc.RETRY_S + 1)


def test_a_bad_answer_is_not_saved(tmp_path):
    assert not mc.refresh(tmp_path, download=lambda url: b"[1, 2]", now=5.0)
    assert not mc.refresh(tmp_path, download=lambda url: b"not json", now=5.0 + mc.RETRY_S + 1)
    assert mc.lookup(tmp_path, "deepseek-flash", "https://api.deepseek.com") == {}
    (tmp_path / mc.FILE_NAME).write_text("{broken")
    assert mc.due(tmp_path)
    assert mc.lookup(tmp_path, "x") == {}


def test_nearly_all_routes_saying_pictures_is_enough(tmp_path):
    routes = {
        f"host{i}": {
            "api": f"https://h{i}.test/v1",
            "models": {"m-1": {"modalities": {"input": ["text", "image"] if i else ["text"]}}},
        }
        for i in range(6)
    }
    home = _home(tmp_path, routes)
    assert mc.lookup(home, "m-1", "https://elsewhere.test/v1") == {"vision": True}
    assert mc.lookup(home, "m-1", "https://h0.test/v1") == {"vision": False}  # its own entry


def test_two_providers_on_one_host_both_vote(tmp_path):
    text, image = {"input": ["text"]}, {"input": ["text", "image"]}
    routes = {
        "a": {"api": "https://same.test/v1", "models": {"m-2": {"modalities": image}}},
        "b": {"api": "https://same.test/v2", "models": {"m-2": {"modalities": text}}},
        "c": {"api": "https://other.test/v1", "models": {"m-2": {"modalities": image}}},
    }
    home = _home(tmp_path, routes)
    assert mc.lookup(home, "m-2", "https://same.test/v1") == {}  # its providers disagree
    assert mc.lookup(home, "m-2", "https://elsewhere.test") == {}  # 2 of 3 is not enough
