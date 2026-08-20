"""Durability contract for a saved profile's bridge credential.

A saved web-bridge profile is a *durable identity*: its keyring entry is the
bearer token an already-configured MCP connector authenticates with. Two
regressions previously destroyed that identity, both reproduced here:

* a losing owner -- one that minted the token and then failed, typically on a
  port bind -- deleted the shared credential in its ``finally`` block, so a
  *live* owner of the same profile kept serving while its identity vanished
  from the OS keyring;
* ``resolve()`` reported a momentarily unreadable backend the same way it
  reported a genuinely absent entry, so a transient keyring failure made the
  launcher mint a replacement secret over the live one.

No test here touches a real OS keyring: a fake backend holds the values.
"""

from __future__ import annotations

import unittest
from unittest import mock

import _path_setup  # noqa: F401

from karox.bridge import BridgeCredentialMissing, BridgeCredentialStore
from karox.connection_status import CredentialStatus, _check_bridge_credential
from karox.credentials import CredentialError
from karox import web_bridge_launcher


class _FakeBackend:
    """Minimal credential backend with an injectable read failure."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.get_error: Exception | None = None
        self.deleted: list[str] = []

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[account] = secret

    def get(self, service: str, account: str):
        if self.get_error is not None:
            raise self.get_error
        return self.values.get(account)

    def delete(self, service: str, account: str) -> None:
        self.deleted.append(account)
        self.values.pop(account, None)


class ResolveDistinguishesAbsenceFromFailureTests(unittest.TestCase):
    """Absence and unavailability must not look alike to callers."""

    def test_absent_entry_raises_the_missing_subclass(self) -> None:
        store = BridgeCredentialStore(_FakeBackend())
        with self.assertRaises(BridgeCredentialMissing):
            store.resolve("os-keyring:bridge/web-saved-abc")

    def test_missing_stays_a_credential_error_for_existing_callers(self) -> None:
        store = BridgeCredentialStore(_FakeBackend())
        # Callers written against the broader type keep working.
        with self.assertRaises(CredentialError):
            store.resolve("os-keyring:bridge/web-saved-abc")

    def test_unreadable_backend_is_not_reported_as_absent(self) -> None:
        backend = _FakeBackend()
        backend.get_error = RuntimeError("vault temporarily locked")
        store = BridgeCredentialStore(backend)
        with self.assertRaises(CredentialError) as caught:
            store.resolve("os-keyring:bridge/web-saved-abc")
        self.assertNotIsInstance(caught.exception, BridgeCredentialMissing)

    def test_present_entry_resolves(self) -> None:
        # The keyring account is the bare session id; the "bridge/" part of the
        # reference is the namespace prefix, not part of the account name.
        backend = _FakeBackend({"web-saved-abc": "s3cret-value"})
        store = BridgeCredentialStore(backend)
        self.assertEqual(store.resolve("os-keyring:bridge/web-saved-abc"), "s3cret-value")


class CredentialStatusMappingTests(unittest.TestCase):
    """An unreadable store must fail closed rather than invite a re-mint."""

    def test_absent_credential_maps_to_missing(self) -> None:
        with mock.patch.object(
            BridgeCredentialStore,
            "resolve",
            side_effect=BridgeCredentialMissing("absent"),
        ):
            status, fingerprint = _check_bridge_credential("web-saved-abc")
        self.assertEqual(status, CredentialStatus.MISSING)
        self.assertIsNone(fingerprint)

    def test_unreadable_backend_maps_to_error_not_missing(self) -> None:
        with mock.patch.object(
            BridgeCredentialStore,
            "resolve",
            side_effect=CredentialError("cannot read bridge OS credential: OSError"),
        ):
            status, fingerprint = _check_bridge_credential("web-saved-abc")
        self.assertEqual(status, CredentialStatus.ERROR)
        self.assertIsNone(fingerprint)

    def test_empty_session_id_is_missing(self) -> None:
        status, fingerprint = _check_bridge_credential("")
        self.assertEqual(status, CredentialStatus.MISSING)
        self.assertIsNone(fingerprint)


class IdentityExclusivityTests(unittest.TestCase):
    """Discarding a shared credential requires proof of sole ownership."""

    def test_another_live_owner_blocks_exclusivity(self) -> None:
        with mock.patch("psutil.pids", return_value=[4321]), mock.patch(
            "karox.port_ownership.prove_saved_bridge_owner_identity",
            return_value=True,
        ):
            self.assertFalse(
                web_bridge_launcher._saved_bridge_identity_is_exclusive("chatgpt-dev")
            )

    def test_no_other_owner_allows_exclusivity(self) -> None:
        with mock.patch("psutil.pids", return_value=[4321]), mock.patch(
            "karox.port_ownership.prove_saved_bridge_owner_identity",
            return_value=False,
        ):
            self.assertTrue(
                web_bridge_launcher._saved_bridge_identity_is_exclusive("chatgpt-dev")
            )

    def test_own_pid_is_never_counted_as_another_owner(self) -> None:
        import os

        # The caller itself proves ownership; that must not block its own cleanup.
        with mock.patch("psutil.pids", return_value=[os.getpid()]), mock.patch(
            "karox.port_ownership.prove_saved_bridge_owner_identity",
            return_value=True,
        ) as proof:
            self.assertTrue(
                web_bridge_launcher._saved_bridge_identity_is_exclusive("chatgpt-dev")
            )
        proof.assert_not_called()

    def test_unreadable_process_table_is_not_proof_of_absence(self) -> None:
        with mock.patch("psutil.pids", side_effect=RuntimeError("denied")):
            self.assertFalse(
                web_bridge_launcher._saved_bridge_identity_is_exclusive("chatgpt-dev")
            )

    def test_unprovable_single_process_keeps_the_credential(self) -> None:
        with mock.patch("psutil.pids", return_value=[4321]), mock.patch(
            "karox.port_ownership.prove_saved_bridge_owner_identity",
            side_effect=RuntimeError("access denied"),
        ):
            self.assertFalse(
                web_bridge_launcher._saved_bridge_identity_is_exclusive("chatgpt-dev")
            )

    def test_empty_profile_name_is_never_exclusive(self) -> None:
        self.assertFalse(web_bridge_launcher._saved_bridge_identity_is_exclusive(""))
        self.assertFalse(web_bridge_launcher._saved_bridge_identity_is_exclusive(None))


class ReaperNeverRevokesADurableIdentityTests(unittest.TestCase):
    """The orphan reaper must not mistake neighbouring artifacts for records.

    ``owner_exit_record_path`` writes ``<session>.last-exit.json`` into the same
    directory as the watchdog records and under the same suffix. It names a
    session id and the pid of an owner that has *already exited*, and carries no
    ``persistent_session`` flag -- so read as a watchdog record it is
    indistinguishable from a throwaway orphan, and reaping it revoked the saved
    profile's credential on the very next start.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        patcher = mock.patch.dict(
            "os.environ",
            {
                "KAROX_RUNTIME_DIR": self.root,
                "KAROX_VNEXT_RUNTIME_DIR": self.root,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.watchdog = web_bridge_launcher.watchdog_dir()
        self.watchdog.mkdir(parents=True, exist_ok=True)

    def _write(self, path, payload: dict) -> None:
        import json

        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_exit_record_is_not_reaped(self) -> None:
        session_id = "web-saved-3a19c4459fd8db7b317fc9f8"
        # A dead owner pid is what makes the reaper consider an entry at all.
        self._write(
            web_bridge_launcher.owner_exit_record_path(session_id),
            {
                "session_id": session_id,
                "saved_profile": "hyperagent-auto",
                "reason": "WebBridgeLaunchError",
                "owner_pid": 999_999,
                "recorded_at": 1.0,
            },
        )
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ), mock.patch(
            "karox.bridge.BridgeCredentialStore.delete"
        ) as delete, mock.patch.object(
            web_bridge_launcher, "SessionStore"
        ) as sessions:
            reaped = web_bridge_launcher.reap_orphaned_web_bridges()
        self.assertEqual(reaped, ())
        delete.assert_not_called()
        sessions.return_value.revoke.assert_not_called()
        # The post-mortem must also survive, since the parent reads it to report
        # why a child died.
        self.assertTrue(web_bridge_launcher.owner_exit_record_path(session_id).exists())

    def test_saved_profile_record_without_the_flag_is_not_revoked(self) -> None:
        session_id = "web-saved-18870c825f9eba831276059d"
        self._write(
            self.watchdog / f"{session_id}.json",
            {
                "session_id": session_id,
                "saved_profile": "chatgpt-dev",
                "owner_pid": 999_999,
            },
        )
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ), mock.patch(
            "karox.bridge.BridgeCredentialStore.delete"
        ) as delete, mock.patch.object(
            web_bridge_launcher, "SessionStore"
        ) as sessions:
            web_bridge_launcher.reap_orphaned_web_bridges()
        delete.assert_not_called()
        sessions.return_value.revoke.assert_not_called()

    def test_durable_session_id_alone_is_enough_to_be_spared(self) -> None:
        session_id = "web-saved-deadbeefdeadbeefdeadbeef"
        self._write(
            self.watchdog / f"{session_id}.json",
            {"session_id": session_id, "owner_pid": 999_999},
        )
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ), mock.patch(
            "karox.bridge.BridgeCredentialStore.delete"
        ) as delete, mock.patch.object(
            web_bridge_launcher, "SessionStore"
        ) as sessions:
            web_bridge_launcher.reap_orphaned_web_bridges()
        delete.assert_not_called()
        sessions.return_value.revoke.assert_not_called()

    def test_a_real_throwaway_orphan_is_still_reaped(self) -> None:
        session_id = "web-ephemeral-1234"
        self._write(
            self.watchdog / f"{session_id}.json",
            {
                "session_id": session_id,
                "saved_profile": None,
                "persistent_session": False,
                "owner_pid": 999_999,
            },
        )
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ), mock.patch(
            "karox.bridge.BridgeCredentialStore.delete"
        ) as delete, mock.patch.object(
            web_bridge_launcher, "SessionStore"
        ):
            reaped = web_bridge_launcher.reap_orphaned_web_bridges()
        delete.assert_called_once_with(session_id)
        self.assertEqual(reaped, (session_id,))


