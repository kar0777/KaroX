"""Skills (SKILL.md folders), slash commands and plugins."""

import asyncio
import json

import pytest

from karox6 import plugins
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.skills import SkillTool, discover, expand, listing, split_front_matter


def _skill(root, name, description, body="Do the thing.", folder=None):
    path = root / (folder or name)
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """A home directory of its own: the real ~/.claude/skills never leak in."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


def test_front_matter_forms():
    text = (
        "---\nname: x\ndescription: >\n  folded over\n  two lines\nnote: 'it''s'\n"
        'quoted: "a \\"b\\""\nliteral: |\n  one\n  two\n---\nbody here\n'
    )
    fields, body = split_front_matter(text)
    assert fields["description"] == "folded over two lines"
    assert fields["note"] == "it's"
    assert fields["quoted"] == 'a "b"'
    assert fields["literal"] == "one\ntwo"
    assert body.strip() == "body here"
    assert split_front_matter("no front matter") == ({}, "no front matter")
    assert split_front_matter("---\nname: x\nnever closed")[0] == {}


def test_discovery_order_and_validity(tmp_path, user_home):
    ws, home = tmp_path / "ws", tmp_path / "karox"
    _skill(ws / ".claude" / "skills", "review", "project review")
    _skill(home / "skills", "review", "user review")  # the project's wins
    _skill(user_home / ".claude" / "skills", "notes", "release notes")
    _skill(ws / ".karox" / "skills", "Bad Name!", "x")  # not a usable name
    bare = ws / ".agents" / "skills" / "empty"
    bare.mkdir(parents=True)
    (bare / "SKILL.md").write_text("no front matter, no description\n", encoding="utf-8")
    found = {s.name: s for s in discover(ws, home)}
    assert set(found) == {"review", "notes"}
    assert (found["review"].description, found["review"].source) == ("project review", "project")
    assert found["notes"].source == "user"


def test_the_name_defaults_to_the_folder(tmp_path, user_home):
    path = tmp_path / "ws" / ".karox" / "skills" / "deploy-check"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\ndescription: check a deploy\n---\nSteps.\n")
    assert [s.name for s in discover(tmp_path / "ws", None)] == ["deploy-check"]


def test_listing_is_bounded_and_lists_no_bodies(tmp_path, user_home):
    root = tmp_path / "ws" / ".karox" / "skills"
    for i in range(80):
        _skill(root, f"s{i:02d}", "d" * 500, body="SECRET BODY")
    text = listing(discover(tmp_path / "ws", None))
    assert "SECRET BODY" not in text
    assert len(text) < 13_000
    assert "more skills" in text
    assert listing([]) == ""


def test_expand_and_arguments(tmp_path, user_home):
    from karox6.skills import _read_skill

    folder = _skill(tmp_path, "fix", "fix things", body="Fix $ARGUMENTS now.")

    skill = _read_skill(folder / "SKILL.md", "project")
    text = expand(skill, "the login page")
    assert "Fix the login page now." in text
    assert "Arguments:" not in text
    assert folder.as_posix() in text
    other = _read_skill(_skill(tmp_path, "plain", "p") / "SKILL.md", "project")
    assert expand(other, "x y").endswith("Arguments: x y")


def test_the_tool_loads_and_lists(tmp_path, ctx, user_home):
    _skill(tmp_path / "ws" / ".karox" / "skills", "fix", "fix things", body="Step one.")
    tool = SkillTool(discover(tmp_path / "ws", None))
    loaded = asyncio.run(tool.run({"name": "/FIX"}, ctx))
    assert "Step one." in loaded.content
    assert not loaded.is_error
    assert "fix (project): fix things" in asyncio.run(tool.run({}, ctx)).content
    missing = asyncio.run(tool.run({"name": "fi"}, ctx))
    assert missing.is_error
    assert "Did you mean: fix" in missing.content


def test_a_session_offers_skills_only_when_there_are_some(tmp_path, user_home):
    ws = tmp_path / "ws"
    ws.mkdir()
    config = SessionConfig(model="m", workspace=ws, home=tmp_path / "h", skills=True)
    session = Session(config, ScriptedProvider([Turn(text="ok")]))
    assert "skill" not in session._tool_map
    assert "Skills:" not in session.system_prompt
    _skill(ws / ".karox" / "skills", "fix", "fix things")
    session = Session(config, ScriptedProvider([Turn(text="ok")]))
    assert "skill" in session._tool_map
    assert "- fix: fix things" in session.system_prompt
    off = SessionConfig(model="m", workspace=ws, home=tmp_path / "h")  # the default: off
    assert "skill" not in Session(off, ScriptedProvider([Turn(text="ok")]))._tool_map


def test_commands_are_for_the_user_not_the_model(tmp_path, user_home):
    ws = tmp_path / "ws"
    commands = ws / ".claude" / "commands"
    (commands / "git").mkdir(parents=True)
    (commands / "ship.md").write_text("---\ndescription: ship it\n---\nShip $ARGUMENTS.\n")
    (commands / "git" / "tidy.md").write_text("# Tidy the branch\nSquash fixups.\n")
    found = {s.name: s for s in discover(ws, None, commands=True)}
    assert found["ship"].kind == "command"
    assert found["git:tidy"].description == "Tidy the branch"
    assert expand(found["ship"], "v2") == "Ship v2."  # the prompt as written, no skill wrapper
    assert expand(found["git:tidy"], "fast") == "# Tidy the branch\nSquash fixups.\n\nfast"
    assert discover(ws, None) == []  # without commands
    assert listing(list(found.values())) == ""  # never listed to the model
    assert SkillTool(list(found.values())).skills == {}


def _plugin(tmp_path, name="kit"):
    src = tmp_path / "src" / name
    _skill(src / "skills", "kit-skill", "from the plugin")
    (src / "commands").mkdir(exist_ok=True)
    (src / "commands" / "kit-cmd.md").write_text("Run the kit.\n")
    (src / ".claude-plugin").mkdir(exist_ok=True)
    (src / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "description": "a kit", "version": "1.2"})
    )
    (src / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kit": {
                        "command": "python",
                        "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
                        "env": {"ROOT": "${KAROX_PLUGIN_ROOT}"},
                    }
                }
            }
        )
    )
    return src


def test_plugin_install_enable_disable_remove(tmp_path, user_home):
    home = tmp_path / "karox"
    installed = plugins.install(home, str(_plugin(tmp_path)))
    assert installed.name == "kit"
    assert installed.parts() == ["1 skill", "1 command", "MCP"]
    (listed,) = plugins.plugins(home)
    assert (listed.description, listed.version, listed.enabled) == ("a kit", "1.2", True)
    servers = plugins.mcp_servers(home)
    root = (home / "plugins" / "kit").as_posix()
    assert servers["kit"]["args"] == [f"{root}/server.py"]
    assert servers["kit"]["env"] == {"ROOT": root}
    found = {s.name: s for s in discover(tmp_path / "ws", home, commands=True)}
    assert found["kit-skill"].source == "plugin:kit"
    assert found["kit-cmd"].kind == "command"

    plugins.set_enabled(home, "kit", False)
    assert plugins.mcp_servers(home) == {}
    assert discover(tmp_path / "ws", home, commands=True) == []
    plugins.set_enabled(home, "KIT", True)
    assert plugins.mcp_servers(home)

    with pytest.raises(plugins.PluginError, match="installed"):
        plugins.install(home, str(_plugin(tmp_path)))
    plugins.remove(home, "kit")
    assert plugins.plugins(home) == []
    with pytest.raises(plugins.PluginError, match="no plugin"):
        plugins.remove(home, "kit")


def test_a_folder_that_is_not_a_plugin_is_not_kept(tmp_path):
    home = tmp_path / "karox"
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "README.md").write_text("hi")
    with pytest.raises(plugins.PluginError, match="not a plugin"):
        plugins.install(home, str(junk))
    assert not (home / "plugins" / "junk").exists()
    with pytest.raises(plugins.PluginError, match="no such folder"):
        plugins.install(home, str(tmp_path / "missing"))
    with pytest.raises(plugins.PluginError, match="usable plugin name"):
        plugins.install(home, str(_plugin(tmp_path)), name="../escape")


def test_a_broken_plugin_mcp_file_is_reported_not_fatal(tmp_path):
    home = tmp_path / "karox"
    folder = home / "plugins" / "bad"
    _skill(folder / "skills", "b", "b")
    (folder / "mcp.json").write_text("{not json")
    seen = []
    assert plugins.mcp_servers(home, warn=seen.append) == {}
    assert "plugin bad" in seen[0]


def test_user_mcp_config_wins_over_a_plugin(tmp_path, monkeypatch):
    from karox6.mcp import client

    started = []

    async def fake_connect(name, spec):
        started.append((name, spec.get("command")))
        raise RuntimeError("not really")

    monkeypatch.setattr(client, "connect", fake_connect)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"kit": {"command": "mine"}}}))
    extra = {"kit": {"command": "plugin"}, "other": {"command": "plugin2"}}
    asyncio.run(client.load_mcp_tools(config, warn=lambda _m: None, extra=extra))
    assert sorted(started) == [("kit", "mine"), ("other", "plugin2")]
    asyncio.run(client.load_mcp_tools(None, warn=lambda _m: None, extra={"x": {"command": "c"}}))
    assert ("x", "c") in started


def test_front_matter_comments_escapes_and_indented_fences():
    text = (
        '---\nname: "review" # the name\nwhere: plain value # note\n'
        "description: |\n  first\n  ---\n  after the inner fence\n"
        'esc: "tab\there"\n---\nbody\n'
    )
    fields, body = split_front_matter(text)
    assert fields["name"] == "review"
    assert fields["where"] == "plain value"
    assert fields["description"] == "first\n---\nafter the inner fence"
    assert fields["esc"] == "tab\there"
    assert body.strip() == "body"


def test_a_project_command_beats_a_user_skill(tmp_path, user_home):
    ws, home = tmp_path / "ws", tmp_path / "karox"
    _skill(home / "skills", "deploy", "user deploy skill")
    (ws / ".karox" / "commands").mkdir(parents=True)
    (ws / ".karox" / "commands" / "deploy.md").write_text("Project deploy.\n")
    found = {s.name: s for s in discover(ws, home, commands=True)}
    assert (found["deploy"].kind, found["deploy"].source) == ("command", "project")


def test_install_refuses_a_folder_holding_the_plugins_folder(tmp_path):
    home = tmp_path / "karox"
    _skill(home / "skills", "s", "s")  # tmp_path/karox looks like a plugin, and holds plugins/
    with pytest.raises(plugins.PluginError, match="plugins folder itself"):
        plugins.install(home, str(home))


def test_a_failed_install_leaves_nothing_behind(tmp_path, monkeypatch):
    home = tmp_path / "karox"

    def broken_clone(source, into):
        into.mkdir(parents=True)
        raise plugins.PluginError("git clone failed: no network")

    monkeypatch.setattr(plugins, "_clone", broken_clone)
    with pytest.raises(plugins.PluginError, match="no network"):
        plugins.install(home, "https://example.com/kit.git")
    assert list((home / "plugins").iterdir()) == []
