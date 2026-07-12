#!/usr/bin/env python3
"""Cross-platform installation doctor for KaroX.

Unlike the legacy PowerShell doctor, this resolves both the normal source layout
(`server/repo_tools.py`) and the historical nested Windows install layout
(`server/server/repo_tools.py`). It performs safe static/runtime checks without
opening a repository session or binding a public tunnel.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import py_compile
from pathlib import Path
from typing import Any


def resolve_server_dir(root: Path) -> Path:
    candidates = (root / "server", root / "server" / "server", root)
    for candidate in candidates:
        if (candidate / "repo_tools.py").is_file():
            return candidate.resolve()
    checked = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"repo_tools.py was not found. Checked: {checked}")


def add(checks: list[dict[str, Any]], name: str, ok: bool, detail: str = "", *, warning: bool = False) -> None:
    checks.append({"name": name, "ok": bool(ok), "warning": bool(warning), "detail": detail})


def compile_file(path: Path) -> tuple[bool, str]:
    try:
        py_compile.compile(str(path), doraise=True)
        return True, str(path)
    except (OSError, py_compile.PyCompileError) as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a KaroX installation.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    checks: list[dict[str, Any]] = []

    add(checks, "Application root", root.is_dir(), str(root))
    try:
        server_dir = resolve_server_dir(root)
        add(checks, "Server module directory", True, str(server_dir))
        nested = server_dir == (root / "server" / "server").resolve()
        if nested:
            add(
                checks,
                "Legacy nested server layout",
                True,
                "Supported and detected. A later reinstall may flatten it automatically.",
                warning=True,
            )
    except FileNotFoundError as exc:
        server_dir = root / "server"
        add(checks, "Server module directory", False, str(exc))

    required_root_files = (
        "start.ps1",
        "start.sh",
        "start.core.ps1",
        "start.core.sh",
        "requirements.txt",
        "VERSION",
        "scripts/patch_notion_provider.py",
        "scripts/notion_profile.py",
        "scripts/tailscale_readiness.py",
        "scripts/notion_doctor.py",
    )
    for relative in required_root_files:
        path = root / relative
        add(checks, relative, path.is_file(), str(path))

    server_files = (
        "repo_tools.py",
        "app_entry.py",
        "notion_gateway.py",
        "notion_entry.py",
        "mcp_host_security.py",
    )
    for name in server_files:
        path = server_dir / name
        add(checks, f"server/{name}", path.is_file(), str(path))
        if path.is_file():
            ok, detail = compile_file(path)
            add(checks, f"compile {name}", ok, detail)

    script_files = (
        "patch_notion_provider.py",
        "notion_profile.py",
        "tailscale_readiness.py",
        "notion_doctor.py",
        "karox_cli.py",
        "support_bundle.py",
    )
    for name in script_files:
        path = root / "scripts" / name
        if path.is_file():
            ok, detail = compile_file(path)
            add(checks, f"compile scripts/{name}", ok, detail)

    for module in ("fastapi", "uvicorn", "httpx", "mcp", "pydantic"):
        add(checks, f"Python dependency: {module}", importlib.util.find_spec(module) is not None)

    version_path = root / "VERSION"
    version = version_path.read_text(encoding="utf-8-sig").strip() if version_path.is_file() else "unknown"
    failures = [item for item in checks if not item["ok"]]
    warnings = [item for item in checks if item["warning"]]
    payload = {
        "ok": not failures,
        "version": version,
        "root": str(root),
        "serverDir": str(server_dir),
        "failures": len(failures),
        "warnings": len(warnings),
        "checks": checks,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"KaroX doctor · v{version}")
        print(f"Application: {root}")
        for item in checks:
            marker = "WARN" if item["warning"] else ("OK" if item["ok"] else "FAIL")
            detail = f" — {item['detail']}" if item["detail"] else ""
            print(f"[{marker}] {item['name']}{detail}")
        print()
        if failures:
            print(f"KaroX doctor found {len(failures)} error(s).")
        else:
            print("KaroX doctor completed successfully.")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
