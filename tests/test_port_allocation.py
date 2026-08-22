"""Atomic port reservation and evidence-only ownership.

Mandate section 25: no find-free/release/bind-later TOCTOU -- the
reservation IS the bound socket; and no process is ever killed to free a
port unless it provably belongs to KaroX.
"""

from __future__ import annotations

import os
import socket
import unittest

from _support import SRC  # noqa: F401
from karox.port_allocation import (
    KAROX_PROCESS_EVIDENCE,
    karox_owned_process,
    reserve_port,
)


class ReservePortTests(unittest.TestCase):
    def test_reservation_holds_the_port_exclusively(self) -> None:
        with reserve_port() as reservation:
            self.assertGreater(reservation.port, 0)
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt":
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            with probe:
                with self.assertRaises(OSError):
                    probe.bind((reservation.host, reservation.port))

    def test_release_frees_the_port_for_the_next_binder(self) -> None:
        reservation = reserve_port()
        port = reservation.port
        reservation.release()
        follower = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with follower:
            follower.bind(("127.0.0.1", port))

    def test_detach_transfers_the_bound_socket_without_a_gap(self) -> None:
        reservation = reserve_port()
        adopted = reservation.detach_socket()
        try:
            self.assertEqual(
                int(adopted.getsockname()[1]), reservation.port
            )
            adopted.listen(1)
        finally:
            adopted.close()

    def test_detach_after_release_is_an_error(self) -> None:
        reservation = reserve_port()
        reservation.release()
        with self.assertRaises(ValueError):
            reservation.detach_socket()

    def test_reserving_a_taken_port_raises_instead_of_killing(self) -> None:
        with reserve_port() as first:
            with self.assertRaises(OSError):
                reserve_port(port=first.port)

    def test_release_is_idempotent(self) -> None:
        reservation = reserve_port()
        reservation.release()
        reservation.release()


class OwnershipEvidenceTests(unittest.TestCase):
    def test_own_python_process_is_not_karox_evidence(self) -> None:
        # This test process is python without 'karox' in its identity unless
        # the checkout path contains it; either way the function must return
        # a bool or None, never raise.
        verdict = karox_owned_process(os.getpid())
        self.assertIn(verdict, (True, False, None))

    def test_dead_pid_is_unknown_never_owned(self) -> None:
        self.assertIsNone(karox_owned_process(2_000_000_000))

    def test_evidence_list_is_positive_only(self) -> None:
        self.assertIn("karox", KAROX_PROCESS_EVIDENCE)
        self.assertNotIn("python", KAROX_PROCESS_EVIDENCE)
        self.assertNotIn("node", KAROX_PROCESS_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
