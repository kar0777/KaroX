"""Static checks a browser refuses on before the page runs."""

from __future__ import annotations

from pathlib import Path

from karox.web_check import check_page, default_pages, diagnostic_pages, is_scratch_page


def _page(tmp_path: Path, body: str) -> Path:
    page = tmp_path / "scene.html"
    page.write_text(
        "<!doctype html><html><body>" + body + "</body></html>",
        encoding="utf-8",
    )
    return page


def _duplicate_lines(page: Path) -> list[str]:
    return [item for item in check_page(page) if "more than once at top level" in item]


def test_a_second_top_level_declaration_is_named(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "<script type='module'>const FISH_L = 0.9;"
        "function body() { return FISH_L; }"
        "const FISH_L = 1.1;</script>",
    )

    problems = _duplicate_lines(page)

    assert len(problems) == 1
    assert "FISH_L" in problems[0]
    assert "before the page runs" in problems[0]


def test_shadowing_inside_two_functions_is_legal(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "<script type='module'>const FISH_L = 0.9;"
        "function a() { const FISH_L = 1; return FISH_L; }"
        "function b() { const FISH_L = 2; return FISH_L; }</script>",
    )

    assert _duplicate_lines(page) == []


def test_names_in_strings_and_comments_are_not_declarations(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "<script type='module'>"
        "const note = 'const FISH_L = 1;';\n"
        "// const FISH_L = 2;\n"
        "/* const FISH_L = 3; */\n"
        "const FISH_L = 0.9;</script>",
    )

    assert _duplicate_lines(page) == []


def test_an_external_script_is_not_read(tmp_path: Path) -> None:
    page = _page(tmp_path, "<script src='scene.js'></script>")

    assert check_page(page) == []


def test_a_single_declaration_of_each_name_passes(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "<script type='module'>const a = 1; let b = 2; var c = 3;"
        "function f() {} class G {}</script>",
    )

    assert _duplicate_lines(page) == []


def test_scratch_probes_are_not_deliverable_pages(tmp_path: Path) -> None:
    """A diagnostic page must not outrank or represent the deliverable.

    The 3D run left ``_probe.html`` next to ``index.html``. Sorting every root
    page put the probe first, so the final visual check judged it and its
    failure stayed owed against a correct game.
    """

    for name in ("index.html", "about.html", "_probe.html", "probe2.html", "scratch.html"):
        (tmp_path / name).write_text("<html></html>", encoding="utf-8")

    assert is_scratch_page("_probe.html")
    assert is_scratch_page("probe2.html")
    assert is_scratch_page("my-scratch-page.html")
    assert not is_scratch_page("index.html")
    assert not is_scratch_page("about.html")
    assert [page.name for page in default_pages(tmp_path)] == [
        "index.html",
        "about.html",
    ]

def test_a_created_page_nothing_delivers_is_diagnostic(tmp_path: Path) -> None:
    """A run's own harness page is not part of what the user opens.

    A measured 3D run wrote ``selftest.html`` (driving the game's hooks) and
    ``layoutprobe.html`` next to ``index.html``. Neither name has a probe
    token, so each harness edit made the game's proof stale.
    """

    (tmp_path / "index.html").write_text(
        '<script type="module" src="app.js"></script><a href="help.html">help</a>',
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("import './hud.js';", encoding="utf-8")
    (tmp_path / "help.html").write_text('<a href="faq.html">faq</a>', encoding="utf-8")
    (tmp_path / "faq.html").write_text("<p>faq</p>", encoding="utf-8")
    (tmp_path / "selftest.html").write_text(
        '<script type="module">import "./app.js"; // see layoutprobe.html</script>',
        encoding="utf-8",
    )
    (tmp_path / "layoutprobe.html").write_text("<p>layout</p>", encoding="utf-8")
    (tmp_path / "settings.html").write_text("<p>settings</p>", encoding="utf-8")
    created = ["selftest.html", "layoutprobe.html", "help.html", "faq.html", "settings.html", "app.js"]

    diagnostic = diagnostic_pages(tmp_path, created, "Add a settings.html page to the game.")

    # Linked from the deliverable (directly or through a linked page) or named
    # by the task: delivered. Referenced only by another harness page: not.
    assert diagnostic == {"selftest.html", "layoutprobe.html"}


def test_diagnostic_pages_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "game.html").write_text("<canvas></canvas>", encoding="utf-8")
    (tmp_path / "selftest.html").write_text("<p>harness</p>", encoding="utf-8")
    # The created pages are the only pages: they are the deliverable.
    assert diagnostic_pages(tmp_path, ["game.html", "selftest.html"]) == set()
    # A page that existed before the run is never the run's instrument.
    (tmp_path / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
    assert diagnostic_pages(tmp_path, ["index.html"]) == set()
    assert diagnostic_pages(tmp_path, []) == set()
    # A missing repository marks nothing.
    assert diagnostic_pages(tmp_path / "missing", ["selftest.html"]) == set()
