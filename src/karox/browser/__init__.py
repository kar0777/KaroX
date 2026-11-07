"""Full browser research and interaction as typed karo-core-callable tools.

Public names are exported lazily.  Importing ``karox.browser`` must stay a
cheap package import; a caller that wants the Playwright session asks for
``BrowserSession`` explicitly.  This also keeps the package initializer out of
the submodule import cycle (session → tools → package).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ACTION_CAPABILITIES": (".session", "ACTION_CAPABILITIES"),
    "BROWSER_ACTIONS": (".tools", "BROWSER_ACTIONS"),
    "BROWSER_ERROR_KINDS": (".errors", "BROWSER_ERROR_KINDS"),
    "BrowserAccessPolicy": (".netpolicy", "BrowserAccessPolicy"),
    "BrowserCancelled": (".errors", "BrowserCancelled"),
    "BrowserCredentialError": (".credentials", "BrowserCredentialError"),
    "BrowserCredentialInjectionError": (".credentials", "BrowserCredentialInjectionError"),
    "BrowserCredentialInjectionResult": (".credentials", "BrowserCredentialInjectionResult"),
    "BrowserCredentialReference": (".credentials", "BrowserCredentialReference"),
    "BrowserCredentialStore": (".credentials", "BrowserCredentialStore"),
    "BrowserError": (".errors", "BrowserError"),
    "BrowserSecurityError": (".errors", "BrowserSecurityError"),
    "BrowserSession": (".session", "BrowserSession"),
    "BrowserSessionError": (".errors", "BrowserSessionError"),
    "BrowserToolAdapter": (".tools", "BrowserToolAdapter"),
    "BrowserToolRuntime": (".tools", "BrowserToolRuntime"),
    "CancelToken": (".session", "CancelToken"),
    "INPUT_ACTIONS": (".session", "INPUT_ACTIONS"),
    "READ_ACTIONS": (".session", "READ_ACTIONS"),
    "SessionPinnedProxy": (".netpolicy", "SessionPinnedProxy"),
    "browser_tool_definition": (".tools", "browser_tool_definition"),
    "classify_browser_error": (".errors", "classify_browser_error"),
    "execute_browser_command": (".tools", "execute_browser_command"),
    "inject_browser_credential": (".credentials", "inject_browser_credential"),
    "prefix_browser_error": (".errors", "prefix_browser_error"),
    "validate_browser_url": (".netpolicy", "validate_browser_url"),
}

__all__ = [
    "ACTION_CAPABILITIES",
    "BROWSER_ACTIONS",
    "BROWSER_ERROR_KINDS",
    "BrowserAccessPolicy",
    "BrowserCancelled",
    "BrowserCredentialError",
    "BrowserCredentialInjectionError",
    "BrowserCredentialInjectionResult",
    "BrowserCredentialReference",
    "BrowserCredentialStore",
    "BrowserError",
    "BrowserSecurityError",
    "BrowserSession",
    "BrowserSessionError",
    "BrowserToolAdapter",
    "BrowserToolRuntime",
    "CancelToken",
    "INPUT_ACTIONS",
    "READ_ACTIONS",
    "SessionPinnedProxy",
    "browser_tool_definition",
    "classify_browser_error",
    "execute_browser_command",
    "inject_browser_credential",
    "prefix_browser_error",
    "validate_browser_url",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
