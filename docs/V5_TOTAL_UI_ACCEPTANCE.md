# KaroX v5 — Total UI Acceptance

Deterministic inventory of every shipping user-facing surface and its
acceptance state. Generated from production code (every `Screen`/`ModalScreen`
class under `src/karox/`), not from memory. Updated 2026-08-21 during the
total product QA + UX hardening pass (workstream `release-live-pass3`).

Legend: **Func** = dedicated functional suite green · **Size** = exercised by
`tests/test_tui_total_size_acceptance.py` (40x12, 46x14, 60x18, 73x19, 80x24,
100x28, 120x30, 160x45; RU at every size, EN sampled) · **Scroll** = dialog
scrolls when content overflows (no clipped unreachable controls).

## Owner-found live defects fixed in this pass

| # | Defect | Root cause | Fix | Regression |
|---|--------|-----------|-----|------------|
| 1 | Selecting `faceboooook` degraded the shared bridge card to "not configured / Launch KaroX" | `ServiceConnectScreen` discovery matched only the saved profile's anchor repository | Discovery now matches any entry of the profile's approved project registry; a project outside the registry still never matches (B7 no-collapse preserved) | `tests/test_tui_shared_bridge_project_switch.py` |
| 2 | Small connected ChatGPT window hid lower controls with no usable scroll | `#svc-dialog` was a plain clipped `Vertical` | `VerticalScroll` + focus-scrolls-into-view | same file, small-terminal test |
| 3 | Coverage run stalled around `test_repo_context` / `git init` / `communicate` | Inherited user gitconfig (locked `~/.gitconfig`; `core.fsmonitor` daemon holding pipes on Windows) | Test git sandbox now nulls `GIT_CONFIG_GLOBAL/SYSTEM`, disables prompts, bounds `git init` at 120s with an actionable error | `tests/_support.py`, measured 7.4s normal run |

## Defects found and fixed by the new size-acceptance gate

| Screen | Defect at small sizes | Fix |
|--------|----------------------|-----|
| ConnectionHubScreen | title/list/hint clipped, no scroll (40x12–73x19) | `#connhub-dialog` → `VerticalScroll` |
| WorkspaceManagerScreen (Ctrl+W) | Use/Default/Remove/Back button row clipped, no scroll | `#workspace-manager` → `VerticalScroll` |
| ModelProvidersScreen | fixed `width: 86` wider than 40–80 col terminals; bottom buttons clipped | responsive `width: 100%; max-width: 86`, `height: auto; max-height: 90%`, list capped `max-height: 16`, dialog → `VerticalScroll` |

## Surface inventory (35 screens + root)

### Root / chat shell (`tui.py`)
| Surface | Func | Size | Scroll | Evidence / notes |
|---------|------|------|--------|------------------|
| Root app (home, composer, status line) | yes | yes | n/a | `test_tui.py`, `test_tui_layout.py`, `test_tui_minimal_shell.py`, `test_tui_activity_line.py`, `test_tui_keyboard_responsiveness.py`; mounts at all 8 sizes in size gate |
| LanguageScreen | yes | default | fits | `test_tui.py` first-run flow |
| ConnectionChoiceScreen | yes | default | fits | `test_tui_connections.py` |
| ProviderPresetScreen | yes | default | fits | `test_tui_provider_flow.py` |
| PuterInfoScreen | yes | default | fits | `test_tui_provider_flow.py` |
| ModelPickerScreen (tui) | yes | default | list scrolls | `test_tui_model_picker.py` |
| ProviderLimitsScreen | yes | default | fits | `test_tui_provider_flow.py` |
| ManualModelScreen | yes | default | fits | `test_tui_provider_flow.py` |
| ProviderSetupScreen | yes | default | fields scroll | `test_tui_provider_flow.py`, `test_tui_copy_auth_action.py` |
| BridgeSetupScreen | yes | default | yes (`VerticalScroll #bridge-dialog`) | `test_web_bridge_launcher.py`, `test_tui_bridge_secret_output.py` |
| ConfirmScreen | yes | default | fits | `test_tui.py` destructive flows |
| SessionBrowserScreen | yes | default + compact | list scrolls | `test_tui_session_browser.py`, `test_tui_session_browser_compact.py` |
| SessionDetailScreen | yes | default | yes (`#session-detail-body`) | `test_tui_session_detail.py`, `test_tui_session_detail_overview.py`, `test_tui_session_view.py` |

