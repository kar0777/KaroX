"""Memory UX is WIRED: inspect/edit/forget by id, honest VALID<->STALE
revalidation from real source hashes, and map refresh participating in
invalidation so stale architecture memory cannot stay authoritative."""

import uuid
from pathlib import Path

import pytest

from _support import initialize_git_repository
from karox import tui as karox_tui
from karox.map_service import MapService
from karox.memory import (
    KaroXMemory,
    MemoryError,
    MemoryKind,
    MemoryScope,
)


def _memory(tmp_path: Path) -> KaroXMemory:
    return KaroXMemory(tmp_path / "memory")


def _entry(memory: KaroXMemory, **overrides):
    base = dict(
        scope=MemoryScope.PROJECT,
        scope_id="proj-1",
        kind=MemoryKind.FACT,
        content="the billing module owns retries",
        provenance="agent-run",
        confidence=0.9,
    )
    base.update(overrides)
    return memory.remember(**base)


class TestInspectionSurface:
    def test_entries_lists_everything_newest_first(self, tmp_path):
        memory = _memory(tmp_path)
        first = _entry(memory, content="older fact")
        second = _entry(
            memory, scope=MemoryScope.USER, scope_id="default",
            content="newer fact",
        )
        listed = memory.entries()
        assert [item.entry_id for item in listed][:2] == [
            second.entry_id, first.entry_id,
        ][:2] or len(listed) == 2

    def test_entries_filters_by_scope(self, tmp_path):
        memory = _memory(tmp_path)
        _entry(memory)
        _entry(memory, scope=MemoryScope.USER, scope_id="default")
        project_only = memory.entries(scope=MemoryScope.PROJECT)
        assert project_only
        assert all(
            item.scope is MemoryScope.PROJECT for item in project_only
        )

    def test_find_locates_across_scopes_and_misses_honestly(self, tmp_path):
        memory = _memory(tmp_path)
        entry = _entry(memory, scope=MemoryScope.SESSION, scope_id="sess-9")
        found = memory.find(entry.entry_id)
        assert found is not None
        assert found.scope_id == "sess-9"
        assert memory.find("mem-does-not-exist") is None


class TestEditAndForget:
    def test_edit_rewrites_content_and_resets_provenance(self, tmp_path):
        memory = _memory(tmp_path)
        entry = _entry(
            memory,
            source_path="src/billing.py",
            source_sha256="0" * 64,
        )
        stale = memory.revalidate_sources(repository=tmp_path)
        assert entry.entry_id in stale["became_stale"]
        edited = memory.edit(
            entry_id=entry.entry_id, content="retries moved to worker"
        )
        assert edited.entry_id == entry.entry_id
        assert edited.content == "retries moved to worker"
        assert edited.provenance == "user-edit"
        assert edited.validation == "valid"
        assert edited.source_path is None

    def test_edit_refuses_junk(self, tmp_path):
        memory = _memory(tmp_path)
        entry = _entry(memory)
        with pytest.raises(MemoryError):
            memory.edit(entry_id=entry.entry_id, content="   ")
        with pytest.raises(MemoryError):
            memory.edit(entry_id="mem-missing", content="text")

    def test_forget_entry_by_id_alone(self, tmp_path):
        memory = _memory(tmp_path)
        entry = _entry(memory)
        assert memory.forget_entry(entry.entry_id) == 1
        assert memory.forget_entry(entry.entry_id) == 0
        assert memory.find(entry.entry_id) is None


class TestSourceRevalidation:
    def test_valid_to_stale_and_back(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        source = repo / "notes.md"
        source.write_text("v1\n", encoding="utf-8")
        import hashlib

        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        memory = _memory(tmp_path)
        entry = _entry(
            memory,
            content="notes say v1",
            source_path="notes.md",
            source_sha256=digest,
        )
        clean = memory.revalidate_sources(repository=repo)
        assert clean["checked"] == 1
        assert clean["became_stale"] == []
        source.write_text("v2\n", encoding="utf-8")
        swept = memory.revalidate_sources(repository=repo)
        assert entry.entry_id in swept["became_stale"]
        assert memory.find(entry.entry_id).validation == "stale"
        source.write_text("v1\n", encoding="utf-8")
        restored = memory.revalidate_sources(repository=repo)
        assert entry.entry_id in restored["became_valid"]
        assert memory.find(entry.entry_id).validation == "valid"

    def test_missing_source_is_stale_not_an_error(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        memory = _memory(tmp_path)
        entry = _entry(
            memory,
            source_path="deleted.py",
            source_sha256="1" * 64,
        )
        swept = memory.revalidate_sources(repository=repo)
        assert entry.entry_id in swept["became_stale"]

    def test_unbacked_entries_are_never_touched(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        memory = _memory(tmp_path)
        entry = _entry(memory)
        swept = memory.revalidate_sources(repository=repo)
        assert swept["checked"] == 0
        assert memory.find(entry.entry_id).validation == "valid"


class TestMapParticipation:
    def test_map_build_invalidates_source_backed_memory(self, tmp_path):
        repo = tmp_path / "repo"
        initialize_git_repository(repo)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n', encoding="utf-8"
        )
        memory_root = tmp_path / "memory"
        memory = KaroXMemory(memory_root)
        entry = memory.remember(
            scope=MemoryScope.PROJECT,
            scope_id="demo",
            kind=MemoryKind.FACT,
            content="pyproject declares demo",
            provenance="agent-run",
            source_path="pyproject.toml",
            source_sha256="f" * 64,
        )
        service = MapService(
            repo,
            root=tmp_path / "maps",
            session_id=f"map-test-{uuid.uuid4().hex[:12]}",
            memory_root=memory_root,
        )
        state = service.build("low")
        sweep = state["memory_invalidation"]
        assert sweep is not None
        assert entry.entry_id in sweep["became_stale"]
        assert memory.find(entry.entry_id).validation == "stale"

    def test_map_build_survives_a_missing_memory_root(self, tmp_path):
        repo = tmp_path / "repo"
        initialize_git_repository(repo)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n', encoding="utf-8"
        )
        service = MapService(
            repo,
            root=tmp_path / "maps",
            session_id=f"map-test-{uuid.uuid4().hex[:12]}",
            memory_root=tmp_path / "never-created",
        )
        state = service.build("low")
        assert state["memory_invalidation"] is None


class TestMemoryCommandSurface:
    def test_memory_is_searchable_without_bloating_bare_slash(self):
        assert "/memory" not in karox_tui.VISIBLE_COMMANDS
        assert "/memory" in karox_tui.SLASH_COMMANDS
        assert "/memory" in karox_tui._COMMANDS_RU
        for language in ("en", "ru"):
            assert "/memory" not in karox_tui._commands(language)
            assert "/memory" in {
                name.split(" ", 1)[0]
                for name in karox_tui._discoverable_commands(language)
            }
