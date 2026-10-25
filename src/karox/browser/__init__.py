"""Full browser research and interaction as typed karo-core-callable tools.

Public surface:

* :class:`~karox.browser.netpolicy.BrowserAccessPolicy` -- the explicit,
  secret-free permission object for one session (loopback vs external HTTPS,
  domain allowlists, headed mode, takeover, downloads, plain test credentials,
  detached instances).
* :class:`~karox.browser.session.BrowserSession` -- the Playwright engine:
  navigation, rendered extraction, interaction, tabs, scrolling, dialogs,
  iframes, uploads/downloads, screenshots, cookie persistence, ownership-proven
  detach/reconnect, user takeover and stale-element recovery.
* :class:`~karox.browser.tools.BrowserToolAdapter` /
  :class:`~karox.browser.tools.BrowserToolRuntime` /
  :func:`~karox.browser.tools.browser_tool_definition` -- the engine-neutral
  adapter an integrator registers with karo-core as the stable
  ``browser.command`` tool, with structured results and cancellation.
* :func:`~karox.browser.tools.execute_browser_command` -- drop-in replacement
  for the ``workspace_worker`` refusal with the identical call contract.
"""

from __future__ import annotations

from .credentials import (
    BrowserCredentialError,
    BrowserCredentialInjectionError,
    BrowserCredentialInjectionResult,
    BrowserCredentialReference,
    BrowserCredentialStore,
    inject_browser_credential,
)
from .errors import (
    BROWSER_ERROR_KINDS,
    BrowserCancelled,
    BrowserError,
    BrowserSecurityError,
    BrowserSessionError,
    classify_browser_error,
    prefix_browser_error,
)
from .netpolicy import (
    BrowserAccessPolicy,
    SessionPinnedProxy,
    validate_browser_url,
)
from .session import (
    ACTION_CAPABILITIES,
    INPUT_ACTIONS,
    READ_ACTIONS,
    BrowserSession,
    CancelToken,
)
from .tools import (
    BROWSER_ACTIONS,
    BrowserToolAdapter,
    BrowserToolRuntime,
    browser_tool_definition,
    execute_browser_command,
)

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
