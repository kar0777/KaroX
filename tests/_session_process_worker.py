"""Cross-process lease worker used by the SessionStore regression tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from karox.sessions import SessionStore


def main() -> int:
    root = Path(sys.argv[1])
    ready = Path(sys.argv[2])
    release = Path(sys.argv[3])
    store = SessionStore(root)
    lease = store.acquire("sample", "worker", ttl_seconds=5)
    try:
        record = store.load("sample")
        record.summary = "saved by worker"
        store.save(record, record.revision, lease)
        store.heartbeat(lease, ttl_seconds=5)
        ready.touch()
        deadline = time.time() + 10
        while not release.exists() and time.time() < deadline:
            time.sleep(0.05)
            store.heartbeat(lease, ttl_seconds=5)
        return 0 if release.exists() else 2
    finally:
        store.release(lease)


if __name__ == "__main__":
    raise SystemExit(main())
