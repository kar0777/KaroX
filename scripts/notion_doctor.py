#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path

from patch_notion_provider import patch_ps, patch_sh


def check_module(name: str) -> dict[str, object]:
    return {"name": name, "ok": importlib.util.find_spec(name) is not None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    checks: list[dict[str, object]] = []

    for rel in (
        "start.core.ps1",
        "start.core.sh",
        "scripts/patch_notion_provider.py",
        "scripts/karox_admin.py",
        "server/repo_tools.py",
        "server/app_entry.py",
        "server/notion_gateway.py",
        "server/notion_entry.py",
        "NOTION.md",
    ):
        path = root / rel
        checks.append({"name": rel, "ok": path.is_file()})

    checks.extend(check_module(name) for name in ("fastapi", "uvicorn", "httpx", "mcp"))

    try:
        ps = (root / "start.core.ps1").read_text(encoding="utf-8-sig")
        sh = (root / "start.core.sh").read_text(encoding="utf-8-sig")
        ps_out = patch_ps(ps, str(root))
        sh_out = patch_sh(sh, str(root))
        checks.append({
            "name": "PowerShell provider patch",
            "ok": "Notion Custom Agent" in ps_out and "app_entry:app" in ps_out and "notion_entry:app" in ps_out,
        })
        checks.append({
            "name": "POSIX provider patch",
            "ok": "Notion Custom Agent" in sh_out and "app_entry:app" in sh_out and "notion_entry:app" in sh_out,
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
