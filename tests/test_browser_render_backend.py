"""Headless WebGL asks for the GPU where the hardware path is the normal one."""

from __future__ import annotations

import pytest

from karox.browser import render_backend


@pytest.mark.parametrize(
    ("platform", "env", "hardware"),
    [
        ("win32", None, True),
        ("linux", None, False),
        ("darwin", None, False),
        ("win32", "0", False),
        ("linux", "1", True),
        ("linux", "software", False),
    ],
)
def test_the_renderer_choice(monkeypatch, platform, env, hardware) -> None:
    monkeypatch.setattr(render_backend.sys, "platform", platform)
    if env is None:
        monkeypatch.delenv("KAROX_BROWSER_GPU", raising=False)
    else:
        monkeypatch.setenv("KAROX_BROWSER_GPU", env)

    args = render_backend.launch_args()

    assert render_backend.hardware_rendering() is hardware
    assert ("--disable-gpu" in args) is not hardware
    # WebGL must still work when no hardware context can be created.
    assert "--enable-unsafe-swiftshader" in args
    if hardware and platform == "win32":
        assert "--use-angle=d3d11" in args


def test_the_fallback_is_always_software() -> None:
    assert render_backend.software_args() == ["--disable-gpu", "--enable-unsafe-swiftshader"]
