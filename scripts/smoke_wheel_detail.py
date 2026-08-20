"""Clean-wheel smoke: mount ConnectionDetailScreen from an installed wheel.

Runs headlessly against the ``karox`` that imports from this interpreter's
site-packages (the freshly built wheel in an isolated venv). No real credential
is used: an isolated config tree holds a fake registry/credential reference.

Exit code 0 means the Hub -> Detail -> Esc journey mounted and stayed alive for
every record shape, three times each.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Isolate config/runtime BEFORE importing karox, so it never touches the user's
# real profiles or OS-keyring credentials. Same overrides the test harness uses.
_ROOT = Path(tempfile.mkdtemp(prefix="karox-smoke-"))
(_ROOT / "config").mkdir(parents=True, exist_ok=True)
(_ROOT / "runtime").mkdir(parents=True, exist_ok=True)
os.environ["KAROX_VNEXT_CONFIG_DIR"] = str(_ROOT / "config")
os.environ["KAROX_CONFIG_DIR"] = str(_ROOT / "config")
os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(_ROOT / "runtime")
os.environ["KAROX_RUNTIME_DIR"] = str(_ROOT / "runtime")

from karox import tui  # noqa: E402 - must follow env setup
from karox.connections import (  # noqa: E402
    McpClientTarget,
    connection_registry,
)
from karox.paths import config_dir  # noqa: E402
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry  # noqa: E402

CID = "c0ffee1234deadbe"


def seed_provider(provider_id: str = "openrouter") -> None:
    reg = ProviderRegistry(config_dir() / "vnext" / "providers.json")
    reg.put_provider(
        ProviderRecord(
            provider_id=provider_id,
            adapter_kind="openai_compatible_chat",
            base_url=f"https://{provider_id}.example/v1",
            privacy_class="public",
        )
    )
    reg.put_model(ModelRecord(provider_id, "claude-opus"))


def seed_service(preset_id: str = "clickup") -> None:
    connection_registry().put(
        McpClientTarget(
            connection_id=CID,
            name="My ClickUp",
            preset_id=preset_id,
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel="cloudflare",
            runtime_profile="generic-streamable-http",
            public_url="https://bridge.example.com",
            url_stability="temporary",
            credential_ref=f"os-keyring:connection/{CID}",
            credential_fingerprint="sha256:fixture",
            port=8765,
        )
    )


async def run_one(label: str, *, kind: str, identity: str, seed) -> bool:
    app = tui.KaroXApp(Path(tempfile.mkdtemp()), language="en")
    try:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            seed()
            screens = app._connections_screens_cached()
            screen = screens["ConnectionDetailScreen"](
                "en", kind=kind, identity=identity
            )
            app.push_screen(screen)
            await pilot.pause()
            ok = (
                type(app.screen).__name__ == "ConnectionDetailScreen"
                and app.screen.is_running is True
            )
            await pilot.press("escape")
            await pilot.pause()
            print(f"  {label}: mounted={ok} is_running={app.screen.is_running if hasattr(app.screen,'is_running') else '?'}")
            return ok
    except Exception as exc:  # pragma: no cover - smoke
        print(f"  {label}: CRASH {type(exc).__name__}: {exc}")
        return False
    finally:
        # The app's teardown is async and already handled by the context
        # manager's exit; nothing to call here. Kept as a guard only.
        pass


async def main() -> int:
    scenarios = [
        ("provider", {"kind": "provider", "identity": "openrouter", "seed": seed_provider}),
        ("stopped-clickup", {"kind": "service", "identity": CID, "seed": lambda: seed_service("clickup")}),
        ("stopped-chatgpt", {"kind": "service", "identity": CID, "seed": lambda: seed_service("chatgpt-web")}),
    ]
    all_ok = True
    for label, cfg in scenarios:
        print(f"[{label}]")
        for _ in range(3):
            if not await run_one(label, **cfg):
                all_ok = False
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
