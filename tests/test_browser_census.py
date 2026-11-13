"""The scene census hook: added to a three.js build only, once, and never elsewhere."""

from __future__ import annotations

from karox.browser import census
from karox.browser.contract import ACTION_CAPABILITIES, READ_ACTIONS
from karox.risk_mapping import BROWSER_ACTION_RISK_KINDS

THREE = b"class Object3D {}\nclass Scene extends Object3D {}\nexport { Scene };\n"


def test_the_hook_is_appended_to_a_three_module_build_once() -> None:
    url = "https://unpkg.com/three@0.160.0/build/three.module.js"
    once = census.instrument(url, THREE)
    assert once.startswith(THREE) and b"__karoxCensus" in once and b"onBeforeRender" in once
    assert census.instrument(url, once) == once


def test_r171_core_build_gets_the_hook_where_scene_lives() -> None:
    """r171+ three.module.js imports Scene from three.core.js; the hook goes there."""

    core = census.instrument("https://unpkg.com/three@0.186.0/build/three.core.js", THREE)
    assert b"__karoxCensus" in core
    shell = b"import { Scene } from './three.core.js';\nexport { Scene };\n"
    assert census.instrument("https://unpkg.com/three@0.186.0/build/three.module.js", shell) == shell
    assert census.instrument("https://unpkg.com/three@0.186.0/build/three.core.min.js", THREE) == THREE


def test_a_post_processed_scene_is_kept_as_a_fallback() -> None:
    assert b"__karoxThreeOffscreen" in census.HOOK_JS.encode("utf-8")
    assert "__karoxThree || globalThis.__karoxThreeOffscreen" in census.CENSUS_JS


def test_other_files_and_minified_builds_are_served_unchanged() -> None:
    assert census.instrument("https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js", THREE) == THREE
    assert census.instrument("https://unpkg.com/three@0.160.0/build/three.module.min.js", THREE) == THREE
    assert census.instrument("https://unpkg.com/three@0.160.0/build/three.module.js", b"minified") == b"minified"


def test_census_is_a_read_and_its_width_is_bounded() -> None:
    assert "census" in READ_ACTIONS and "census" in ACTION_CAPABILITIES
    assert BROWSER_ACTION_RISK_KINDS["census"] == "browser.read"
    assert census.census_width(None) == 960 and census.census_width(5000) == 960 and census.census_width("x") == 960


def test_a_drag_target_already_on_screen_needs_no_scroll() -> None:
    from karox.browser.interaction import _on_screen

    view = {"width": 1280, "height": 800}
    assert _on_screen({"x": 0, "y": 0, "width": 1280, "height": 800}, view)
    assert not _on_screen({"x": 0, "y": 900, "width": 100, "height": 100}, view)
    assert not _on_screen({"x": 0, "y": 0, "width": 10, "height": 10}, {})
