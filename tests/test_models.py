"""What KaroX knows about a model: built-in facts, the user's models.json, learned facts."""

import asyncio
import json

import pytest

from karox6.core.session import Session, SessionConfig
from karox6.models import model_info, rejects_images, remember
from karox6.providers.base import ProviderError
from karox6.providers.scripted import ScriptedProvider, Turn


@pytest.mark.parametrize(
    ("model", "vision", "window"),
    [
        ("claude-haiku-5-5", True, None),
        ("claude-sonnet-4-6", True, None),
        ("claude-sonnet-4-5-20250929", True, 200_000),
        ("claude-3-7-sonnet-latest", True, 200_000),
        ("claude-haiku-4-5", True, 200_000),
        ("anthropic/claude-opus-5", True, None),
        ("gpt-5.2", True, None),
        ("gpt-oss-120b", False, None),
        ("gemini-3-pro", True, None),
        ("deepseek-v4.1-flash", False, None),
        ("deepseek/deepseek-chat:free", False, None),
        ("Qwen/Qwen3-VL-235B", True, None),
        ("zai-org/GLM-5.3-Flash", None, None),
        ("some-local-model", None, None),
    ],
)
def test_built_in_facts(model, vision, window):
    info = model_info(model)
    assert info.vision is vision
    assert info.context_window == window


def test_the_users_file_and_what_was_learned(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps(
            {
                "models": {
                    "zai-org/GLM-5.3-Flash": {"context_window": 200_000},
                    "my-local-*": {"vision": True, "context_window": "big"},
                    "deepseek-v4.1-flash": {"vision": True},
                }
            }
        )
    )
    glm = model_info("zai-org/GLM-5.3-Flash", tmp_path)
    assert (glm.vision, glm.context_window, glm.source) == (None, 200_000, "user")
    local = model_info("my-local-7b", tmp_path)
    assert (local.vision, local.context_window) == (True, None)  # a bad value is ignored
    assert model_info("deepseek-v4.1-flash", tmp_path).vision is True  # the user wins

    remember(tmp_path, "zai-org/GLM-5.3-Flash", vision=False)
    glm = model_info("zai-org/GLM-5.3-Flash", tmp_path)
    assert (glm.vision, glm.context_window) == (False, 200_000)
    data = json.loads((tmp_path / "models.json").read_text())
    assert data["learned"] == {"zai-org/GLM-5.3-Flash": {"vision": False}}
    assert "my-local-*" in data["models"]  # the user's part is kept as it was
    assert "pictures: no" in glm.describe()
    assert "200k" in glm.describe()


def test_rejects_images():
    assert rejects_images("bad_request", "Model does not support image input")
    assert rejects_images("bad_request", "Invalid content type: image_url")
    assert not rejects_images("bad_request", "max_tokens too large")
    assert not rejects_images("rate_limit", "image quota")


def _picture(tmp_path):
    from karox6.tools.screen import encode_png

    path = tmp_path / "shot.png"
    path.write_bytes(encode_png(2, 2, bytes([1, 2, 3]) * 4))
    return [{"path": str(path), "media_type": "image/png"}]


def _run(session, prompt, images):
    async def go():
        return [e async for e in session.run(prompt, images=images)]

    return asyncio.run(go())


def test_a_model_without_vision_gets_a_note_instead_of_the_picture(tmp_path):
    provider = ScriptedProvider([Turn(text="ok")])
    config = SessionConfig(model="deepseek-v4.1-flash", workspace=tmp_path, home=tmp_path / "h")
    session = Session(config, provider)
    _run(session, "what is this?", _picture(tmp_path))
    sent = provider.requests[0].messages[-1]
    assert sent.images == []
    assert "shot.png" in sent.content
    assert "takes no pictures" in sent.content
    assert session.messages[0].images  # the stored conversation keeps the picture


def test_a_refused_picture_is_learned_and_the_request_sent_again(tmp_path):
    refusal = ProviderError("bad_request", "this model does not support image input", status=400)
    provider = ScriptedProvider([Turn(error=refusal), Turn(text="a cat, I guess")])
    home = tmp_path / "h"
    config = SessionConfig(model="zai-org/GLM-5.3-Flash", workspace=tmp_path, home=home)
    session = Session(config, provider)
    events = _run(session, "what is this?", _picture(tmp_path))
    assert events[-1].status == "completed"
    assert provider.requests[0].messages[-1].images
    assert provider.requests[1].messages[-1].images == []
    assert any(getattr(e, "kind", "") == "no_images" for e in events)
    assert model_info("zai-org/GLM-5.3-Flash", home).vision is False
    assert session.model().source == "learned"

    # a refusal that is not about pictures stays an error
    other = ProviderError("bad_request", "the image field is wrong", status=400)
    config2 = SessionConfig(model="claude-haiku-5-5", workspace=tmp_path, home=home)
    session2 = Session(config2, ScriptedProvider([Turn(error=other)]))
    assert _run(session2, "hi", None)[-1].status == "error"  # no pictures were sent


def _picture_tool(tmp_path):
    from karox6.tools.base import ToolResult

    shot = _picture(tmp_path)[0]["path"]

    class Shot:
        name, description, parameters, read_only = "shot", "take a picture", {}, True

        async def run(self, args, ctx):
            return ToolResult("took a picture", images=[__import__("pathlib").Path(shot)])

    return Shot()


@pytest.mark.parametrize(
    ("model", "shown"),
    [("claude-haiku-5-5", True), ("zai-org/GLM-5.3-Flash", False), ("deepseek-v4.1", False)],
)
def test_tool_pictures_follow_the_results_only_for_a_model_that_sees(tmp_path, model, shown):
    provider = ScriptedProvider([Turn(calls=[("shot", {}), ("shot", {})]), Turn(text="done")])
    config = SessionConfig(model=model, workspace=tmp_path, home=tmp_path / "h")
    session = Session(config, provider, extra_tools=[_picture_tool(tmp_path)])
    assert _run(session, "look", None)[-1].status == "completed"
    sent = provider.requests[1].messages
    roles = [m.role for m in sent]
    stored = [m for m in session.messages if m.role == "tool"]
    assert all(len(m.images) == 1 for m in stored)  # kept in the session either way
    assert all(not m.images for m in sent if m.role == "tool")  # never on the wire there
    if shown:
        assert roles[-3:] == ["tool", "tool", "user"]
        assert len(sent[-1].images) == 2
        assert "pictures from the tool results above: shot" in sent[-1].content
    else:
        assert roles[-2:] == ["tool", "tool"]
        assert not any(m.images for m in sent)


def test_read_of_a_picture_file_attaches_it(tmp_path, ctx):
    from karox6.tools.fs import ReadTool

    path = _picture(ctx.workspace)[0]["path"]
    result = asyncio.run(ReadTool().run({"file_path": path}, ctx))
    assert not result.is_error
    assert "is a picture (image/png" in result.content
    assert [str(p) for p in result.images] == [path]
