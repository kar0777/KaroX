#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
import tempfile
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
    assert '"app_entry:app"' in ps_out
    assert '"notion_entry:app"' in ps_out
    assert "printf notion" in sh_out
    assert '$tunnel_url/mcp' in sh_out
    assert 'server_app="app_entry:app"' in sh_out
    assert 'server_app="notion_entry:app"' in sh_out

    for relative in (
        "server/repo_tools.py",
        "server/app_entry.py",
        "server/notion_gateway.py",
        "server/notion_entry.py",
        "scripts/karox_admin.py",
    ):
        ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)

    with tempfile.TemporaryDirectory() as temp:
        generated_ps = Path(temp) / "generated.ps1"
        generated_sh = Path(temp) / "generated.sh"
        generated_ps.write_text(ps_out, encoding="utf-8")
        generated_sh.write_text(sh_out, encoding="utf-8")
        assert generated_ps.stat().st_size > 1000
        assert generated_sh.stat().st_size > 1000

    print("KaroX provider source checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