### Connections (`tui_connections.py`)
| Surface | Func | Size | Scroll | Evidence / notes |
|---------|------|------|--------|------------------|
| ConnectionHubScreen | yes | yes (8 sizes) | **fixed this pass** | `test_tui_connection_hub.py`, size gate |
| McpClientsScreen | yes | default | list scrolls | `test_tui_connections.py` |
| _PresetPickerScreen | yes | default | fits | `test_tui_connection_management.py` |
| _McpClientFormScreen | yes | default | yes (`#mcp-form-scroll`) | `test_tui_connection_management.py` |
| _ClickupAutoScreen | yes | default | yes (`#cu-auto-advanced`) | `test_tui_clickup.py`, `test_tui_clickup_connection_screen.py`, `test_clickup_setup.py` |
| _ClickupResultScreen | yes | default | fits | `test_tui_clickup.py` |
| _ProviderDetailsScreen | yes | default | fits | `test_tui_provider_flow.py` |
| _ProviderEditScreen | yes | default | fits | `test_tui_provider_flow.py` |
| ModelProvidersScreen | yes | yes (8 sizes) | **fixed this pass** | `test_tui_provider_flow.py`, size gate |
| _ConfirmScreen | yes | default | fits | `test_tui_connection_detail_lifecycle.py`; safe default, Esc cancels |
| ServicePickerScreen | yes | default | list scrolls | `test_tui_service_flow.py` |
| ServiceConnectScreen | yes | yes (8 sizes) | **fixed this pass** | `test_tui_service_flow.py`, `test_tui_shared_bridge_project_switch.py`, size gate |
| ServiceMoreScreen | yes | default | fits | `test_tui_connect_polish.py` (progressive disclosure: More/Advanced) |
| ServiceDiagnosticsScreen | yes | default | yes (`#svc-diag-tech`) | `test_tui_connect_polish.py` |
| PermissionDetailScreen | yes | default | fits | `test_tui_connect_polish.py` |
| BrowserCredentialManagerScreen | yes | default | list scrolls | `test_browser_credential_cli.py`, `test_browser_credential_runtime.py` |
| SavedWebProfileSettingsScreen | yes | default | yes (`#swp-body`) | `test_tui_saved_web_profile_settings.py` |
| AdvancedSettingsScreen | yes | default | yes (`#adv-body`) | `test_tui_advanced_connections.py` |
| ConnectionDetailScreen | yes | default | action list scrolls | `test_tui_connection_detail_lifecycle.py`, `test_tui_connection_copy_e2e.py` |

### Dashboard (`tui_dashboard.py`)
| Surface | Func | Size | Scroll | Evidence / notes |
|---------|------|------|--------|------------------|
| ModelPickerScreen (dashboard) | yes | default | list scrolls | `test_tui_dashboard.py` |
| UsageCostScreen | yes | default | fits | `test_tui_dashboard.py` |

### Workspace (`tui_workspace.py`)
| Surface | Func | Size | Scroll | Evidence / notes |
|---------|------|------|--------|------------------|
| WorkspacePickerScreen | yes | default | list scrolls | `test_workspace_picker_v1.py` |
| WorkspaceManagerScreen (Ctrl+W) | yes | yes (8 sizes) | **fixed this pass** | `test_workspace_picker_v1.py`, size gate; long/Cyrillic/space paths covered by project-switch regression |

## Keyboard acceptance
- Esc closes every screen exercised by the size gate at every size (asserted).
- Tab/arrow/Enter flows: `test_tui_keyboard_responsiveness.py`,
  `test_tui_connect_polish.py` (Enter belongs to the focused button; Space
  activates), OptionList arrows native.
- Focused widgets scroll into view inside every `VerticalScroll` dialog.

## Copy actions
- URL vs approval password vs Authorization credential are distinct actions
  with distinct labels (`test_tui_copy_auth_action.py`,
  `test_tui_connection_copy_e2e.py`); secrets never rendered, only references
  (`test_tui_bridge_secret_output.py`).

## Known limitations (non-blocking, honest)
- Interactive screenshot/pilot SVG artifacts and a human visual pass need the
  desktop; the machine-side gate asserts geometry (fits / scrolls / closes),
  not aesthetics.
- First-run wizard live keyboard walk on a real terminal remains a desktop
  smoke item.
- `scratch/` diagnostics are machine-local and gitignored.
