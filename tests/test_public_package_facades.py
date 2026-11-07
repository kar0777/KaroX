"""The package facades stay compatible while loading lazily.

Public imports such as ``from karox.browser import BrowserSession`` existed
before the package initializers were made lazy.  These assertions protect that
public path, including submodule imports used by tests and callers, without
requiring the whole package at import time.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def test_browser_public_names_stay_identical_to_their_modules() -> None:
    browser = importlib.import_module("karox.browser")
    session = importlib.import_module("karox.browser.session")
    netpolicy = importlib.import_module("karox.browser.netpolicy")
    assert browser.BrowserSession is session.BrowserSession
    assert browser.CancelToken is session.CancelToken
    assert browser.BrowserAccessPolicy is netpolicy.BrowserAccessPolicy
    assert "BrowserSession" in browser.__all__


def test_browser_submodule_import_path_still_works() -> None:
    from karox.browser import code_sources

    assert code_sources.__name__ == "karox.browser.code_sources"


def test_tui_public_names_stay_identical_to_the_app_module() -> None:
    tui = importlib.import_module("karox.tui")
    app = importlib.import_module("karox.tui.app")
    assert tui.KaroxApp is app.KaroxApp
    assert tui.run_tui is app.run_tui


def test_tui_submodule_import_path_still_works() -> None:
    from karox.tui import commands

    assert commands.__name__ == "karox.tui.commands"


@pytest.mark.skipif(sys.platform != "win32", reason="desktop adapter is Windows-only")
def test_computer_use_public_names_stay_identical_to_their_modules() -> None:
    desktop = importlib.import_module("karox.computer_use")
    adapter = importlib.import_module("karox.computer_use.adapter")
    approval = importlib.import_module("karox.computer_use.approval")
    assert desktop.ComputerUseAdapter is adapter.ComputerUseAdapter
    assert desktop.TOOL_SPECS is adapter.TOOL_SPECS
    assert desktop.ApprovalStore is approval.ApprovalStore


@pytest.mark.skipif(sys.platform != "win32", reason="desktop adapter is Windows-only")
def test_computer_use_submodule_import_path_still_works() -> None:
    from karox.computer_use import input as desktop_input

    assert desktop_input.__name__ == "karox.computer_use.input"
