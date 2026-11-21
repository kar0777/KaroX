"""Chat features: /undo and /redo, /fork, /init, pictures, /mcp, the change preview."""

import asyncio
import json
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from karox6 import chat_features, cli, clipboard
from karox6.chat_features import (
    McpState,
    closest_command,
    diff_preview,
    find_images,
    mcp_report,
)
from karox6.core.messages import Message
from karox6.core.session import SessionConfig
from karox6.core.store import SessionStore
from karox6.providers.anthropic import AnthropicProvider
from karox6.providers.base import ModelRequest
from karox6.providers.gemini import GeminiProvider
from karox6.providers.openai_chat import to_wire
from karox6.providers.openai_responses import OpenAIResponsesProvider
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.screen import encode_png
from karox6.tui import ChatApp
from karox6.tui_text import Palette, _glyphs

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
PLAIN = Palette(False)
GLYPH = _glyphs("utf-8")


class Term:
    """Scripted terminal: each line is text, or a callable run right before it is read."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.out = []

    async def read_line(self, prompt):
        if not self.lines:
            raise EOFError
        line = self.lines.pop(0)
        if callable(line):
            line()
            return await self.read_line(prompt)
        return line

    def write(self, text):
        self.out.append(text)

    @property
    def text(self):
        return "".join(self.out)


def chat(tmp_path, turns, lines, **kwargs):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    term = Term(lines)
    provider = ScriptedProvider(list(turns))
    app = ChatApp(
        config=SessionConfig(model="test", workspace=ws, home=tmp_path / "home"),
        provider=provider,
        read_line=term.read_line,
        write=term.write,
        **kwargs,
    )
    assert asyncio.run(app.run()) == 0
    return app, provider, term, ws


def write_turn(path, content, reply="done"):
    return [Turn(calls=[("write", {"file_path": path, "content": content})]), Turn(text=reply)]


# -- /undo, /redo ---------------------------------------------------------------------


@needs_git
def test_undo_takes_back_the_turns_files_and_messages_and_redo_returns_them(tmp_path):
    back = []
    turns = [*write_turn("made.txt", "one\n"), *write_turn("made.txt", "two\n", "again")]
    lines = ["create it", "now change it", "/undo", "/undo", "/undo", "/redo", "/quit"]
    app, _, term, ws = chat(tmp_path, turns, lines, snapshots=True, prefill=back.append)
    # two undos took both turns back, the third had nothing left; one redo returned turn 1
    assert term.text.count("undid: ") == 2
    assert "undid: now change it" in term.text
    assert "undid: create it" in term.text
    assert "nothing to undo" in term.text
    assert "redid: create it" in term.text
    assert "restored 1 file: made.txt" in term.text
    assert "re-applied 1 file: made.txt" in term.text
    assert (ws / "made.txt").read_text(encoding="utf-8") == "one\n"
    assert back == ["now change it", "create it"]  # each undone message returns to the input
    roles = [m.role for m in app.session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]  # turn one only
    # what the log holds is what a later --resume sees
    saved = SessionStore.load(tmp_path / "home", app.session.session_id)
    assert [m.content for m in saved.messages if m.role == "user"] == ["create it"]


@needs_git
def test_the_model_does_not_see_an_undone_turn(tmp_path):
    turns = [*write_turn("a.txt", "x\n"), Turn(text="fresh answer")]
    _, provider, _, ws = chat(
        tmp_path, turns, ["make a.txt", "/undo", "something else", "/quit"], snapshots=True
    )
    assert not (ws / "a.txt").exists()
    assert [m.content for m in provider.requests[-1].messages] == ["something else"]


@needs_git
def test_undo_leaves_a_file_the_user_changed_afterwards(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    def user_edits():
        (ws / "mine.txt").write_text("agent text\nand my own line\n", encoding="utf-8")

    turns = [
        Turn(
            calls=[
                ("write", {"file_path": "mine.txt", "content": "agent text\n"}),
                ("write", {"file_path": "other.txt", "content": "agent only\n"}),
            ]
        ),
        Turn(text="done"),
    ]
    _, _, term, _ = chat(
        tmp_path, turns, ["write both", user_edits, "/undo", "/quit"], snapshots=True
    )
    assert (ws / "mine.txt").read_text(encoding="utf-8") == "agent text\nand my own line\n"
    assert not (ws / "other.txt").exists()
    assert "restored 1 file: other.txt" in term.text
    assert "left alone (changed since): mine.txt" in term.text


def test_undo_without_snapshots_still_takes_back_the_messages(tmp_path):
    turns = write_turn("kept.txt", "stays\n")
    app, _, term, ws = chat(tmp_path, turns, ["make it", "/undo", "/redo", "/redo", "/quit"])
    assert (ws / "kept.txt").exists()  # nothing was recorded, so nothing is restored
    assert "files were not recorded" in term.text
    assert "nothing to redo" in term.text
    assert len(app.session.messages) == 4


@needs_git
def test_a_new_turn_clears_redo_and_new_or_resume_clear_undo(tmp_path):
    turns = [*write_turn("a.txt", "1\n"), Turn(text="second"), Turn(text="third")]
    lines = ["first", "/undo", "second", "/redo", "/new", "/undo", "third", "/quit"]
    _, _, term, _ = chat(tmp_path, turns, lines, snapshots=True)
    assert "nothing to redo" in term.text  # "second" was a new turn
    assert "nothing to undo" in term.text  # /new: the old session's turns are not this one's


# -- /fork ----------------------------------------------------------------------------


def test_fork_continues_in_a_copy_and_leaves_the_original(tmp_path):
    turns = [Turn(text="first answer"), Turn(text="second answer")]
    app, provider, term, _ = chat(tmp_path, turns, ["/fork", "hello", "/fork", "more", "/quit"])
    assert "nothing to fork yet" in term.text
    home = tmp_path / "home"
    forked = app.session.session_id
    original = next(p.name for p in (home / "sessions").iterdir() if p.name != forked)
    assert f"forked: now in {forked}, a copy of {original} (2 messages)" in term.text
    assert f"/resume {original}" in term.text
    # the fork carries the conversation on...
    sent = [m.content for m in provider.requests[1].messages]
    assert sent == ["hello", "first answer", "more"]
    # ...the original keeps only what it had, and each resumes on its own
    assert [m.content for m in SessionStore.load(home, original).messages] == sent[:2]
    resumed = SessionStore.load(home, forked)
    assert [m.content for m in resumed.messages] == [*sent, "second answer"]
    assert resumed.usage.requests == 2  # the copied turn's usage and its own


def test_cli_flags_continue_and_fork():
    args = cli._parser({}).parse_args(["chat", "--model", "m", "--continue"])
    assert args.resume == "last"
    args = cli._parser({}).parse_args(["chat", "--model", "m", "--fork", "last", "--no-undo"])
    assert (args.fork, args.resume, args.no_undo) == ("last", None, True)


# -- /init ----------------------------------------------------------------------------


def test_init_asks_for_agents_md_and_is_named_in_undo(tmp_path):
    turns = [Turn(text="written"), Turn(text="written again")]
    _, provider, term, _ = chat(tmp_path, turns, ["/init", "/init the tests", "/undo", "/quit"])
    first = provider.requests[0].messages[-1].content
    assert first.startswith("Write an AGENTS.md file at the root of this project")
    assert "Pay special attention to" not in first
    assert provider.requests[1].messages[-1].content.endswith("Pay special attention to: the tests")
    assert "undid: /init the tests" in term.text


# -- pictures -------------------------------------------------------------------------


def png(path: Path, width=2, height=2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_png(width, height, bytes([200, 30, 30]) * (width * height)))
    return path


def test_a_picture_named_in_the_message_is_attached_and_kept_with_the_session(tmp_path):
    ws = tmp_path / "ws"
    png(ws / "shots" / "bug.png", 3, 2)
    app, provider, term, _ = chat(
        tmp_path,
        [Turn(text="I see it"), Turn(text="still")],
        ["why? @shots/bug.png", "and now?", "/quit"],
    )
    user = provider.requests[0].messages[-1]
    assert user.content == "why? [image 1]"
    assert len(user.images) == 1
    stored = Path(user.images[0]["path"])
    assert stored.parent == app.session.store.dir / "images"
    assert stored.read_bytes() == (ws / "shots" / "bug.png").read_bytes()
    assert user.images[0]["media_type"] == "image/png"
    assert "pictures: bug.png (3x2, " in term.text
    # the picture stays in the conversation on later requests and after a resume
    assert provider.requests[1].messages[0].images == user.images
    saved = SessionStore.load(tmp_path / "home", app.session.session_id)
    assert saved.messages[0].images == user.images


def test_finding_pictures_in_a_message(tmp_path):
    png(tmp_path / "a.png")
    png(tmp_path / "my shots" / "b c.JPG")
    absolute = png(tmp_path / "abs.webp")
    found = find_images(f'see @a.png and "my shots/b c.JPG" and {absolute} and @a.png', tmp_path)
    assert [p.name for p in found.paths] == ["a.png", "b c.JPG", "abs.webp"]
    assert found.text == "see [image 1] and [image 2] and [image 3] and [image 1]"
    # a bare name in a sentence is not an attachment; a missing @picture is said
    assert find_images("please fix a.png and logo.png", tmp_path).paths == []
    missing = find_images("look @nope.png", tmp_path)
    assert missing.paths == []
    assert missing.warnings == ["skipped @nope.png (no such picture)"]
    (tmp_path / "huge.png").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    big = find_images("@huge.png", tmp_path)
    assert big.paths == []
    assert "pictures up to 5 MB" in big.warnings[0]
    # numbering continues after pictures that are already waiting (from /paste)
    assert find_images("@a.png", tmp_path, already=2).text == "[image 3]"


def test_pictures_on_the_wire_for_every_provider(tmp_path):
    picture = png(tmp_path / "p.png")
    data = __import__("base64").b64encode(picture.read_bytes()).decode()
    image = {"path": str(picture), "media_type": "image/png"}
    gone = {"path": str(tmp_path / "deleted.png"), "media_type": "image/png"}
    message = Message("user", "what is this?", images=[image, gone])
    request = ModelRequest("m", "sys", [message])

    wire = to_wire(message)
    assert wire["content"][0] == {"type": "text", "text": "what is this?"}
    assert wire["content"][1]["image_url"]["url"] == f"data:image/png;base64,{data}"
    assert wire["content"][2] == {
        "type": "text",
        "text": "[image deleted.png is no longer available]",
    }
    assert to_wire(Message("user", "plain")) == {"role": "user", "content": "plain"}

    blocks = AnthropicProvider("https://x").build_body(request)["messages"][0]["content"]
    assert blocks[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }
    assert [b["type"] for b in blocks] == ["image", "text", "text"]
    assert blocks[-1]["text"] == "what is this?"

    parts = GeminiProvider("https://x").build_body(request)["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "image/png", "data": data}}
    assert parts[-1]["text"] == "what is this?"

    items = OpenAIResponsesProvider("https://x").build_body(request)["input"][0]["content"]
    assert items[0] == {"type": "input_text", "text": "what is this?"}
    assert items[1] == {"type": "input_image", "image_url": f"data:image/png;base64,{data}"}


def test_paste_keeps_the_clipboard_picture_for_the_next_message(tmp_path, monkeypatch):
    def grab(target):
        return png(target, 4, 4)

    monkeypatch.setattr(clipboard, "grab_image", grab)
    _, provider, term, _ = chat(tmp_path, [Turn(text="ok")], ["/paste", "what is on it?", "/quit"])
    assert "picture ready: clipboard-" in term.text
    user = provider.requests[0].messages[-1]
    assert len(user.images) == 1
    assert user.content == "what is on it?"

    def nothing(target):
        raise clipboard.ClipboardError("the clipboard holds no picture")

    monkeypatch.setattr(clipboard, "grab_image", nothing)
    _, provider, term, _ = chat(tmp_path, [Turn(text="ok")], ["/paste", "hello", "/quit"])
    assert "the clipboard holds no picture" in term.text
    assert provider.requests[0].messages[-1].images == []


def test_windows_clipboard_bitmaps_become_png():
    header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 0, 0, 0, 0, 0)
    blue, red = bytes([255, 0, 0]), bytes([0, 0, 255])  # stored as B, G, R
    bottom_up = header + blue * 2 + b"\0\0" + red * 2 + b"\0\0"  # rows padded to 4 bytes
    out = clipboard.dib_to_png(bottom_up)
    assert out == encode_png(2, 2, bytes([255, 0, 0]) * 2 + bytes([0, 0, 255]) * 2)  # red on top
    top_down = struct.pack("<IiiHHIIiiII", 40, 1, -2, 1, 32, 0, 0, 0, 0, 0, 0)
    out = clipboard.dib_to_png(top_down + bytes([1, 2, 3, 255]) + bytes([4, 5, 6, 255]))
    assert out == encode_png(1, 2, bytes([3, 2, 1, 6, 5, 4]))
    eight_bit = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 8, 0, 0, 0, 0, 0, 0) + b"\0" * 8
    with pytest.raises(clipboard.ClipboardError, match="8 bits per pixel"):
        clipboard.dib_to_png(eight_bit)
    with pytest.raises(clipboard.ClipboardError, match="cut short"):
        clipboard.dib_to_png(header + b"\0" * 3)


# -- the change preview ---------------------------------------------------------------


def test_diff_preview_numbers_lines_and_caps_long_changes():
    args = json.dumps(
        {"old_string": "a = 1\nb = 2\nc = 3", "new_string": "a = 1\nb = 20\nc = 3\nd = 4"}
    )
    assert diff_preview("edit", args, {"line": 10}) == [
        ("-", "   11 - b = 2"),
        ("+", "   11 + b = 20"),
        ("+", "   13 + d = 4"),
    ]
    assert diff_preview("edit", args, {}) == [
        ("-", "  - b = 2"),
        ("+", "  + b = 20"),
        ("+", "  + d = 4"),
    ]
    long = json.dumps({"old_string": "x", "new_string": "\n".join(f"line {i}" for i in range(20))})
    rows = diff_preview("agent-1/edit", long, {"line": 1}, limit=5)
    assert len(rows) == 6
    assert rows[-1] == ("…", "  … 16 more changed lines (/diff)")
    assert diff_preview("bash", args, {}) == []
    assert diff_preview("edit", "not json", {}) == []
    assert diff_preview("edit", "\x1b[2Jevil", {}) == []


def test_an_edit_shows_what_it_changed_under_its_line(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("import os\n\n\ndef main():\n    print('hi')\n", encoding="utf-8")
    edit = {"file_path": "app.py", "old_string": "print('hi')", "new_string": "print('hello')"}
    _, _, term, _ = chat(tmp_path, [Turn(calls=[("edit", edit)]), Turn(text="ok")], ["go", "/quit"])
    lines = term.text.splitlines()
    at = next(i for i, line in enumerate(lines) if "edit app.py" in line)
    assert "+1 -1" in lines[at]
    assert lines[at + 1] == "      5 - print('hi')"
    assert lines[at + 2] == "      5 + print('hello')"


# -- /mcp, hints ----------------------------------------------------------------------


def test_mcp_screen(tmp_path):
    assert "send one, then /mcp" in mcp_report(None, PLAIN, GLYPH)
    assert "--no-mcp" in mcp_report(McpState(path=None), PLAIN, GLYPH)
    config = tmp_path / "mcp.json"
    empty = mcp_report(McpState(path=config), PLAIN, GLYPH)
    assert "no MCP servers configured" in empty
    assert str(config) in empty
    assert '"mcpServers"' in empty
    servers = {
        "docs": {"command": "npx", "args": ["-y", "docs-server"], "env": {"TOKEN": "secret-1"}},
        "web": {"url": "https://mcp.example.test/mcp", "headers": {"Authorization": "secret-2"}},
        "off": {"command": "x", "disabled": True},
    }
    config.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    tools = [{"name": "search", "description": "Search  the\ndocs"}, {"name": "open"}]
    handle = SimpleNamespace(name="docs", tools=tools, restarts=1)
    state = McpState(path=config, handles=[handle], failures={"web": "401 Unauthorized"})
    report = mcp_report(state, PLAIN, GLYPH)
    assert "✓ docs  connected, 2 tools, restarted 1x" in report
    assert "stdio: npx -y docs-server" in report
    assert "✗ web  failed: 401 Unauthorized" in report
    assert "http: https://mcp.example.test/mcp" in report
    assert "- off  disabled in the config" in report
    assert "secret" not in report  # no environment, no headers
    one = mcp_report(state, PLAIN, GLYPH, "docs")
    assert "mcp__docs__search  Search the docs" in one
    assert "mcp__docs__open" in one
    assert mcp_report(state, PLAIN, GLYPH, "web") == "web: not connected (401 Unauthorized)\n"
    assert "no MCP server named nope; configured: docs, web, off" in mcp_report(
        state, PLAIN, GLYPH, "nope"
    )


def test_a_mistyped_command_names_the_one_that_was_meant(tmp_path):
    names = ["/status", "/sessions", "/setup", "/help", "/undo", "/quit"]
    assert closest_command("/stauts", names) == "/status"
    assert closest_command("/hlp", names) == "/help"
    assert closest_command("/und", names) == "/undo"
    assert closest_command("/s", names) is None  # three commands start so
    assert closest_command("/zzzz", names) is None
    _, _, term, _ = chat(tmp_path, [], ["/stauts", "/zzzz", "/quit"])
    assert "unknown command /stauts; did you mean /status? /help lists commands" in term.text
    assert "unknown command /zzzz; /help lists commands" in term.text


def test_init_prompt_is_generic():
    text = chat_features.INIT_PROMPT.lower()
    assert "agents.md" in text
    assert "single test" in text
    assert "do not change any other file" in text
