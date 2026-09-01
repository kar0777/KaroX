"""The Project Map is WIRED: levels are the effort contract, refresh is
incremental and measured, previews are honest, and the agent runtime gets a
bounded digest instead of a project dump."""

import json
import uuid
from pathlib import Path

import pytest

from _support import initialize_git_repository
from karox import tui as karox_tui
from karox.effort import EFFORT_LEVELS, budget_for
from karox.map_service import (
    MAP_LEVELS,
    MapService,
    default_map_level,
    level_contract,
    normalize_map_level,
    render_preview,
    render_status,
    stored_map_digest,
)
from karox import project_context as karox_project_context


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    initialize_git_repository(repo)
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[build-system]\nrequires = ["setuptools"]\n',
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "auth.py").write_text(
        "def login(user):\n    return bool(user)\n", encoding="utf-8"
    )
    (repo / "src" / "pkg" / "billing.py").write_text(
        "from . import auth\n\ndef charge(user):\n    return auth.login(user)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_auth.py").write_text(
        "from src.pkg import auth\n\ndef test_login():\n    assert auth.login('u')\n",
        encoding="utf-8",
    )
    return repo


def _service(tmp_path: Path, repo: Path) -> MapService:
    return MapService(
        repo,
        root=tmp_path / "maps",
        session_id=f"map-test-{uuid.uuid4().hex[:12]}",
    )


class TestLevelContract:
    def test_map_levels_are_the_effort_ladder(self):
        assert MAP_LEVELS == EFFORT_LEVELS

    def test_aliases_normalize_and_auto_is_refused(self):
        assert normalize_map_level("xhigh") == "extra-high"
        with pytest.raises(ValueError):
            normalize_map_level("auto")
        with pytest.raises(ValueError):
            normalize_map_level("turbo")

    def test_contract_derives_from_the_budget_table(self):
        for level in MAP_LEVELS:
            contract = level_contract(level)
            budget = budget_for(level)
            assert contract.git_history_commits == budget.git_history_depth
            assert (contract.git_history_commits > 0) == (
                budget.git_history_depth > 0
            )

    def test_depth_grows_with_the_ladder_not_vibes(self):
        assert level_contract("low").engine_depth is None
        assert level_contract("medium").engine_depth == "focused"
        assert level_contract("high").engine_depth == "standard"
        assert level_contract("extra-high").engine_depth == "deep"
        ultra = level_contract("ultra")
        assert ultra.engine_depth == "deep"
        assert ultra.passes == 2
        assert ultra.validates_sources
        assert not level_contract("medium").validates_sources

    def test_default_level_comes_from_the_effort_budget(self):
        for level in EFFORT_LEVELS:
            assert default_map_level(level) == budget_for(level).map_depth
        assert default_map_level("auto") == "medium"
        assert default_map_level(None) == "medium"
        assert default_map_level("nonsense") == "medium"


