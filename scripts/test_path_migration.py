#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import karox_paths


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_config = root / "legacy-config"
        old_runtime = root / "legacy-runtime"
        new_config = root / "KaroX-config"
        new_runtime = root / "KaroX-runtime"
        old_config.mkdir(parents=True)
        (old_runtime / "sessions" / "s1").mkdir(parents=True)
        (old_config / "settings.json").write_text(json.dumps({"language": "ru"}), encoding="utf-8")
        (old_config / "notion-connection.json").write_text(json.dumps({"protectedKey": "opaque-dpapi"}), encoding="utf-8")
        legacy_path = str(old_runtime / "sessions" / "s1" / "logs")
        (old_runtime / "sessions" / "s1" / "session.json").write_text(json.dumps({"sessionDir": legacy_path}), encoding="utf-8")

        old_env = dict(os.environ)
        try:
            os.environ["KAROX_CONFIG_DIR"] = str(new_config)
            os.environ["KAROX_RUNTIME_DIR"] = str(new_runtime)
            os.environ["KAROX_LEGACY_CONFIG_DIR"] = str(old_config)
            os.environ["KAROX_LEGACY_RUNTIME_DIR"] = str(old_runtime)
            result = karox_paths.migrate_legacy()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result["ok"] is True
        assert (new_config / "settings.json").is_file()
        assert (new_config / "notion-connection.json").is_file()
        session = json.loads((new_runtime / "sessions" / "s1" / "session.json").read_text(encoding="utf-8"))
        assert str(new_runtime) in session["sessionDir"]
        assert "RepoPilotBridge" not in session["sessionDir"]
        assert json.loads((new_config / "notion-connection.json").read_text(encoding="utf-8"))["protectedKey"] == "opaque-dpapi"

    print("KaroX path migration checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
