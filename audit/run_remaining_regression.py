"""Run one frozen group of the remaining KaroX regression suite.

Serial Windows execution, bounded time and process tree, no model calls.
Source inputs are loaded from REGRESSION_SPLIT_PLAN_20261108.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "audit" / "REGRESSION_SPLIT_PLAN_20261108.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("group", type=int)
    parser.add_argument("--limit", type=int, default=180)
    args = parser.parse_args()
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if not 0 <= args.group < len(plan["groups"]):
        parser.error("Group out of range")
    group = plan["groups"][args.group]
    if shutil.disk_usage("C:\\").free < 7 * 1024 ** 3:
        raise SystemExit("INFRA_BLOCKED: C drive reserve under 7 GiB")
    if shutil.disk_usage("D:\\").free < 15 * 1024 ** 3:
        raise SystemExit("INFRA_BLOCKED: D drive reserve under 15 GiB")
    if psutil.virtual_memory().available < 3 * 1024 ** 3:
        raise SystemExit("INFRA_BLOCKED: RAM reserve under 3 GiB")
    record = ROOT / "audit" / f"REGRESSION_GROUP_{args.group}_20261108.json"
    logpath = ROOT / "audit" / f"regression_group_{args.group}_20261108.log"
    if record.exists() or logpath.exists():
        raise SystemExit(f"Refusing overwrite of existing record/log: {record}, {logpath}")
    command = [sys.executable, "-m", "pytest", "-q", "-ra", "--durations=10",
               "-o", "addopts=", *group["files"]]
    started = time.monotonic()
    timed_out = False
    with logpath.open("wb") as logfile:
        proc = subprocess.Popen(command, cwd=ROOT, stdout=logfile, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            code = proc.wait(timeout=args.limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                rootproc = psutil.Process(proc.pid)
                descendants = rootproc.children(recursive=True)
                for child in descendants:
                    child.terminate()
                _, alive = psutil.wait_procs(descendants, timeout=3)
                for child in alive:
                    child.kill()
            except psutil.NoSuchProcess:
                pass
            proc.kill()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
            code = 124
    duration = round(time.monotonic() - started, 3)
    log = logpath.read_bytes()
    record_data = {
        "group": args.group,
        "files": group["files"],
        "expected_count": group["test_count"],
        "command": command,
        "exit": code,
        "timed_out": timed_out,
        "seconds": duration,
        "log_path": str(logpath.relative_to(ROOT)),
        "log_bytes": len(log),
        "log_sha256": hashlib.sha256(log).hexdigest(),
        "log_tail": log[-3500:].decode("utf-8", errors="replace"),
    }
    record.write_text(json.dumps(record_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: record_data[k] for k in (
        "group", "expected_count", "exit", "timed_out", "seconds", "log_bytes", "log_sha256",
        "log_tail")}, indent=2, ensure_ascii=False), flush=True)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
