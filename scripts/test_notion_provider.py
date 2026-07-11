#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patch_notion_provider import patch_ps, patch_sh  # noqa: E402


def main() -> int:
    ps = (ROOT / "start.core.ps1").read_text(encoding="utf-8-sig")
    sh = (ROOT / "start.core.sh").read_text(encoding="utf-8-sig")
    ps_out = patch_ps(ps, str(ROOT))
    sh_out = patch_sh(sh, str(ROOT))
    assert 'return "notion"' in ps_out
    assert 'server URL: $tunnelUrl/mcp' in ps_out
    assert 'notion_gateway:app' in ps_out
    assert "printf notion" in sh_out
    assert '$tunnel_url/mcp' in sh_out
    assert 'notion_gateway:app' in sh_out
    ast.parse((ROOT / "server" / "notion_gateway.py").read_text(encoding="utf-8"))
    print("Notion provider source checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
