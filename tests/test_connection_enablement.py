"""B5 foundation: persisted enable/disable for providers and connections.

These are the contracts the management UI is allowed to assume. They are
deliberately written against the *stores*, not the screens: disabled has to be
a fact that survives a process restart before anything may draw it.

Every persistence assertion re-reads through a freshly constructed registry
pointed at the same file, so an in-memory dataclass that merely looks right
cannot pass. Where a test says it survives a reload, it means the bytes on
disk said so.

The other half of the contract is what enable/disable must *not* mean.
The flag says only: this saved configuration may be used for new work. It is
not a claim that the credential is valid, the provider reachable, the bridge
running or the external service confirmed. Several tests below exist purely to
keep those two ideas apart.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connection_controller import (
    BLOCKER_CONNECTION_DISABLED,
    ConnectionController,
    ConnectionLaunchError,
)
from karox.connections import (
    ConnectionError,
    ConnectionRegistry,
    McpClientTarget,
)
from karox.credentials import CredentialStore
from karox.provider_controller import ProviderController
from karox.registry import (
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
    RegistryError,
)

# Values that are not JSON booleans. A registry file holds machine-written
# JSON, so none of these may be interpreted -- and the two string cases are
# the whole point, because bool of the text false is True.
NON_BOOLEANS = ("false", "true", "", "no", 0, 1, 2, 0.0, None, [], {}, ["true"])


def _provider(provider_id: str = "alpha", **overrides: object) -> ProviderRecord:
    values: dict[str, object] = {
        "provider_id": provider_id,
        "adapter_kind": "openai_compatible_chat",
        "base_url": f"https://{provider_id}.example/v1",
        "privacy_class": "public",
    }
    values.update(overrides)
    return ProviderRecord(**values)  # type: ignore[arg-type]


def _target(
    connection_id: str = "c-0123456789abcdef",
    *,
    name: str = "ClickUp",
    **overrides: object,
) -> McpClientTarget:
    values: dict[str, object] = {
        "connection_id": connection_id,
        "name": name,
        "preset_id": "clickup",
        "transport": "streamable_http",
        "endpoint_path": "/mcp",
        "auth_scheme": "bearer",
        "tunnel": "cloudflare",
        "runtime_profile": "generic-streamable-http",
        "public_url": "https://bridge.example.com",
        # A cloudflare tunnel publishes a temporary URL, so claiming stability
        # here makes the launcher refuse the fixture for `stable_url_unavailable`
        # long before it reaches anything about being enabled.
        "url_stability": "temporary",
        "credential_ref": f"os-keyring:connection/{connection_id}",
        "credential_fingerprint": "sha256:fixture",
        "port": 8765,
    }
    values.update(overrides)
    return McpClientTarget(**values)  # type: ignore[arg-type]


class _MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.deleted: list[tuple[str, str]] = []

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.values:
            from karox.credentials import CredentialError

            raise CredentialError("credential does not exist")
        self.deleted.append((service, account))
        del self.values[(service, account)]


class _RuntimeManager:
    """Records what was asked of it, so did-not-stop is provable."""

    def __init__(self, states: dict[str, str]) -> None:
        self.states = dict(states)
        self.stopped: list[str] = []
        self.forgotten: list[str] = []

    def status(self, connection_id: str) -> dict[str, object]:
        return {
            "connection_id": connection_id,
            "runtime_id": f"rt-{connection_id}",
            "state": self.states.get(connection_id, "configured_not_running"),
            "managed": self.states.get(connection_id) in {"running", "degraded"},
        }

    def stop(self, connection_id: str) -> dict[str, object]:
        self.stopped.append(connection_id)
        self.states[connection_id] = "stopped"
        return self.status(connection_id)

    def forget(self, connection_id: str) -> None:
        self.forgotten.append(connection_id)
        self.states.pop(connection_id, None)


# ---------------------------------------------------------------------------
# ProviderRecord / ProviderRegistry
# ---------------------------------------------------------------------------


class ProviderEnablementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "providers.json"
        self.registry = ProviderRegistry(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def reload(self) -> ProviderRegistry:
        """A second registry over the same file: persistence, not memory."""

        return ProviderRegistry(self.path)

    def seed(self, provider_id: str = "alpha", *, model_id: str = "model-a") -> None:
        self.registry.put_provider(
            _provider(provider_id, credential_ref=f"os-keyring:provider/{provider_id}")
        )
        self.registry.put_model(ModelRecord(provider_id, model_id))

    def test_provider_json_without_enabled_reads_as_enabled(self) -> None:
        """A registry written before B5 has no opinion, so it keeps working.

        Defaulting the other way would silently park every provider a user
        already had, the moment they upgraded.
        """

        self.seed()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for item in payload["providers"]:
            item.pop("enabled", None)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertTrue(self.reload().provider("alpha").enabled)

    def test_enabled_false_survives_reload(self) -> None:
        self.seed()
        self.registry.set_provider_enabled("alpha", False)

        self.assertFalse(self.reload().provider("alpha").enabled)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(stored["providers"][0]["enabled"], False)

    def test_enabled_true_survives_reload(self) -> None:
        self.seed()
        self.registry.set_provider_enabled("alpha", False)
        self.registry.set_provider_enabled("alpha", True)

        self.assertTrue(self.reload().provider("alpha").enabled)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(stored["providers"][0]["enabled"], True)

    def test_non_boolean_enabled_is_rejected_not_coerced(self) -> None:
        """The defect this closes: the bool of the text false is True.

        Coercing in from_dict would make a corrupted or hand-edited file read
        as enabled, which is the one direction the mistake must never fall: it
        silently re-arms a provider the user switched off.
        """

        base = asdict(_provider())
        for value in NON_BOOLEANS:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProviderRecord.from_dict(dict(base, enabled=value))

    def test_non_boolean_enabled_on_disk_is_rejected_by_the_registry(self) -> None:
        self.seed()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["providers"][0]["enabled"] = "false"
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(RegistryError):
            self.reload().providers()

    def test_literal_booleans_and_a_missing_key_are_accepted(self) -> None:
        base = asdict(_provider())
        self.assertTrue(ProviderRecord.from_dict(dict(base, enabled=True)).enabled)
        self.assertFalse(ProviderRecord.from_dict(dict(base, enabled=False)).enabled)
        without = dict(base)
        without.pop("enabled")
        self.assertTrue(ProviderRecord.from_dict(without).enabled)

    def test_disable_preserves_record_models_and_credential(self) -> None:
        """Disable is not delete. Nothing may be lost by parking a provider."""

        self.seed()
        self.registry.put_model(ModelRecord("alpha", "model-b"))
        before = self.registry.provider("alpha")

        self.registry.set_provider_enabled("alpha", False)
        after = self.reload().provider("alpha")

        self.assertEqual(after.provider_id, before.provider_id)
        self.assertEqual(after.base_url, before.base_url)
        self.assertEqual(after.adapter_kind, before.adapter_kind)
        self.assertEqual(after.credential_ref, before.credential_ref)
        self.assertEqual(
            [item.model_id for item in self.reload().models("alpha")],
            ["model-a", "model-b"],
        )

    def test_disabling_the_selected_provider_clears_the_selection(self) -> None:
        """And chooses no replacement.

        Picking some other model for the user is a product decision nobody
        made. The selection becomes empty and the surface above says so.
        """

        self.seed()
        self.registry.select_model("alpha", "model-a")

        self.registry.set_provider_enabled("alpha", False)

        self.assertIsNone(self.reload().selected_model())

    def test_disabling_another_provider_leaves_the_selection_alone(self) -> None:
        self.seed("alpha")
        self.seed("beta", model_id="model-b")
        self.registry.select_model("alpha", "model-a")

        self.registry.set_provider_enabled("beta", False)

        selected = self.reload().selected_model()
        assert selected is not None
        self.assertEqual(
            (selected.provider_id, selected.model_id), ("alpha", "model-a")
        )

    def test_registry_refuses_to_select_a_disabled_provider(self) -> None:
        self.seed()
        self.registry.set_provider_enabled("alpha", False)

        with self.assertRaises(RegistryError):
            self.registry.select_model("alpha", "model-a")
        self.assertIsNone(self.reload().selected_model())

    def test_repeated_disable_is_idempotent(self) -> None:
        self.seed()
        first = self.registry.set_provider_enabled("alpha", False)
        second = self.registry.set_provider_enabled("alpha", False)

        self.assertFalse(first.enabled)
        self.assertFalse(second.enabled)
        self.assertFalse(self.reload().provider("alpha").enabled)
        self.assertEqual(len(self.reload().providers()), 1)

    def test_disabling_an_unknown_provider_is_an_error(self) -> None:
        with self.assertRaises(RegistryError):
            self.registry.set_provider_enabled("nope", False)


# ---------------------------------------------------------------------------
# ProviderController
# ---------------------------------------------------------------------------


class ProviderControllerEnablementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "providers.json"
        self.registry = ProviderRegistry(self.path)
        self.backend = _MemoryCredentials()
        self.credentials = CredentialStore(self.backend)
        self.controller = ProviderController(
            registry=self.registry,
            credentials=self.credentials,
            tester=lambda provider, model: {"status": "ok"},
        )
        self.registry.put_provider(_provider("alpha"))
        self.registry.put_model(ModelRecord("alpha", "model-a"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def reload(self) -> ProviderRegistry:
        return ProviderRegistry(self.path)

    def test_repair_selection_skips_disabled_providers(self) -> None:
        self.registry.put_provider(_provider("beta"))
        self.registry.put_model(ModelRecord("beta", "model-b"))
        self.registry.set_provider_enabled("alpha", False)

        repaired = self.controller.repair_selection()

        assert repaired is not None
        self.assertEqual(repaired.provider_id, "beta")

    def test_repair_selection_is_none_when_every_provider_is_disabled(self) -> None:
        self.registry.set_provider_enabled("alpha", False)

        self.assertIsNone(self.controller.repair_selection())
        self.assertIsNone(self.reload().selected_model())

    def test_enable_neither_selects_a_model_nor_claims_the_provider_works(self) -> None:
        """Enabled and working are two different claims.

        A provider comes back from being parked with its key possibly revoked
        and its endpoint possibly moved. Only a check the user runs may turn
        that into a proven state, so enabling neither selects nor verifies.
        """

        self.registry.set_provider_enabled("alpha", False)

        mutation = self.controller.set_provider_enabled("alpha", True)

        self.assertEqual(mutation.status, "enabled")
        self.assertIsNone(mutation.selected_model)
        self.assertIsNone(self.reload().selected_model())

    def test_edit_of_a_disabled_provider_keeps_it_disabled(self) -> None:
        """A form has no enabled field, so a naive save would re-arm it.

        The controller carries the previous flag forward instead of taking the
        dataclass default, because leaving disable is a deliberate action with
        its own button.
        """

        self.registry.set_provider_enabled("alpha", False)

        self.controller.configure_provider_model(
            _provider("alpha", base_url="https://alpha.example/v2"),
            ModelRecord("alpha", "model-a"),
            activate=True,
        )

        reloaded = self.reload().provider("alpha")
        self.assertFalse(reloaded.enabled)
        self.assertEqual(reloaded.base_url, "https://alpha.example/v2")

    def test_activate_true_cannot_activate_a_disabled_provider(self) -> None:
        self.registry.set_provider_enabled("alpha", False)

        self.controller.configure_provider_model(
            _provider("alpha"),
            ModelRecord("alpha", "model-a"),
            activate=True,
        )

        self.assertIsNone(self.reload().selected_model())

    def test_replacing_the_secret_of_a_disabled_provider_does_not_enable_it(self) -> None:
        self.registry.set_provider_enabled("alpha", False)

        self.controller.set_credential("alpha", "alpha-value")

        self.assertFalse(self.reload().provider("alpha").enabled)

    def test_set_provider_enabled_never_touches_the_secret(self) -> None:
        self.controller.set_credential("alpha", "alpha-value")
        reference = self.reload().provider("alpha").credential_ref

        self.controller.set_provider_enabled("alpha", False)
        self.controller.set_provider_enabled("alpha", True)

        self.assertEqual(self.reload().provider("alpha").credential_ref, reference)
        self.assertEqual(self.backend.deleted, [])
        self.assertTrue(self.controller.details("alpha").credential["available"])

    def test_set_provider_enabled_returns_the_persisted_state(self) -> None:
        mutation = self.controller.set_provider_enabled("alpha", False)

        assert mutation.provider is not None
        self.assertEqual(mutation.status, "disabled")
        self.assertFalse(mutation.provider.enabled)
        self.assertEqual(
            mutation.provider.enabled,
            self.reload().provider("alpha").enabled,
        )


# ---------------------------------------------------------------------------
# McpClientTarget / ConnectionRegistry
# ---------------------------------------------------------------------------


CONNECTION_ID = "c-0123456789abcdef"


class ConnectionEnablementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "connections.json"
        self.registry = ConnectionRegistry(self.path)
        self.registry.put(_target())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def reload(self) -> ConnectionRegistry:
        return ConnectionRegistry(self.path)

    def test_connection_json_without_enabled_reads_as_enabled(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for item in payload["connections"]:
            item.pop("enabled", None)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertTrue(self.reload().get(CONNECTION_ID).enabled)

    def test_enabled_false_survives_reload(self) -> None:
        self.registry.set_enabled(CONNECTION_ID, False)

        self.assertFalse(self.reload().get(CONNECTION_ID).enabled)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(stored["connections"][0]["enabled"], False)

    def test_non_boolean_enabled_is_rejected_not_coerced(self) -> None:
        base = _target().to_dict()
        for value in NON_BOOLEANS:
            with self.subTest(value=value):
                with self.assertRaises(ConnectionError):
                    McpClientTarget.from_dict(dict(base, enabled=value))

    def test_non_boolean_enabled_on_disk_is_rejected_by_the_registry(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["connections"][0]["enabled"] = "false"
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(ConnectionError):
            self.reload().list()

    def test_disable_preserves_every_other_field(self) -> None:
        """Everything re-enabling depends on has to still be there.

        Transport, port, tunnel, endpoint path, runtime profile and the
        credential *reference* -- the last one is why enabling can ask for no
        secret at all.
        """

        before = self.registry.get(CONNECTION_ID)

        self.registry.set_enabled(CONNECTION_ID, False)
        after = self.reload().get(CONNECTION_ID)

        for field in (
            "transport",
            "port",
            "tunnel",
            "endpoint_path",
            "runtime_profile",
            "auth_scheme",
            "public_url",
            "credential_ref",
            "credential_fingerprint",
            "preset_id",
            "name",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(after, field), getattr(before, field))

    def test_disable_does_not_remove_the_record(self) -> None:
        self.registry.set_enabled(CONNECTION_ID, False)

        self.assertEqual(
            [item.connection_id for item in self.reload().list()],
            [CONNECTION_ID],
        )

    def test_enable_does_not_create_a_second_record(self) -> None:
        self.registry.set_enabled(CONNECTION_ID, False)
        self.registry.set_enabled(CONNECTION_ID, True)

        self.assertEqual(len(self.reload().list()), 1)
        self.assertTrue(self.reload().get(CONNECTION_ID).enabled)

    def test_repeated_set_enabled_is_idempotent(self) -> None:
        first = self.registry.set_enabled(CONNECTION_ID, False)
        second = self.registry.set_enabled(CONNECTION_ID, False)

        self.assertFalse(first.enabled)
        self.assertFalse(second.enabled)
        self.assertEqual(len(self.reload().list()), 1)

    def test_set_enabled_rejects_a_non_boolean(self) -> None:
        with self.assertRaises(ConnectionError):
            self.registry.set_enabled(CONNECTION_ID, "false")  # type: ignore[arg-type]
        self.assertTrue(self.reload().get(CONNECTION_ID).enabled)


# ---------------------------------------------------------------------------
# ConnectionController
# ---------------------------------------------------------------------------


class ConnectionControllerEnablementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "connections.json"
        self.registry = ConnectionRegistry(self.path)
        self.registry.put(_target())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def controller(self, *, state: str = "configured_not_running"):
        runtime = _RuntimeManager({CONNECTION_ID: state})
        launched: list[str] = []

        def tester(target, *, endpoint_url, secret, timeout_seconds):
            return {"state": "ok", "tool_count": 3}

        def remover(connection_id, *, registry, credentials=None):
            return registry.remove(connection_id)

        def launcher(target):
            launched.append(target.connection_id)
            runtime.states[target.connection_id] = "running"
            return {
                "success": True,
                "target": target,
                "public_endpoint": target.effective_url,
                "runtime_id": f"rt-{target.connection_id}",
            }

        built = ConnectionController(
            registry=self.registry,
            runtime_manager=runtime,
            tester=tester,
            secret_resolver=lambda target: f"resolved-{target.connection_id}",
            remover=remover,
            launcher=launcher,
            credentials=object(),
        )
        return built, runtime, launched

    def test_disable_does_not_stop_or_restart_the_runtime(self) -> None:
        """A live bridge keeps serving the URL its user already pasted.

        Stopping it is a separate, visible decision. A registry write that
        killed a running endpoint would be exactly the silent side effect this
        contract exists to prevent.
        """

        controller, runtime, launched = self.controller(state="running")

        result = controller.set_enabled(CONNECTION_ID, False)

        self.assertEqual(runtime.stopped, [])
        self.assertEqual(launched, [])
        self.assertEqual(runtime.states[CONNECTION_ID], "running")
        self.assertEqual(result["status"], "disabled")
        self.assertIs(result["enabled"], False)

    def test_a_running_runtime_stays_running_after_a_registry_disable(self) -> None:
        controller, _runtime, _launched = self.controller(state="running")

        controller.set_enabled(CONNECTION_ID, False)

        self.assertEqual(controller.get(CONNECTION_ID).state, "running")
        self.assertFalse(controller.get(CONNECTION_ID).target.enabled)

    def test_start_of_a_disabled_connection_is_refused_with_a_named_blocker(self) -> None:
        controller, runtime, launched = self.controller()
        controller.set_enabled(CONNECTION_ID, False)

        with self.assertRaises(ConnectionLaunchError) as caught:
            controller.start(CONNECTION_ID)

        self.assertIn(BLOCKER_CONNECTION_DISABLED, caught.exception.blockers)
        self.assertEqual(launched, [])
        self.assertEqual(runtime.states[CONNECTION_ID], "configured_not_running")

    def test_enable_does_not_start_a_process(self) -> None:
        controller, runtime, launched = self.controller()
        controller.set_enabled(CONNECTION_ID, False)

        result = controller.set_enabled(CONNECTION_ID, True)

        self.assertEqual(launched, [])
        self.assertEqual(runtime.states[CONNECTION_ID], "configured_not_running")
        self.assertEqual(result["status"], "enabled")

    def test_start_is_allowed_again_after_enable(self) -> None:
        controller, _runtime, launched = self.controller()
        controller.set_enabled(CONNECTION_ID, False)
        controller.set_enabled(CONNECTION_ID, True)

        launch = controller.start(CONNECTION_ID)

        self.assertTrue(launch.success)
        self.assertEqual(launched, [CONNECTION_ID])

    def test_get_and_list_report_the_persisted_enabled_state(self) -> None:
        controller, _runtime, _launched = self.controller()
        controller.set_enabled(CONNECTION_ID, False)

        self.assertFalse(controller.get(CONNECTION_ID).target.enabled)
        self.assertEqual(
            [state.target.enabled for state in controller.list()],
            [False],
        )
        self.assertIs(
            controller.get(CONNECTION_ID).to_dict()["target"]["enabled"],
            False,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
