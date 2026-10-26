"""Smoke coverage for the kernel modules restored in the 6.x line.

Every test here runs offline: no provider, no network and no real model. The
point is that the modules import inside the release tree and that their pure
contracts still hold after extraction from v5.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from karox import (
    desktop_apps,
    effort_signals,
    image_attachments,
    map_service,
    memory,
    mode_artifacts,
    project_context,
    project_map,
    routing,
    unified_patch,
    workspace_transaction,
    workspace_worker,
)
from karox.effort import recommend_effort
from karox.hot_worker import hot_worker_supervisor
from karox.models import EvidenceRecord
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
)
from karox.registry import (
    ModelPricing,
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
)


class FakeRuntime:
    """The subset of ``CoreRuntime`` the workspace transaction touches."""

    MAX_FILE_BYTES = 2_000_000
    MAX_PROCESS_TIMEOUT_SECONDS = 3600.0

    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()

    def safe_path(self, relative: str, for_write: bool = False) -> Path:
        del for_write
        candidate = (self.repository / relative).resolve()
        candidate.relative_to(self.repository)
        return candidate

    @staticmethod
    def _file_sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _run(
        self, argv: list[str], timeout: float, *, keep_full: bool = False
    ) -> dict[str, object]:
        del timeout, keep_full
        return {
            "argv": list(argv),
            "exit_code": 0,
            "timed_out": False,
        }


def test_every_restored_module_imports() -> None:
    assert workspace_transaction.MAX_BATCH_OPERATIONS > 0
    assert workspace_worker.WORKER_PROTOCOL_VERSION == 1
    assert unified_patch.MAX_PATCH_BYTES > 0
    assert effort_signals.derive_task_signals
    assert image_attachments.load_image_attachments
    assert mode_artifacts.PLAN_SECTIONS
    assert memory.KaroXMemory
    assert project_map.ProjectFactMap
    assert map_service.MapService
    assert project_context.discover_project_context
    assert routing.RoutedProvider


def test_hot_worker_group_loads_and_validates_entry_points() -> None:
    module = hot_worker_supervisor().module()
    for name in (
        "validate_repo_command",
        "execute_repo_command",
        "execute_tests",
    ):
        assert callable(getattr(module, name))
    assert hot_worker_supervisor().status()["generation"] >= 1


def test_desktop_shim_refuses_with_typed_error() -> None:
    with pytest.raises(workspace_worker.InvalidCommand):
        desktop_apps.execute_desktop_app_action(None, "app.discover", {}, 1.0)


def test_workspace_transaction_commits_and_checks_expected_hash(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    transaction = workspace_transaction.WorkspaceTransaction(runtime)
    transaction.stage_write("pkg/hello.txt", "hello\n")
    result = transaction.commit()

    assert result["changed_files"] == ["pkg/hello.txt"]
    assert (tmp_path / "pkg" / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    evidence = result["_evidence"]
    assert isinstance(evidence[0], EvidenceRecord)
    assert evidence[0].kind == "repo_transaction"

    replay = workspace_transaction.WorkspaceTransaction(runtime)
    with pytest.raises(workspace_worker.InvalidCommand):
        replay.stage_write(
            "pkg/hello.txt",
            "other\n",
            expected_sha256="0" * 64,
        )


def test_workspace_worker_apply_patch_dry_run_and_batch(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    patch = "--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1,1 @@\n+hi\n"
    preview = workspace_worker.execute_repo_command(
        runtime,
        {"action": "apply_patch", "payload": {"patch": patch, "dry_run": True}},
        10.0,
    )
    assert preview["dry_run"] is True
    assert preview["operation_count"] == 1

    applied = workspace_worker.execute_repo_command(
        runtime,
        {
            "action": "batch",
            "payload": {
                "operations": [
                    {"op": "write", "path": "a.txt", "content": "alpha"},
                    {"op": "mkdir", "path": "nested"},
                ]
            },
        },
        10.0,
    )
    assert applied["action"] == "batch"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (tmp_path / "nested").is_dir()


def test_workspace_worker_selects_pytest_targets_and_splits(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        (tests / name).write_text("def test_ok():\n    pass\n", encoding="utf-8")

    focused = workspace_worker.execute_tests(
        runtime,
        {"suite": "focused", "targets": ["tests/test_a.py::test_ok"]},
        60.0,
    )
    assert focused["targets"] == ["tests/test_a.py::test_ok"]
    # tests.run is model-chosen execution, never verification evidence:
    # only a user-approved checks.run argv may prove a change.
    assert "verification_eligible" not in focused
    assert focused["_evidence"][0].kind == "test_run"

    split = workspace_worker.execute_tests(
        runtime, {"suite": "split", "split": 2, "part": 1}, 60.0
    )
    assert split["split"] == 2 and split["part"] == 1
    assert split["target_count"] == 2

    with pytest.raises(workspace_worker.InvalidCommand):
        workspace_worker.execute_tests(
            runtime, {"suite": "focused", "targets": ["tests/../README.md"]}, 60.0
        )


def test_unified_patch_hunk_application() -> None:
    patch = (
        "--- a/hello.txt\n+++ b/hello.txt\n"
        "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
    )
    files = unified_patch.parse_unified_patch(patch)
    assert files[0].old_path == "hello.txt"
    assert unified_patch.apply_hunks(
        "one\ntwo\n", files[0].hunks, path="hello.txt"
    ) == "one\nTWO\n"


def test_memory_roundtrip_and_secret_refusal(tmp_path: Path) -> None:
    store = memory.KaroXMemory(tmp_path / "memory")
    entry = store.remember(
        scope=memory.MemoryScope.USER,
        kind=memory.MemoryKind.FACT,
        content="The user prefers dark mode",
        key="theme",
    )
    recalled = store.recall(
        query="which theme does the user prefer?",
        scopes=[(memory.MemoryScope.USER, "default")],
    )
    assert [item.entry_id for item in recalled] == [entry.entry_id]

    assert store.forget(
        scope=memory.MemoryScope.USER, entry_id=entry.entry_id
    ) == 1
    assert store.entries() == ()

    with pytest.raises(memory.MemoryPolicyError):
        store.remember(
            scope=memory.MemoryScope.USER,
            kind=memory.MemoryKind.NOTE,
            content="api_key=sk-abcdef1234567890",
        )


def test_image_attachment_loads_png_and_rejects_outside_path(tmp_path: Path) -> None:
    png = tmp_path / "pixel.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    loaded = image_attachments.load_image_attachments(tmp_path, ["pixel.png"])
    assert len(loaded) == 1
    assert loaded[0].mime == "image/png"
    assert loaded[0].image.data.startswith(b"\x89PNG")

    with pytest.raises(image_attachments.ImageAttachmentError):
        image_attachments.load_image_attachments(tmp_path, ["../outside.png"])


def test_mode_artifact_is_written_with_honest_missing_sections(
    tmp_path: Path,
) -> None:
    meta = mode_artifacts.save_mode_artifact(
        session_id="session-1",
        kind="plan",
        task="ship it",
        content="## Goal\n\nDo the thing.\n",
        root=tmp_path,
    )
    written = Path(meta["path"])
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("# Plan artifact")
    assert "Current state" in meta["missing_sections"]
    assert len(meta["sha256"]) == 64


def test_project_map_builds_facts_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=77"]\n\n'
        '[project]\nname = "demo-project"\n\n'
        '[project.scripts]\ndemo = "demo:main"\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("def test_ok():\n    pass\n")
    (tmp_path / "demo.py").write_text("print('hi')\n")

    facts = project_map.ProjectFactMap(tmp_path, tmp_path / "facts.json").build()
    assert facts["project"]["name"]["value"] == "demo-project"
    assert facts["test_files"] == 1
    assert facts["project"]["test_command"]["value"] == "python -m pytest"
    assert "demo-project" in project_map.ProjectFactMap(
        tmp_path, tmp_path / "facts.json"
    ).summary()


def test_map_service_contract_and_status(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n")
    service = map_service.MapService(tmp_path, root=tmp_path / "maps")

    assert service.status()["exists"] is False
    contract = map_service.level_contract("ultra")
    assert contract.passes == 2
    assert contract.git_history_commits > 0
    preview = service.preview("medium")
    assert preview["files_indexed"] >= 1
    assert preview["level"] == "medium"

    with pytest.raises(ValueError):
        map_service.normalize_map_level("auto")


def test_project_context_reads_instructions_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("Use tabs.\n", encoding="utf-8")

    context = project_context.discover_project_context(repository, branch="main")
    assert "Use tabs." in context.instructions
    assert "project-instructions source=\"AGENTS.md\"" in context.instructions
    assert "git branch: main" in context.environment
    assert [source.path for source in context.sources] == ["AGENTS.md"]
    assert context.prompt_suffix.startswith("\n\n<environment>")


def test_effort_signals_are_derived_and_explained() -> None:
    derived = effort_signals.derive_task_signals(
        "fix auth in src/karox/security.py and tests/test_security.py",
        map_state={
            "implementation": ["src/karox/security.py"],
            "git_evidence": {
                "co_change_pairs": [
                    {
                        "paths": [
                            "src/karox/security.py",
                            "tests/test_security.py",
                        ],
                        "count": 4,
                    }
                ],
                "hot_files": [
                    {"path": "src/karox/security.py", "commits": 8}
                ],
            },
        },
    )
    assert derived.signals.risk_area is True
    # Both referenced files see the other side of the recorded pair, so the
    # partner set is exactly those two paths.
    assert derived.signals.dependency_breadth == 2
    assert derived.signals.regression_history is True
    assert any("co-change partners" in line for line in derived.evidence)
    assert recommend_effort(derived.signals).level in {"high", "extra-high", "ultra"}

    vague = effort_signals.derive_task_signals(
        "sometimes login does not work, please investigate"
    )
    assert vague.signals.ambiguity is True
    assert any("ambiguous statement" in line for line in vague.evidence)


class _FakeProvider:
    def __init__(self, response: ModelResponse | None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.closed = False

    def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    def close(self) -> None:
        self.closed = True


def _routing_registry(tmp_path: Path, provider_ids: tuple[str, ...]) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "providers.json")
    for provider_id in provider_ids:
        registry.put_provider(
            ProviderRecord(
                provider_id=provider_id,
                adapter_kind="openai_compatible_chat",
                base_url=f"https://{provider_id}.invalid/v1",
                privacy_class="public",
            )
        )
        registry.put_model(
            ModelRecord(
                provider_id=provider_id,
                model_id="model-1",
                tools="true",
                streaming="true",
                max_output_tokens=1024,
                pricing=ModelPricing(
                    version="test-1",
                    currency="USD",
                    input_per_million=1.0,
                    output_per_million=2.0,
                    source="test",
                ),
            )
        )
    return registry


def _request() -> ModelRequest:
    return ModelRequest(
        model="model-1",
        messages=(ModelMessage("user", "hello"),),
        deadline_seconds=30.0,
    )


def test_routing_decorates_a_completed_response(tmp_path: Path) -> None:
    registry = _routing_registry(tmp_path, ("demo-1",))
    policy = routing.RoutingPolicy(
        routes=(routing.RouteTarget("demo-1", "model-1"),),
        max_total_tokens=10_000,
    )
    provider = _FakeProvider(
        ModelResponse(
            content="answer",
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    )
    router = routing.RoutedProvider(registry, lambda record: provider, policy)

    result = router.complete(_request())
    assert result.selected_provider == "demo-1"
    assert result.selected_model == "model-1"
    assert result.cumulative_usage["total_tokens"] == 15
    assert result.cost is not None and result.cost > 0
    assert result.budget_exceeded is False
    assert result.route_attempts[0]["status"] == "completed"

    tight = routing.RoutedProvider(
        registry,
        lambda record: _FakeProvider(
            ModelResponse(
                content="answer",
                tool_calls=(),
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        ),
        routing.RoutingPolicy(
            routes=(routing.RouteTarget("demo-1", "model-1"),),
            max_cost=0.000001,
            currency="USD",
        ),
    )
    over_budget = tight.complete(_request())
    assert over_budget.budget_exceeded is True
    assert over_budget.budget_reason == "cost_budget"


@pytest.mark.parametrize(
    "kind",
    [
        ProviderErrorKind.AUTHENTICATION,
        ProviderErrorKind.PERMISSION,
        ProviderErrorKind.MALFORMED_RESPONSE,
    ],
)
def test_routing_falls_back_on_an_endpoint_scoped_failure(
    tmp_path: Path, kind: ProviderErrorKind
) -> None:
    """A dead key or a broken endpoint must not end a run with a healthy route.

    These are properties of route 0, not of the request: another configured
    endpoint may still serve it. A malformed response is safe to fall back on
    only because routing refuses after the first emitted delta, so the user has
    seen nothing from the broken route.
    """

    registry = _routing_registry(tmp_path, ("demo-1", "demo-2"))
    failing = _FakeProvider(None, ProviderError(kind, "endpoint refused"))
    good = _FakeProvider(
        ModelResponse(
            content="fallback",
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )
    )
    policy = routing.RoutingPolicy(
        routes=(
            routing.RouteTarget("demo-1", "model-1"),
            routing.RouteTarget("demo-2", "model-1"),
        ),
        retry=routing.RetryPolicy(max_attempts=1),
    )
    router = routing.RoutedProvider(
        registry,
        lambda record: good if record.provider_id == "demo-2" else failing,
        policy,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    result = router.complete(_request())

    assert result.selected_provider == "demo-2"
    assert result.route_attempts[0]["status"] == "fallback"


def test_routing_falls_back_and_enforces_budgets(tmp_path: Path) -> None:
    registry = _routing_registry(tmp_path, ("demo-1", "demo-2"))
    failing = _FakeProvider(
        None, ProviderError(ProviderErrorKind.TRANSPORT, "endpoint down")
    )
    good = _FakeProvider(
        ModelResponse(
            content="fallback",
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )
    )
    policy = routing.RoutingPolicy(
        routes=(
            routing.RouteTarget("demo-1", "model-1"),
            routing.RouteTarget("demo-2", "model-1"),
        ),
        retry=routing.RetryPolicy(max_attempts=1),
    )
    router = routing.RoutedProvider(
        registry,
        lambda record: good if record.provider_id == "demo-2" else failing,
        policy,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    result = router.complete(_request())
    assert result.selected_provider == "demo-2"
    assert result.route_attempts[0]["status"] == "fallback"

    exhausted = routing.RoutedProvider(
        registry,
        lambda record: good,
        routing.RoutingPolicy(
            routes=(routing.RouteTarget("demo-1", "model-1"),),
            max_total_tokens=10,
        ),
        initial_usage={"total_tokens": 10},
    )
    with pytest.raises(ProviderError) as caught:
        exhausted.complete(_request())
    assert caught.value.kind is ProviderErrorKind.BUDGET_EXCEEDED

    private_only = routing.RoutedProvider(
        registry,
        lambda record: good,
        routing.RoutingPolicy(
            routes=(routing.RouteTarget("demo-1", "model-1"),),
            privacy_limit="private",
        ),
    )
    with pytest.raises(ProviderError) as rejected:
        private_only.complete(_request())
    assert rejected.value.kind is ProviderErrorKind.PERMISSION


def test_workspace_worker_rejects_unknown_actions() -> None:
    with pytest.raises(workspace_worker.InvalidCommand):
        workspace_worker.validate_repo_command({"action": "delete-everything"})