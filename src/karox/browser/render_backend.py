"""Which renderer a headless Chromium uses for WebGL.

Playwright's headless shell draws WebGL with SwiftShader, a CPU rasteriser,
unless it is told otherwise -- even on a machine with a graphics card. Measured
on a developer PC (Ryzen 5 5600, Radeon RX 570, 2026-11-05): the same headless
shell reported ``SwiftShader driver`` by default and ``Radeon RX 570 ...
Direct3D11`` with the hardware flags below, and a software-rendered three.js
scene held every core while KaroX looked at it. A visual check, a render check
and every browser action on a 3D page pay that cost.

So on Windows, where ANGLE's Direct3D 11 backend is the browser's normal path
and falls back to WARP without a GPU, the hardware renderer is the default.
Elsewhere the software path stays the default (a sandbox without a GPU is the
common case there); ``KAROX_BROWSER_GPU=1`` opts in, ``KAROX_BROWSER_GPU=0``
forces software everywhere. ``--enable-unsafe-swiftshader`` is kept in every
set so WebGL still works when no hardware context can be created, and a launch
that fails with hardware flags is retried with the software set.
"""

from __future__ import annotations

import os
import sys

SOFTWARE_ARGS = ("--disable-gpu", "--enable-unsafe-swiftshader")
HARDWARE_ARGS = ("--enable-gpu", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader")
WINDOWS_HARDWARE_ARGS = ("--use-angle=d3d11", *HARDWARE_ARGS)

_OFF = {"0", "off", "false", "no", "software"}
_ON = {"1", "on", "true", "yes", "hardware"}


def hardware_rendering() -> bool:
    """Whether headless browsers should ask for the GPU on this machine."""

    mode = os.environ.get("KAROX_BROWSER_GPU", "auto").strip().lower()
    if mode in _OFF:
        return False
    if mode in _ON:
        return True
    return sys.platform == "win32"


def launch_args() -> list[str]:
    """Chromium flags for a headless launch on this machine."""

    if not hardware_rendering():
        return list(SOFTWARE_ARGS)
    return list(WINDOWS_HARDWARE_ARGS if sys.platform == "win32" else HARDWARE_ARGS)


def software_args() -> list[str]:
    """The fallback set: SwiftShader, which every Chromium build carries."""

    return list(SOFTWARE_ARGS)
