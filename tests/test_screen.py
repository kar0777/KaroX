import asyncio
import struct
import sys
import zlib
from pathlib import Path

import pytest

from karox6.tools import screen
from karox6.tools.screen import ScreenError, ScreenshotTool, Window

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="GDI capture is Windows")


def chunks(png):
    assert png.startswith(screen.PNG_SIGNATURE)
    pos, found = 8, []
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + data), kind
        found.append((kind, data))
        pos += 12 + length
    return found


def test_png_encoder_writes_a_valid_truecolor_png():
    # 3x2: red, green, blue / white, black, grey
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 128, 128, 128])
    png = screen.encode_png(3, 2, rgb)
    found = chunks(png)
    assert [kind for kind, _ in found] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", found[0][1]) == (3, 2, 8, 2, 0, 0, 0)
    raw = zlib.decompress(found[1][1])
    assert len(raw) == 2 * (1 + 9)
    rows = [raw[0:10], raw[10:20]]
    assert [row[0] for row in rows] == [0, 0]  # filter type None
    assert rows[0][1:4] == bytes([255, 0, 0])  # top-left red
    assert rows[0][7:10] == bytes([0, 0, 255])  # top-right blue
    assert rows[1][7:10] == bytes([128, 128, 128])  # bottom-right grey
    assert found[2][1] == b""
    assert screen.png_size(png) == (3, 2)
    with pytest.raises(ValueError, match="not a 3x2 RGB image"):
        screen.encode_png(3, 2, rgb[:-1])


def test_bgra_rows_become_rgb():
    assert screen.bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 0])) == bytes([3, 2, 1, 6, 5, 4])


def test_window_lookup_prefers_an_exact_title_then_the_topmost_match():
    def win(title):
        return Window(1, title, 0, 0, 100, 100, False)

    windows = [win("Notes - Editor"), win("editor"), win("Other")]
    assert screen._find(windows, "EDITOR") == (windows[1], 2)  # exact beats topmost
    assert screen._find(windows, "notes") == (windows[0], 1)
    with pytest.raises(ScreenError, match=r"no visible window title contains 'zzz'.*\"Other\""):
        screen._find(windows, "zzz")


def fake_png(width, height=10):
    return screen.encode_png(width, height, bytes(width * height * 3))


def test_posix_capture_shrinks_with_the_resizer(monkeypatch):
    commands = []

    def run(command):
        commands.append(command)
        if "-resize" in command:  # magick <in> -resize <W>x <out>
            width = int(command[command.index("-resize") + 1][:-1])
            Path(command[-1]).write_bytes(fake_png(width))
        else:
            Path(command[-1]).write_bytes(fake_png(3000))

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(screen, "_run", run)
    monkeypatch.setattr(screen.shutil, "which", lambda name: f"/usr/bin/{name}")
    shot = screen.capture(None, 1200)
    assert (shot.width, shot.height) == (1200, 10)
    assert shot.what == "the screen (3000x10 px)"
    assert commands[0][0] == "grim"
    assert commands[1][0] == "/usr/bin/magick"


def test_posix_capture_without_a_tool_says_what_to_install(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(screen.shutil, "which", lambda name: None)
    with pytest.raises(ScreenError, match="install grim"):
        screen.capture()
    with pytest.raises(ScreenError, match="not supported on this platform"):
        screen.capture("Editor")
    with pytest.raises(ScreenError, match="not supported on this platform"):
        screen.windows_text()


@windows_only
def test_desktop_capture_on_windows():
    shot = screen.capture(None, 1600)
    assert screen.png_size(shot.png) == (shot.width, shot.height)
    assert 100 <= shot.width <= 1600
    assert shot.height >= 100
    assert 1000 < len(shot.png) <= screen.PNG_LIMIT
    assert shot.what.startswith("the whole desktop (")
    found = chunks(shot.png)
    pixels = zlib.decompress(b"".join(data for kind, data in found if kind == b"IDAT"))
    assert len(pixels) == shot.height * (1 + shot.width * 3)


@windows_only
def test_capture_scales_down_to_fit(monkeypatch):
    small = screen.capture(None, 400)
    assert small.width <= 400
    full = screen.capture(None, 2560)
    source_width = int(full.what.split(", ")[1].split("x")[0])
    assert small.width == min(400, source_width)
    monkeypatch.setattr(screen, "PNG_LIMIT", 10_000)
    tight = screen.capture(None, 2560)
    assert tight.width < min(2560, source_width)
    assert len(tight.png) <= 10_000 or tight.width == screen.MIN_WIDTH


@windows_only
def test_list_windows_and_an_unknown_title(ctx):
    tool = ScreenshotTool()
    listed = asyncio.run(tool.run({"list_windows": True}, ctx))
    assert not listed.is_error
    assert listed.content.startswith(("Visible windows, topmost first", "No visible windows"))
    assert "image_png" not in listed.meta
    missing = asyncio.run(tool.run({"window": "no such window 0x5f3a"}, ctx))
    assert missing.is_error
    assert "no visible window title contains 'no such window 0x5f3a'" in missing.content


def test_png_size_rejects_a_cut_png():
    with pytest.raises(screen.ScreenError):
        screen.png_size(screen.PNG_SIGNATURE + b"\0\0\0\rIHDR\0\0")
