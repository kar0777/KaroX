"""Tests for the one-shot pre-finish 'related files' notice."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from karox6 import cli
from karox6.core import events as ev
from karox6.core import related
from karox6.core.related import (
    RELATED_FILES_NOTICE,
    extract_symbols,
    find_related_files,
    format_related_notice,
    is_production_python,
    related_files,
)
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.settings import load_settings
from karox6.tools.fs import ReadTool

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(ws: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    return proc.stdout


def make_repo(tmp_path: Path, files: dict[str, str], *, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    git(ws, "init", "-q")
    git(ws, "config", "user.email", "t@example.com")
    git(ws, "config", "user.name", "t")
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "init")
    return ws


def run(session: Session, prompt: str = "task") -> list[ev.Event]:
    async def collect() -> list[ev.Event]:
        return [e async for e in session.run(prompt)]

    return asyncio.run(collect())


def finished(events: list[ev.Event]) -> ev.RunFinished:
    assert isinstance(events[-1], ev.RunFinished)
    return events[-1]


# -- symbol extraction --------------------------------------------------------------------


def test_symbol_extraction_ast_defs_assigns_attrs_and_filters(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    mod = ws / "pkg" / "engine.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "\n".join(
            [
                "from other import ignored_import_attr",
                "DEFAULT_RETRY_CAP: int = 7",
                "",
                "class QuotaTracker:",
                "    def compute_budget(self, cfg):",
                '        """Docstring mentioning .fake_doc_attr."""',
                "        # Comment mentioning .fake_comment_attr",
                '        label = "string.fake_str_attr"',
                "        self.get(cfg.quota_window)",
                "        self.append(cfg.value)",
                "        now = time.monotonic() + os.environ.get('X', '') + dt.isoformat()",
                "        return cfg.xy + self.custom_scale",
                "",
                "def added_helper():",
                "    return 1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    diff_text = "\n".join(
        [
            "diff --git a/pkg/engine.py b/pkg/engine.py",
            "--- a/pkg/engine.py",
            "+++ b/pkg/engine.py",
            "@@ -1,3 +1,3 @@",
            "-from other import old_imp",
            "+from other import ignored_import_attr",
            "-DEFAULT_RETRY_CAP: int = 3",
            "+DEFAULT_RETRY_CAP: int = 7",
            "@@ -5,6 +5,7 @@ def compute_budget(self, cfg):",
            '+        """Docstring mentioning .fake_doc_attr."""',
            "+        # Comment mentioning .fake_comment_attr",
            '+        label = "string.fake_str_attr"',
            "+        self.get(cfg.quota_window)",
            "+        self.append(cfg.value)",
            "+        now = time.monotonic() + os.environ.get('X', '') + dt.isoformat()",
            "-        return cfg.xy + self.old_scale",
            "+        return cfg.xy + self.custom_scale",
            "@@ -12,2 +13,4 @@",
            "-def removed_helper():",
            "-    return 0",
            "+def added_helper():",
            "+    return 1",
        ]
    )

    syms = set(extract_symbols(ws, ["pkg/engine.py"], diff_text))

    # AST enclosing class + method, added & removed defs, module-level assign, code attrs
    assert "QuotaTracker" in syms
    assert "compute_budget" in syms
    assert "added_helper" in syms
    assert "removed_helper" in syms
    assert "DEFAULT_RETRY_CAP" in syms
    assert "quota_window" in syms
    assert "custom_scale" in syms
    assert "old_scale" in syms

    # Filtered out: imports, docstrings, comments, strings, short (<3), stopwords
    for excluded in (
        "ignored_import_attr",
        "fake_doc_attr",
        "fake_comment_attr",
        "fake_str_attr",
        "xy",
        "self",
        "get",
        "append",
        "value",
        "None",
        "True",
        "False",
        "str",
        "int",
        "list",
        "dict",
        "args",
        "kwargs",
        "monotonic",
        "environ",
        "isoformat",
    ):
        assert excluded not in syms


def test_symbol_extraction_falls_back_to_hunk_header_on_syntax_error(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    # Invalid Python syntax on disk -> AST fails -> falls back to hunk header symbol.
    (ws / "broken.py").write_text("def broken_func(:\n    pass\n", encoding="utf-8")
    diff_text = "\n".join(
        [
            "diff --git a/broken.py b/broken.py",
            "--- a/broken.py",
            "+++ b/broken.py",
            "@@ -10,2 +10,2 @@ def fallback_owner(x):",
            "-    return x + 1",
            "+    return x + 2",
        ]
    )
    syms = extract_symbols(ws, ["broken.py"], diff_text)
    assert "fallback_owner" in syms


# -- ranking, exclusions, fallback --------------------------------------------------------


@needs_git
def test_ranking_order_top5_whole_word_and_exclusions(tmp_path: Path) -> None:
    files = {
        "pkg/sub/core.py": (
            "def alpha_calc():\n"
            "    return 1\n\n"
            "def beta_calc():\n"
            "    return 2\n"
        ),
        # 2 distinct symbols -> must rank #1 regardless of dir depth
        "other/two_syms.py": "x = alpha_calc() + beta_calc()\n",
        # 1 symbol, depth 2 (pkg/sub), 1 occurrence -> ranks ahead of shallower files with 1 sym
        "pkg/sub/sibling.py": "y = alpha_calc()\n",
        # 1 symbol, depth 1 (pkg), 3 occurrences -> ahead of depth 1 with 1 occurrence
        "pkg/many_occ.py": "a = alpha_calc() + alpha_calc() + alpha_calc()\n",
        # 1 symbol, depth 1 (pkg), 1 occurrence -> alphabetical tiebreak: aaa before zzz
        "pkg/aaa_tie.py": "b = alpha_calc()\n",
        "pkg/zzz_tie.py": "c = alpha_calc()\n",
        # 1 symbol, depth 0 -> pushed out of top 5
        "top_sixth.py": "d = alpha_calc()\n",
        # Whole-word negative check: substring only -> must NOT match
        "pkg/sub/partial.py": "e = pre_alpha_calc() + alpha_calc_v2()\n",
        # Excluded paths: tests, docs, examples, .venv, site-packages, node_modules
        "tests/test_core.py": "alpha_calc(); beta_calc()\n",
        "pkg/sub/test_inline.py": "alpha_calc(); beta_calc()\n",
        "pkg/sub/core_test.py": "alpha_calc(); beta_calc()\n",
        "conftest.py": "alpha_calc(); beta_calc()\n",
        "docs/guide.py": "alpha_calc(); beta_calc()\n",
        "examples/demo.py": "alpha_calc(); beta_calc()\n",
        ".venv/lib/mod.py": "alpha_calc(); beta_calc()\n",
        "vendor/site-packages/mod.py": "alpha_calc(); beta_calc()\n",
        "node_modules/pkg/mod.py": "alpha_calc(); beta_calc()\n",
    }
    ws = make_repo(tmp_path, files)
    (ws / "pkg/sub/core.py").write_text(
        "def alpha_calc():\n    return 10\n\ndef beta_calc():\n    return 20\n",
        encoding="utf-8",
    )
    diff_text = git(ws, "diff", "HEAD")

    expected = [
        ("other/two_syms.py", ["alpha_calc", "beta_calc"]),
        ("pkg/sub/sibling.py", ["alpha_calc"]),
        ("pkg/many_occ.py", ["alpha_calc"]),
        ("pkg/aaa_tie.py", ["alpha_calc"]),
        ("pkg/zzz_tie.py", ["alpha_calc"]),
    ]

    via_git = related_files(ws, ["pkg/sub/core.py"], diff_text)
    assert via_git == expected


def test_exclusions_outside_workspace_and_notice_format(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    assert not is_production_python("../outside.py", ws)
    assert not is_production_python(str(outside), ws)
    assert not is_production_python(".git/hooks/pre.py", ws)
    assert not is_production_python(".venv/foo.py", ws)
    assert not is_production_python("a/site-packages/foo.py", ws)
    assert not is_production_python("node_modules/foo.py", ws)
    assert not is_production_python("docs/foo.py", ws)
    assert not is_production_python("examples/foo.py", ws)
    assert not is_production_python("tests/foo.py", ws)
    assert not is_production_python("pkg/readme.md", ws)
    assert is_production_python("pkg/module.py", ws)

    notice = format_related_notice(
        [
            ("pkg/a.py", ["s1", "s2", "s3", "s4", "s5"]),
            ("pkg/b.py", ["only_one"]),
        ]
    )
    expected = RELATED_FILES_NOTICE.format(
        lines="- pkg/a.py (s1, s2, s3, s4)\n- pkg/b.py (only_one)"
    )
    assert notice == expected
    assert notice.startswith(
        "Before you finish: these files reference names you changed but you did not edit them:\n"
        "- pkg/a.py (s1, s2, s3, s4)\n"
        "- pkg/b.py (only_one)\n"
        "Check whether any of them needs the same change for the task "
        "(another implementation, a caller, a sibling class). "
        "If none does, finish with your summary."
    )


def test_parse_diff_size_cap_and_deadline_and_ast_bisect(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    # Large file with many functions; change only line inside func_450.
    lines = []
    for i in range(500):
        lines.append(f"def func_{i}():")
        lines.append(f"    return {i}")
    src = "\n".join(lines) + "\n"
    (ws / "many.py").write_text(src, encoding="utf-8")
    target_line = 450 * 2 + 2  # body line of func_450
    diff_text = (
        "diff --git a/many.py b/many.py\n"
        "--- a/many.py\n"
        "+++ b/many.py\n"
        f"@@ -{target_line},1 +{target_line},1 @@\n"
        "-    return 449\n"
        "+    return 450\n"
    )
    syms = extract_symbols(ws, ["many.py"], diff_text)
    assert syms == ["func_450"]

    # Expired deadline returns [] immediately.
    assert extract_symbols(ws, ["many.py"], diff_text, deadline=time.monotonic() - 1.0) == []

    # Size cap truncates diff beyond max_chars.
    padded_diff = (
        "diff --git a/many.py b/many.py\n"
        "--- a/many.py\n"
        "+++ b/many.py\n"
        "@@ -1,2 +1,2 @@\n"
        "+def first_kept():\n"
        + ("# " + "x" * 100 + "\n") * 20
        + "+def cut_off_later():\n"
    )
    _, prod = related._parse_diff(padded_diff, {"many.py"}, max_chars=250)
    assert "first_kept" in prod["many.py"].def_class_syms
    assert "cut_off_later" not in prod["many.py"].def_class_syms


# -- Session integration ------------------------------------------------------------------


@needs_git
def test_session_notice_fires_once_at_first_finish_then_completes(tmp_path: Path) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/primary.py": "def format_price(amount):\n    return f'${amount}'\n",
            "pkg/secondary.py": (
                "from pkg.primary import format_price\n\n"
                "def render_receipt(amount):\n"
                "    return format_price(amount)\n"
            ),
            "tests/test_price.py": "from pkg.primary import format_price\nassert format_price(1)\n",
        },
    )
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/primary.py",
                            "old_string": "return f'${amount}'",
                            "new_string": "return f'${amount:.2f}'",
                        },
                    )
                ]
            ),
            Turn(text="initial summary"),
            # Even if the model edits again after the notice, it must never fire a second time.
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/primary.py",
                            "old_string": "return f'${amount:.2f}'",
                            "new_string": "return f'USD {amount:.2f}'",
                        },
                    )
                ]
            ),
            Turn(text="final summary after check"),
        ]
    )
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home")
    assert cfg.related_files is True
    events = run(Session(cfg, provider))
    done = finished(events)

    assert done.status == "completed"
    assert done.final_text == "final summary after check"
    assert len(provider.requests) == 4

    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "related_files"]
    assert len(notices) == 1
    expected_text = (
        "Before you finish: these files reference names you changed but you did not edit them:\n"
        "- pkg/secondary.py (format_price)\n"
        "Check whether any of them needs the same change for the task "
        "(another implementation, a caller, a sibling class). "
        "If none does, finish with your summary."
    )
    assert notices[0].text == expected_text
    assert "tests/test_price.py" not in notices[0].text
    assert provider.requests[2].messages[-1].role == "user"
    assert provider.requests[2].messages[-1].content == expected_text


@needs_git
def test_session_not_fired_when_no_related_files(tmp_path: Path) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/isolated.py": "def unique_helper():\n    return 1\n",
            "pkg/unrelated.py": "def something_else():\n    return 2\n",
            "tests/test_isolated.py": "from pkg.isolated import unique_helper\n",
        },
    )
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/isolated.py",
                            "old_string": "return 1",
                            "new_string": "return 42",
                        },
                    )
                ]
            ),
            Turn(text="all done"),
        ]
    )
    events = run(Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "home"), provider))
    done = finished(events)
    assert done.status == "completed"
    assert done.final_text == "all done"
    assert len(provider.requests) == 2
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "related_files"]


@needs_git
def test_session_not_fired_when_disabled_via_flag_or_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_HOME", str(tmp_path / "cfg_home"))
    (tmp_path / "cfg_home").mkdir()

    # CLI flag defaults on, --no-related-files turns it off
    default_args = cli._parser().parse_args(["run", "--model", "m", "task"])
    default_cfg = cli._session_config(
        default_args, tmp_path, tmp_path / "h0", stream_deltas=False
    )
    assert default_cfg.related_files is True

    off_args = cli._parser().parse_args(["run", "--model", "m", "--no-related-files", "task"])
    off_cfg = cli._session_config(off_args, tmp_path, tmp_path / "h1", stream_deltas=False)
    assert off_cfg.related_files is False

    # config.toml setting works and CLI overrides it
    (tmp_path / "cfg_home" / "config.toml").write_text("related_files = false\n", encoding="utf-8")
    settings = load_settings()
    assert settings == {"related_files": False}
    cfg_args = cli._parser(settings).parse_args(["run", "--model", "m", "task"])
    assert cfg_args.related_files is False
    over_args = cli._parser(settings).parse_args(
        ["run", "--model", "m", "--related-files", "task"]
    )
    assert over_args.related_files is True

    # Run with --no-related-files config: must NOT fire even though a sibling references the symbol
    ws = make_repo(
        tmp_path,
        {
            "pkg/a.py": "def compute_tax(x):\n    return x * 0.1\n",
            "pkg/b.py": "from pkg.a import compute_tax\nval = compute_tax(5)\n",
        },
    )
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/a.py",
                            "old_string": "return x * 0.1",
                            "new_string": "return x * 0.2",
                        },
                    )
                ]
            ),
            Turn(text="done without notice"),
        ]
    )
    run_cfg = cli._session_config(off_args, ws, tmp_path / "h2", stream_deltas=False)
    events = run(Session(run_cfg, provider))
    assert finished(events).final_text == "done without notice"
    assert len(provider.requests) == 2
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "related_files"]


@needs_git
def test_session_works_for_edits_made_via_bash_command(tmp_path: Path) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/source.py": "def parse_invoice(raw):\n    return raw.strip()\n",
            "pkg/caller.py": (
                "from pkg.source import parse_invoice\n\n"
                "def handle_upload(body):\n"
                "    return parse_invoice(body)\n"
            ),
        },
    )
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home")
    session = Session(cfg, ScriptedProvider([]))
    shell_tool = session.shell.kind
    py_exe = Path(sys.executable).as_posix()
    cmd = (
        f'"{py_exe}" -c "from pathlib import Path; '
        "p = Path('pkg/source.py'); "
        "p.write_text(p.read_text().replace('strip()', 'upper()'))\""
    )
    provider = ScriptedProvider(
        [
            Turn(calls=[(shell_tool, {"command": cmd})]),
            Turn(text="done after bash edit"),
            Turn(text="verified caller"),
        ]
    )
    session = Session(cfg, provider)
    events = run(session)
    done = finished(events)
    assert done.status == "completed"
    assert done.final_text == "verified caller"
    assert len(provider.requests) == 3
    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "related_files"]
    assert len(notices) == 1
    assert "- pkg/caller.py (parse_invoice)" in notices[0].text


@needs_git
def test_session_respects_finish_gate_order_and_skips_read_only_and_review(tmp_path: Path) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/a.py": "def sync_cache():\n    return 1\n",
            "pkg/b.py": "from pkg.a import sync_cache\nx = sync_cache()\n",
        },
    )
    # 1. Both related_files and finish_gate enabled: related_files fires first, then finish_gate.
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/a.py",
                            "old_string": "return 1",
                            "new_string": "return 2",
                        },
                    )
                ]
            ),
            Turn(text="first finish attempt"),
            Turn(text="second finish attempt"),
            Turn(text="final verified answer"),
        ]
    )
    cfg = SessionConfig(
        model="m",
        workspace=ws,
        home=tmp_path / "h1",
        related_files=True,
        finish_gate=True,
    )
    events = run(Session(cfg, provider))
    done = finished(events)
    assert done.status == "completed"
    assert done.final_text == "final verified answer"
    notice_kinds = [e.kind for e in events if isinstance(e, ev.Notice)]
    assert notice_kinds == ["related_files", "finish_gate"]

    # 2. Read-only tool session never fires related_files.
    ro_provider = ScriptedProvider([Turn(text="ro answer")])
    ro_events = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h2", related_files=True),
            ro_provider,
            tools=[ReadTool()],
        )
    )
    assert finished(ro_events).final_text == "ro answer"
    assert not [e for e in ro_events if isinstance(e, ev.Notice) and e.kind == "related_files"]

    # 3. Review mode never fires related_files.
    rev_provider = ScriptedProvider([Turn(text="review mode done")])
    rev_events = run(
        Session(
            SessionConfig(
                model="m",
                workspace=ws,
                home=tmp_path / "h3",
                review=True,
                related_files=True,
            ),
            rev_provider,
        )
    )
    assert finished(rev_events).final_text == "review mode done"
    assert not [e for e in rev_events if isinstance(e, ev.Notice) and e.kind == "related_files"]


@needs_git
def test_timeout_or_error_emits_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/a.py": "def compute_total():\n    return 1\n",
            "pkg/b.py": "from pkg.a import compute_total\ncompute_total()\n",
        },
    )
    (ws / "pkg/a.py").write_text("def compute_total():\n    return 2\n", encoding="utf-8")

    # Simulate timeout in git helper -> returns []
    def slow_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(["git"], 0.01)

    monkeypatch.setattr(related, "_run_git", slow_git)
    t0 = time.monotonic()
    assert find_related_files(ws, ["pkg/a.py"], timeout_s=0.1) == []
    assert time.monotonic() - t0 < 1.0


@needs_git
def test_untracked_new_file_and_session_error_resilience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/existing.py": "def dispatch_event(ev):\n    return ev\n",
        },
    )
    # New untracked file referencing an attribute/function from existing file
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "write",
                        {
                            "file_path": "pkg/new_handler.py",
                            "content": "def handle_all(bus):\n    return bus.dispatch_event(1)\n",
                        },
                    )
                ]
            ),
            Turn(text="initial"),
            Turn(text="final"),
        ]
    )
    events = run(Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "h1"), provider))
    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "related_files"]
    assert len(notices) == 1
    assert "- pkg/existing.py (dispatch_event)" in notices[0].text
    assert finished(events).final_text == "final"

    # Unexpected exception from find_related_files in session -> no notice, run completes cleanly
    from karox6.core import session as session_mod

    def boom(*args: object, **kwargs: object) -> list[tuple[str, list[str]]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(session_mod, "find_related_files", boom)
    provider2 = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/existing.py",
                            "old_string": "return ev",
                            "new_string": "return ev or None",
                        },
                    )
                ]
            ),
            Turn(text="clean finish despite error"),
        ]
    )
    events2 = run(Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "h2"), provider2))
    assert finished(events2).status == "completed"
    assert finished(events2).final_text == "clean finish despite error"
    assert not [e for e in events2 if isinstance(e, ev.Notice) and e.kind == "related_files"]


@needs_git
def test_tracked_clean_file_no_synthetic_diff(tmp_path: Path) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/committed.py": "def already_committed():\n    return 1\n",
            "pkg/caller.py": "from pkg.committed import already_committed\nalready_committed()\n",
        },
    )
    # File is tracked and clean vs HEAD (e.g. agent committed its own edit):
    # must NOT generate a synthetic "new file" diff or surface callers.
    assert find_related_files(ws, ["pkg/committed.py"]) == []


@needs_git
def test_subdirectory_workspace_inside_bigger_repo(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "other_app/external.py": "from sub.pkg.core import compute_fee\ncompute_fee()\n",
            "sub/pkg/core.py": "def compute_fee():\n    return 1\n",
            "sub/pkg/sibling.py": "from pkg.core import compute_fee\nval = compute_fee()\n",
        },
        name="mono",
    )
    sub_ws = repo / "sub"
    (sub_ws / "pkg/core.py").write_text("def compute_fee():\n    return 99\n", encoding="utf-8")

    # Direct call with workspace-relative path returns workspace-relative result only.
    res = find_related_files(sub_ws, ["pkg/core.py"])
    assert res == [("pkg/sibling.py", ["compute_fee"])]

    # Session rooted at the subdirectory also resolves paths relative to sub_ws.
    (sub_ws / "pkg/core.py").write_text("def compute_fee():\n    return 1\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "pkg/core.py",
                            "old_string": "return 1",
                            "new_string": "return 5",
                        },
                    )
                ]
            ),
            Turn(text="initial"),
            Turn(text="final"),
        ]
    )
    events = run(Session(SessionConfig(model="m", workspace=sub_ws, home=tmp_path / "h"), provider))
    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "related_files"]
    assert len(notices) == 1
    assert "- pkg/sibling.py (compute_fee)" in notices[0].text
    assert "other_app/external.py" not in notices[0].text


@needs_git
def test_paths_with_spaces_and_non_ascii(tmp_path: Path) -> None:
    tracked_a = "pkg/my dir/модуль один.py"
    tracked_b = "pkg/my dir/модуль два.py"
    untracked_c = "pkg/my dir/новый файл.py"
    ws = make_repo(
        tmp_path,
        {
            tracked_a: "def calc_tariff(x):\n    return x + 1\n",
            tracked_b: "def run_tariff(x):\n    return calc_tariff(x) + helper_beta(x)\n",
        },
    )
    # 1. Tracked file with spaces + Cyrillic modified
    (ws / tracked_a).write_text("def calc_tariff(x):\n    return x + 2\n", encoding="utf-8")
    res = find_related_files(ws, [tracked_a])
    assert res == [(tracked_b, ["calc_tariff"])]

    # 2. Untracked file with spaces + Cyrillic referencing symbol in tracked_b
    (ws / untracked_c).write_text("def helper_beta(x):\n    return x * 3\n", encoding="utf-8")
    res_untracked = find_related_files(ws, [untracked_c])
    assert res_untracked == [(tracked_b, ["helper_beta"])]


@needs_git
def test_git_grep_caches_is_production_python_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_repo(
        tmp_path,
        {
            "pkg/a.py": "def repeated_sym():\n    return 1\n",
            "pkg/b.py": "\n".join("repeated_sym()" for _ in range(25)) + "\n",
        },
    )
    (ws / "pkg/a.py").write_text("def repeated_sym():\n    return 2\n", encoding="utf-8")

    orig = related.is_production_python
    calls: list[str] = []

    def counting(rel_path: str, *args: object, **kwargs: object) -> bool:
        calls.append(rel_path)
        return orig(rel_path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(related, "is_production_python", counting)
    res = find_related_files(ws, ["pkg/a.py"])
    assert res == [("pkg/b.py", ["repeated_sym"])]
    assert calls.count("pkg/b.py") == 1


@needs_git
def test_session_changes_since_start_error_and_timeout_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox6.core import loopguard as lg_mod
    from karox6.core import session as session_mod

    ws = make_repo(
        tmp_path,
        {
            "pkg/a.py": "def compute_rate():\n    return 1\n",
            "pkg/b.py": "from pkg.a import compute_rate\ncompute_rate()\n",
        },
    )

    # 1. Error in changes_since_start -> caught, no notice, run completes cleanly.
    def failing_changes(self: object) -> None:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(lg_mod.WorkspaceMonitor, "changes_since_start", failing_changes)
    provider1 = ScriptedProvider([Turn(text="done despite changes error")])
    events1 = run(
        Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "h1"), provider1)
    )
    assert finished(events1).status == "completed"
    assert finished(events1).final_text == "done despite changes error"
    assert not [e for e in events1 if isinstance(e, ev.Notice) and e.kind == "related_files"]

    # 2. Slow changes_since_start exceeding timeout budget -> caught, no notice.
    monkeypatch.setattr(session_mod, "RELATED_TIMEOUT_S", 0.05)

    def slow_changes(self: object) -> None:
        time.sleep(0.8)

    monkeypatch.setattr(lg_mod.WorkspaceMonitor, "changes_since_start", slow_changes)
    provider2 = ScriptedProvider([Turn(text="done despite slow changes")])
    t0 = time.monotonic()
    events2 = run(
        Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "h2"), provider2)
    )
    elapsed = time.monotonic() - t0
    assert finished(events2).status == "completed"
    assert finished(events2).final_text == "done despite slow changes"
    assert not [e for e in events2 if isinstance(e, ev.Notice) and e.kind == "related_files"]
    assert elapsed < 1.5