class StaleDurableRecordReclaimTests(unittest.TestCase):
    """A dead owner's record must not brick its own saved profile forever.

    ``claim_watchdog`` refuses to overwrite an existing record so a stale one
    stays available for ``bridge doctor`` to revoke the orphan's credential and
    session. For a *durable* profile there is nothing to revoke -- both are kept
    by design -- so the refusal only meant every successor stayed unregistered,
    the supervisor read the profile as not running, and respawned it forever.
    Takeover is allowed for exactly that case and nothing wider.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = __import__("pathlib").Path(self._tmp.name) / "web-saved-abc.json"
        self.payload = {
            "session_id": "web-saved-abc",
            "saved_profile": "chatgpt-dev",
            "persistent_session": True,
            "owner_pid": 4242,
        }

    def _existing(self, **overrides: object) -> None:
        import json

        record = {
            "session_id": "web-saved-abc",
            "saved_profile": "chatgpt-dev",
            "persistent_session": True,
            "owner_pid": 999_999,
        }
        record.update(overrides)
        self.path.write_text(json.dumps(record), encoding="utf-8")

    def _claim(self) -> None:
        web_bridge_launcher.claim_watchdog(self.path, self.payload)

    def test_dead_owner_of_the_same_identity_is_reclaimed(self) -> None:
        import json

        self._existing()
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ):
            self._claim()
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["owner_pid"], 4242)
        # No staging file may be left behind next to the record.
        leftovers = list(self.path.parent.glob("*.reclaim"))
        self.assertEqual(leftovers, [])

    def test_live_owner_is_never_taken_over(self) -> None:
        self._existing()
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=True
        ):
            with self.assertRaises(web_bridge_launcher.WebBridgeLaunchError):
                self._claim()

    def test_a_different_saved_profile_is_never_taken_over(self) -> None:
        self._existing(saved_profile="clickup-opus")
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ):
            with self.assertRaises(web_bridge_launcher.WebBridgeLaunchError):
                self._claim()

    def test_a_different_session_is_never_taken_over(self) -> None:
        self._existing(session_id="web-saved-other")
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ):
            with self.assertRaises(web_bridge_launcher.WebBridgeLaunchError):
                self._claim()

    def test_a_non_persistent_claim_is_never_allowed_to_reclaim(self) -> None:
        self.payload["persistent_session"] = False
        self._existing()
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ):
            with self.assertRaises(web_bridge_launcher.WebBridgeLaunchError):
                self._claim()

    def test_unreadable_record_is_never_taken_over(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ):
            with self.assertRaises(web_bridge_launcher.WebBridgeLaunchError):
                self._claim()

    def test_record_without_an_owner_pid_is_never_taken_over(self) -> None:
        self._existing(owner_pid=None)
        with mock.patch.object(
            web_bridge_launcher, "_process_is_alive", return_value=False
        ):
            with self.assertRaises(web_bridge_launcher.WebBridgeLaunchError):
                self._claim()

    def test_a_free_path_still_uses_the_exclusive_create(self) -> None:
        import json

        self._claim()
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["owner_pid"], 4242
        )


class ReleaseOwnWatchdogTests(unittest.TestCase):
    """A departing owner must not delete a successor's registration.

    Allowing a dead owner's durable record to be reclaimed opens a race: the
    slow-exiting predecessor reaches its ``finally`` after the successor has
    already taken the path over. An unconditional unlink there deletes the live
    successor's record, which is how a serving bridge ended up with nothing on
    disk naming it.
    """

    def setUp(self) -> None:
        import pathlib
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / "web-saved-abc.json"

    def _write(self, owner_pid: object) -> None:
        import json

        self.path.write_text(
            json.dumps({"session_id": "web-saved-abc", "owner_pid": owner_pid}),
            encoding="utf-8",
        )

    def test_own_record_is_released(self) -> None:
        import os

        self._write(os.getpid())
        self.assertTrue(web_bridge_launcher._release_own_watchdog(self.path))
        self.assertFalse(self.path.exists())

    def test_a_successors_record_is_left_alone(self) -> None:
        self._write(999_999)
        self.assertFalse(web_bridge_launcher._release_own_watchdog(self.path))
        self.assertTrue(self.path.exists())

    def test_a_record_without_an_owner_is_released(self) -> None:
        # Nothing claims it, so leaving it would brick the profile.
        self._write(None)
        self.assertTrue(web_bridge_launcher._release_own_watchdog(self.path))
        self.assertFalse(self.path.exists())

    def test_a_corrupt_record_still_self_heals(self) -> None:
        # A record that cannot be parsed is also one that cannot be reclaimed,
        # so refusing to remove it would strand the profile permanently.
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertTrue(web_bridge_launcher._release_own_watchdog(self.path))
        self.assertFalse(self.path.exists())

    def test_a_missing_record_is_not_an_error(self) -> None:
        self.assertFalse(web_bridge_launcher._release_own_watchdog(self.path))


if __name__ == "__main__":
    unittest.main()
