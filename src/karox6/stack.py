"""Import list of the headless stack, used by `karox debug rss` for the RSS gate.

Grow this list as modules are added so the idle-memory measurement stays honest.
"""

from __future__ import annotations

import httpx  # noqa: F401

import karox6.sysinfo  # noqa: F401
