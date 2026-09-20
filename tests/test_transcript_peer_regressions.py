"""Independent peer-review regressions for committed transcript reads."""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from karox.transcript import TranscriptStore


@pytest.mark.parametrize("operation", ["replay", "latest_sequence", "count"])
def test_same_connection_read_cannot_observe_rolled_back_append(
    tmp_path: Path, operation: str,
) -> None:
    store = TranscriptStore(tmp_path / "events.db", retention=100)
    other = TranscriptStore(tmp_path / "events.db", retention=100)
    entered = threading.Event()
    release = threading.Event()
    read_started = threading.Event()
    read_finished = threading.Event()
    try:
        for _ in range(199):
            store.append(session_id="s", kind="ToolCallProgress", payload={})
        # A real SQLite rollback, not a mocked connection or transaction.
        store._conn.execute(
            "CREATE TRIGGER peer_fail_prune BEFORE DELETE ON events "
            "BEGIN SELECT RAISE(ABORT, 'peer synthetic prune failure'); END"
        )
        original_prune = store._prune_session

        def delayed_prune(session_id: str) -> None:
            entered.set()
            assert release.wait(5), "writer release timed out"
            original_prune(session_id)

        def read(target: TranscriptStore):
            if operation == "replay":
                return list(target.replay("s", from_sequence=199))
            return getattr(target, operation)("s")

        def concurrent_read():
            read_started.set()
            try:
                return read(store)
            finally:
                read_finished.set()

        with patch.object(store, "_prune_session", side_effect=delayed_prune):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(
                    store.append, session_id="s", kind="ToolCallProgress",
                    payload={}, event_id="must-rollback",
                )
                try:
                    assert entered.wait(5), "writer did not reach pruning"
                    committed = read(other)
                    reader = pool.submit(concurrent_read)
                    assert read_started.wait(5), "reader did not start"
                    # Permit a faulty shared-connection read to finish while the
                    # transaction is still open. Correct reads wait for rollback.
                    read_finished.wait(0.25)
                finally:
                    release.set()
                with pytest.raises(sqlite3.IntegrityError, match="peer synthetic"):
                    writer.result(timeout=5)
                observed = reader.result(timeout=5)
        assert observed == committed, "reader exposed an event that SQLite rolled back"
        assert store.count("s") == other.count("s") == 199
        assert store.latest_sequence("s") == 198
    finally:
        release.set()
        other.close()
        store.close()
