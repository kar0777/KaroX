#!/usr/bin/env python3
from __future__ import annotations

import ast
import codecs
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patch_notion_provider import patch_ps, patch_sh  # noqa: E402


def run_patcher(platform: str, source: Path, output: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "patch_notion_provider.py"),
            "--platform",
            platform,
            "--source",
            str(source),
            "--output",
            str(output),
            "--root",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"patcher failed for {platform}:\n{result.stdout}\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    ps_source = ROOT / "start.core.ps1"
    sh_source = ROOT / "start.core.sh"
    ps = ps_source.read_text(encoding="utf-8-sig")
    sh = sh_source.read_text(encoding="utf-8-sig")
    ps_out = patch_ps(ps, str(ROOT))
    sh_out = patch_sh(sh, str(ROOT))

    assert 'return "notion"' in ps_out
    assert 'server URL: $tunnelUrl/mcp' in ps_out
    assert '"app_entry:app"' in ps_out
    assert '"notion_entry:app"' in ps_out
    assert "Нативная командная AI-среда" in ps_out
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
        first_ps = run_patcher("powershell", ps_source, generated_ps)
        first_sh = run_patcher("shell", sh_source, generated_sh)
        assert first_ps["reused"] is False
        assert first_sh["reused"] is False

        ps_bytes = generated_ps.read_bytes()
        sh_bytes = generated_sh.read_bytes()
        assert ps_bytes.startswith(codecs.BOM_UTF8), "PowerShell output must contain a UTF-8 BOM for Windows PowerShell 5.1"
        assert not sh_bytes.startswith(codecs.BOM_UTF8), "Shell output must remain plain UTF-8 without a BOM"

        generated_ps_text = generated_ps.read_text(encoding="utf-8-sig")
        generated_sh_text = generated_sh.read_text(encoding="utf-8")
        assert "Нативная командная AI-среда" in generated_ps_text
        assert "Д/н" in generated_ps_text
        assert "РќР°С‚РёРІРЅ" not in generated_ps_text
        assert 'server URL: $tunnelUrl/mcp' in generated_ps_text
        assert "printf notion" in generated_sh_text

        # Simulate the exact v3.12.0 cache: valid UTF-8 text, but no BOM.
        generated_ps.write_text(generated_ps_text, encoding="utf-8", newline="\n")
        assert not generated_ps.read_bytes().startswith(codecs.BOM_UTF8)
        migrated = run_patcher("powershell", ps_source, generated_ps)
        assert migrated["reused"] is False, "A BOM-less legacy launcher must be rewritten"
        assert generated_ps.read_bytes().startswith(codecs.BOM_UTF8), "Migration must restore the BOM"

        reused = run_patcher("powershell", ps_source, generated_ps)
        assert reused["reused"] is True, "A correctly encoded launcher should be reused"
        assert generated_ps.stat().st_size > 1000
        assert generated_sh.stat().st_size > 1000

    print("KaroX provider source, encoding and cache-migration checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