class TestBuildAndRefresh:
    def test_low_builds_a_durable_structural_map(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        state = service.build("low")
        assert state["level"] == "low"
        assert state["files_scanned"] > 0
        assert state["inspection"] is None
        assert service.load() is not None
        status = service.status()
        assert status["exists"] is True
        assert status["level"] == "low"

    def test_medium_adds_semantic_inspection(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        state = service.build("medium", goal="authentication login flow")
        inspection = state["inspection"]
        assert inspection is not None
        assert inspection["depth"] == "focused"
        assert isinstance(inspection["metrics"], dict)

    def test_unchanged_rebuild_is_warm_and_measured(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        first = service.build("low")
        assert first["warm"] is False
        second = service.build("low")
        assert second["warm"] is True
        bucket = second["measurements"]["low"]
        assert bucket["cold_ms"]
        assert bucket["warm_ms"]

    def test_source_change_flips_validation_to_stale(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("low")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo-renamed"\n', encoding="utf-8"
        )
        sources = service.status()["sources"]
        assert sources["state"] == "STALE"
        assert "pyproject.toml" in sources["stale"]

    def test_validation_recorded_at_extra_high(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        state = service.build("extra-high", goal="billing charge flow")
        validation = state["validation"]
        assert validation is not None
        assert validation["state"] in {"VALID", "STALE"}
        assert state["contract"]["validates_sources"] is True


class TestPreviewHonesty:
    def test_unmeasured_preview_says_estimate(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        preview = service.preview("ultra")
        duration = preview["duration_range"]
        assert duration["basis"] == "estimate"
        low, high = duration["seconds"]
        assert low < high
        assert preview["semantic_analysis"] is True
        assert preview["git_history"] is True
        assert preview["git_history_commits"] == budget_for("ultra").git_history_depth

    def test_measured_preview_uses_real_history(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("low")
        preview = service.preview("low")
        assert preview["duration_range"]["basis"] == "measured"
        assert preview["semantic_analysis"] is False
        assert preview["git_history"] is False

    def test_renderers_are_plain_text_in_both_languages(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("low")
        for language in ("en", "ru"):
            status_text = render_status(service.status(), language)
            preview_text = render_preview(service.preview("medium"), language)
            assert status_text.strip()
            assert preview_text.strip()
            assert "[" not in status_text.split("\n")[0][:4]


class TestAgentRuntimeFeeding:
    def test_digest_is_bounded_not_a_dump(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("medium", goal="authentication login flow")
        digest = service.digest(budget_chars=1200)
        assert digest
        assert len(digest) <= 1200

    def test_stored_digest_reports_freshness_honestly(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("low")
        result = stored_map_digest(repo, root=tmp_path / "maps")
        assert result is not None
        text, meta = result
        assert "Stored project map" in text
        assert meta["enabled"] is True
        assert meta["fresh"] is True
        (repo / "src" / "pkg" / "new_module.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        stale_text, stale_meta = stored_map_digest(
            repo, root=tmp_path / "maps"
        )
        assert stale_meta["fresh"] is False
        assert "stale revision" in stale_text

    def test_missing_map_yields_none_not_noise(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert stored_map_digest(repo, root=tmp_path / "maps") is None

    def test_project_context_injects_the_stored_map(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("medium", goal="authentication login flow")
        monkeypatch.setattr(
            karox_project_context,
            "stored_map_digest",
            lambda root: stored_map_digest(root, root=tmp_path / "maps"),
        )
        context = karox_project_context.discover_project_context(
            repo,
            goal="fix the login bug",
            session_id=f"ctx-{uuid.uuid4().hex[:8]}",
        )
        assert "Stored project map" in context.project_map
        store_meta = context.project_map_metadata["map_store"]
        assert store_meta["enabled"] is True
        assert len(context.project_map) < 20_000

    def test_context_survives_a_broken_map_store(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        def broken(root):
            raise RuntimeError("map store exploded")

        monkeypatch.setattr(
            karox_project_context, "stored_map_digest", broken
        )
        context = karox_project_context.discover_project_context(
            repo,
            goal="fix the login bug",
            session_id=f"ctx-{uuid.uuid4().hex[:8]}",
        )
        assert "map_store" not in context.project_map_metadata


class TestMapCommandSurface:
    def test_map_is_a_first_class_karox_command(self):
        assert "/map" in karox_tui.VISIBLE_COMMANDS
        assert "/map" in karox_tui.SLASH_COMMANDS
        assert "/map" in karox_tui._COMMANDS_RU
        for language in ("en", "ru"):
            assert "/map" in karox_tui._commands(language)


class TestStatePersistence:
    def test_state_file_is_json_and_repository_scoped(self, tmp_path):
        repo = _make_repo(tmp_path)
        service = _service(tmp_path, repo)
        service.build("low")
        payload = json.loads(service.state_path.read_text(encoding="utf-8"))
        assert payload["repository"] == str(repo.resolve())
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        foreign = MapService(
            other,
            root=tmp_path / "maps",
            session_id=f"map-test-{uuid.uuid4().hex[:12]}",
        )
        assert foreign.load() is None
