"""Real Textual interaction benchmark for the API-provider setup flow.

Drives the production ProviderSetupScreen with Textual Pilot and real
ProviderController persistence against an isolated temporary registry and
in-memory keyring. Only provider network discovery/probe are deterministic fakes.
"""
from __future__ import annotations
import asyncio, json, sys, time
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "tests", ROOT / "src"):
    if str(p) not in sys.path: sys.path.insert(0, str(p))
from _tui_harness import isolated_karox_directories  # noqa: E402
from karox import tui  # noqa: E402
from karox.credentials import CredentialStore  # noqa: E402
from karox.provider_controller import ProviderController  # noqa: E402
from karox.registry import ProviderRegistry  # noqa: E402
class MemoryBackend:
    def __init__(self): self.values = {}
    def set(self, service, account, secret): self.values[(service, account)] = secret
    def get(self, service, account): return self.values.get((service, account))
    def delete(self, service, account): self.values.pop((service, account), None)
@dataclass(frozen=True)
class Result:
    obvious_enter_opens_model_picker: bool
    actions_until_model_picker: int | None
    durable_before_model_selection: bool
    key_field_cleared_before_model_selection: bool
    provider_save_visible_initially: bool
    visible_action_count_initially: int
    selected_model_after_happy_path: str | None
    durable_after_happy_path: bool
    actions_until_selected_model: int | None
    elapsed_ms: float
def _visible(widget): return str(widget.styles.display) != "none"
def _preset():
    return next(item for item in tui.provider_presets() if item.preset_id == "openrouter")
async def _wait(predicate, pilot, timeout=2.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        await pilot.pause(0.05)
        if predicate(): return True
    return False
async def run():
    started = time.perf_counter()
    with isolated_karox_directories() as repository:
        registry = ProviderRegistry(repository.parent / "config" / "vnext" / "providers.json")
        controller = ProviderController(registry=registry, credentials=CredentialStore(MemoryBackend()))
        discovery = tui.ModelDiscovery(models=(tui.DiscoveredModel("stealth/ox-alpha", 1_048_576, 16_384, tools="true", vision="true", structured_output="true", streaming="true", input_per_million=0.0, output_per_million=0.0, free=True),), base_url="https://openrouter.ai/api/v1", attempted_urls=("https://openrouter.ai/api/v1",))
        with patch.object(tui, "_load_language", return_value="en"), patch.object(tui, "_selected_model", return_value=None), patch.object(tui, "_provider_controller", return_value=controller), patch.object(tui, "_discover_models_result", return_value=discovery), patch.object(tui, "_probe_provider", return_value={"finish_reason":"stop","usage":{"total_tokens":3},"transport_attempts":1}):
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120,42)) as pilot:
                await pilot.pause(0.2)
                screen = tui.ProviderSetupScreen("en", _preset()); app.push_screen(screen); await pilot.pause(0.2)
                visible_buttons = [b for b in screen.query(tui.Button) if _visible(b)]
                save_visible = _visible(screen.query_one("#provider-save", tui.Button))
                key = screen.query_one("#provider-key", tui.Input); key.value = "benchmark-key-not-a-real-credential"; key.focus()
                actions = 1
                await pilot.press("enter"); actions += 1
                opened_on_enter = await _wait(lambda: isinstance(app.screen, tui.ModelPickerScreen), pilot, 0.8)
                if not opened_on_enter:
                    await pilot.press("f5"); actions += 1
                    await _wait(lambda: isinstance(app.screen, tui.ModelPickerScreen), pilot)
                actions_to_picker = actions if isinstance(app.screen, tui.ModelPickerScreen) else None
                try: details = controller.details("openrouter")
                except Exception: durable_before = False
                else: durable_before = bool(details.credential.get("available"))
                key_cleared = screen.query_one("#provider-key", tui.Input).value == ""
                if isinstance(app.screen, tui.ModelPickerScreen):
                    # The first discovered model is already highlighted; Enter is the obvious apply action.
                    await pilot.press("enter"); actions += 1
                    await _wait(lambda: not isinstance(app.screen, tui.ModelPickerScreen), pilot); await pilot.pause(0.3)
                selected = registry.selected_model()
                try: durable_after = bool(controller.details("openrouter").credential.get("available"))
                except Exception: durable_after = False
                return Result(opened_on_enter, actions_to_picker, durable_before, key_cleared, save_visible, len(visible_buttons), f"{selected.provider_id}/{selected.model_id}" if selected else None, durable_after, actions if selected else None, round((time.perf_counter()-started)*1000,2))
def main(): print(json.dumps(asdict(asyncio.run(run())), ensure_ascii=False, indent=2, sort_keys=True))
if __name__ == "__main__": main()
