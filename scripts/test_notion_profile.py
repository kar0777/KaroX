#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "notion_profile.py"


def call(config: Path, *args: str, expect: int = 0) -> dict[str, object] | str:
    env = dict(os.environ)
    env["KAROX_CONFIG_DIR"] = str(config)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert result.returncode == expect, (result.stdout, result.stderr)
    text = result.stdout.strip()
    return json.loads(text) if "--json" in args else text


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        config = Path(temp) / "config"
        first = call(config, "ensure", "--json", "--include-key")
        second = call(config, "ensure", "--json", "--include-key")
        assert isinstance(first, dict) and isinstance(second, dict)
        assert first["apiKey"] == second["apiKey"], "persistent key changed between launches"
        assert len(str(first["apiKey"])) >= 48
        assert not first["mcpUrl"]

        url = "https://workstation.example-tailnet.ts.net"
        saved = call(config, "set-url", "--url", url, "--json")
        assert isinstance(saved, dict)
        assert saved["mcpUrl"] == url + "/mcp"

        connection = call(config, "connection", "--json", "--show-token")
        assert isinstance(connection, dict)
        assert connection["apiKey"] == first["apiKey"]
        assert connection["mcpUrl"] == url + "/mcp"

        rotated = call(config, "rotate", "--json")
        after_rotate = call(config, "connection", "--json", "--show-token")
        assert isinstance(rotated, dict) and isinstance(after_rotate, dict)
        assert after_rotate["apiKey"] != first["apiKey"]
        assert after_rotate["mcpUrl"] == url + "/mcp"

        call(config, "reset")
        missing = call(config, "status", "--json", expect=1)
        assert isinstance(missing, dict) and missing["configured"] is False

    print("KaroX persistent Notion profile checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
