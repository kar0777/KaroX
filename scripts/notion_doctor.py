#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path

from patch_notion_provider import patch_ps, patch_sh
from product_doctor import resolve_server_dir


def check_module(name: str) -> dict[str, object]:
    return {"name": name, "ok": importlib.util.find_spec(name) is not None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    checks: list[dict[str, object]] = []

    try:
        server_dir = resolve_server_dir(root)
        checks.append({"name": "Resolved server directory", "ok": True, "path": str(server_dir)})
    except OSError as exc:
        server_dir = root / "server"
        checks.append({"name": "Resolved server directory", "ok": False, "error": str(exc)})

    for rel in (
        "start.core.ps1",
        "start.core.sh",
        "scripts/patch_notion_provider.py",
        "scripts/notion_profile.py",
        "scripts/product_doctor.py",
        "scripts/karox_admin.py",
        "NOTION.md",
    ):
        path = root / rel
        checks.append({"name": rel, "ok": path.is_file()})

    for rel in (
        "repo_tools.py",
        "app_entry.py",
        "mcp_host_security.py",
        "notion_gateway.py",
        "notion_entry.py",
    ):
        path = server_dir / rel
        checks.append({"name": f"server/{rel}", "ok": path.is_file(), "path": str(path)})

    checks.extend(check_module(name) for name in ("fastapi", "uvicorn", "httpx", "mcp"))

    try:
        ps = (root / "start.core.ps1").read_text(encoding="utf-8-sig")
        sh = (root / "start.core.sh").read_text(encoding="utf-8-sig")
        ps_out = patch_ps(ps, str(root))
        sh_out = patch_sh(sh, str(root))
        checks.append({
            "name": "PowerShell provider patch",
            "ok": all(
                marker in ps_out
                for marker in (
                    "Notion Custom Agent",
                    "app_entry:app",
                    "notion_entry:app",
                    "Get-PersistentNotionProfile",
                    "karox-notion-stable",
                )
            ),
        })
        checks.append({
            "name": "POSIX provider patch",
            "ok": all(
                marker in sh_out
                for marker in (
                    "Notion Custom Agent",
                    "app_entry:app",
                    "notion_entry:app",
                    "persistent_notion_profile_json",
                    "karox-notion-stable",
                )
            ),
        })
        with tempfile.TemporaryDirectory() as tmp:
            generated_ps = Path(tmp, "generated.ps1")
            generated_sh = Path(tmp, "generated.sh")
            generated_ps.write_text(ps_out, encoding="utf-8")
            generated_sh.write_text(sh_out, encoding="utf-8")
            checks.append({"name": "Generated PowerShell launcher", "ok": generated_ps.stat().st_size > 1000})
            checks.append({"name": "Generated POSIX launcher", "ok": generated_sh.stat().st_size > 1000})
    except (OSError, RuntimeError) as exc:
        checks.append({"name": "Provider patch generation", "ok": False, "error": str(exc)})

    try:
        gateway = (server_dir / "notion_gateway.py").read_text(encoding="utf-8")
        host_security = (server_dir / "mcp_host_security.py").read_text(encoding="utf-8")
        checks.append({
            "name": "Tunnel-aware MCP Host validation",
            "ok": (
                "is_allowed_mcp_host(host)" in gateway
                and "enable_dns_rebinding_protection=False" in gateway
                and ".trycloudflare.com" in host_security
                and ".ts.net" in host_security
            ),
        })
        checks.append({
            "name": "MCP Bearer token remains mandatory",
            "ok": "hmac.compare_digest" in gateway and "status_code=401" in gateway,
        })
    except OSError as exc:
        checks.append({"name": "MCP Host security", "ok": False, "error": str(exc)})

    ok = all(bool(item.get("ok")) for item in checks)
    print("KaroX Notion provider doctor")
    for item in checks:
        marker = "OK" if item.get("ok") else "FAIL"
        detail = f": {item.get('error')}" if item.get("error") else ""
        print(f"[{marker}] {item['name']}{detail}")
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
