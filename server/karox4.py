"""KaroX 4.0 aggregator: imports all phase modules in dependency order.

Loaded from the tail of repo_tools.py inside try/except so a failure in any
phase never takes down the v3 core server.
"""
from __future__ import annotations

import repo_tools as core

import karox4_core  # Phase 0: sessions, resume, idempotency, queue, events, watchdog ping
import karox4_exec  # Phase 1: argv exec, UTF-8, async jobs, waits, checks v2
import karox4_files  # Phase 2: bytes/images, fs ops, apply_patch, checkpoints, search v2
import karox4_git  # Phase 3: git full cycle, secret-scan v2, hunk commits
import karox4_web  # Phase 4: dev-server, http_fetch, browser, package managers
import karox4_desktop  # Phase 5: screen capture/record, opt-in input
import karox4_debug  # Phase 6: REPL, DAP bridge
import karox4_context  # Phase 7: project map, memos, workspaces

KAROX4_MODULES = [
    "karox4_core",
    "karox4_exec",
    "karox4_files",
    "karox4_git",
    "karox4_web",
    "karox4_desktop",
    "karox4_debug",
    "karox4_context",
]

core.audit("karox4_loaded", {"modules": KAROX4_MODULES, "version": core.VERSION})
