"""Typed browser and desktop surface tools wired around the engine run.

The b-browser and b-desktop workers shipped engine-neutral adapters with
exact registration contracts (their reports). This module is the integrator's
wiring: it registers ``browser.command`` and the ``desktop.*`` tools on the
Core runtime the default engine run already uses, so every call flows through
``CoreRuntime.execute`` -- capability decision, Smart-Stop risk gate, audit
trail and stream events -- exactly like the built-in tools. Nothing here
bypasses the guarded environment, and both adapters stay lazily initialized:
registering the tools never launches a browser or touches a desktop window.

Browser defaults follow the b-browser report: loopback-only, headless, no
downloads, no detach, no plain credentials, no takeover. External navigation
still needs ``Capability.NETWORK`` on top of ``browser.input`` because the
adapter's capability guard re-checks per action.

Desktop tools require the ``desktop.input`` capability, which only the
elevated profile grants, so ordinary write sessions never see them and a
direct call is refused by the policy gate. Consequential desktop actions
(launch, visible restore, terminal) carry their own one-shot human approval
inside the adapter, redeemed through the ``confirmation_token`` argument the
schemas below expose.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from .browser import BrowserAccessPolicy, BrowserToolRuntime, browser_tool_definition
from .computer_use import TOOL_SPECS, ComputerUseAdapter
from .core import CoreRuntime, ToolDefinition
from .models import Capability, Origin
from .paths import runtime_dir
from .policy import CapabilityPolicy

#: The desktop actions whose payload may carry a one-shot approval token.
#: ``additionalProperties: false`` schemas would otherwise reject the token,
#: and the token must stay out of the digested payload (an approval covers the
#: exact action, not the credential that redeems it).
_APPROVAL_TOKEN_ACTIONS = frozenset(
    {"desktop.launch_app", "desktop.open_terminal", "desktop.show_window"}
)


def register_browser_tool(
    core: CoreRuntime,
    *,
    capability_policy: CapabilityPolicy,
    origin: Origin,
    session_id: str,
) -> BrowserToolRuntime:
    """Register ``browser.command`` for one agent session; return the runtime.

    The returned runtime owns the lazily created Playwright session; the
    caller closes it with ``close_all()`` when the run ends.
    """

    runtime = BrowserToolRuntime(
        BrowserAccessPolicy(session_id="ad-hoc"),
        browser_root=runtime_dir() / "vnext" / "browser",
        capability_guard=lambda capability: capability_policy.require(origin, capability),
    )

    def handler(arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return runtime.execute(session_id, arguments, deadline_seconds)

    core.register_tool(browser_tool_definition(), handler)
    return runtime


def register_desktop_tools(
    core: CoreRuntime, *, session_id: str
) -> ComputerUseAdapter:
    """Register the ``desktop.*`` tools for one agent session.

    Registration is platform-independent: on a non-Windows host every call
    returns a typed ``platform_unsupported`` observation instead of failing
    the run, and elevation is enforced by the ``desktop.input`` capability,
    not by hiding the tools from the registry.
    """

    adapter = ComputerUseAdapter(session_id=session_id)
    for spec in TOOL_SPECS.values():
        name = str(spec["name"])
        schema = _desktop_schema(spec["input_schema"], name)
        definition = _desktop_definition(spec, schema)

        def handler(
            arguments: Dict[str, Any],
            deadline_seconds: float,
            _action: str = name,
            _adapter: ComputerUseAdapter = adapter,
        ) -> Dict[str, Any]:
            token = arguments.get("confirmation_token")
            payload = {
                key: value
                for key, value in arguments.items()
                if key != "confirmation_token"
            }
            result = _adapter.execute(
                _action,
                payload,
                timeout=deadline_seconds,
                confirmation_token=token if isinstance(token, str) else None,
            )
            return result.to_dict()

        core.register_tool(definition, handler)
    return adapter


def _desktop_schema(raw: Mapping[str, Any], name: str) -> Dict[str, Any]:
    schema = dict(raw)
    if name in _APPROVAL_TOKEN_ACTIONS:
        properties = dict(schema.get("properties") or {})
        properties["confirmation_token"] = {
            "type": "string",
            "description": (
                "The one-shot human approval token for this exact action "
                "(obtain it via desktop.request_approval and a human running "
                "`python -m karox.computer_use approve` in a terminal)."
            ),
        }
        schema["properties"] = properties
    return schema


def _desktop_definition(spec: Mapping[str, Any], schema: Dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        str(spec["name"]),
        str(spec["description"]),
        Capability.DESKTOP_INPUT,
        False,
        schema,
        replayable=False,
    )


__all__ = ["register_browser_tool", "register_desktop_tools"]
