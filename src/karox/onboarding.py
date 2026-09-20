"""Non-secret first-run progress, independent of UI preferences and credentials.

Completion requires successful configuration; dismissing an optional guide is separate.
Only finite choices are persisted; keys, endpoints and diagnostic output never are.
An absent record is intentionally NOT migrated from an existing language setting.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import config_dir
from .security import redact

STEPS = ("provider", "bridge", "tunnel", "review")
SERVICES = ("chatgpt-web", "claude-web")
TUNNELS = ("cloudflare", "tailscale")


@dataclass(frozen=True)
class OnboardingProgress:
    step: str = "provider"
    completed: bool = False
    service: str = "chatgpt-web"
    tunnel: str = "cloudflare"
    provider_outcome: str = "pending"
    bridge_outcome: str = "pending"
    guide_dismissed: bool = False

    def __post_init__(self) -> None:
        if (
            self.step not in STEPS
            or type(self.completed) is not bool
            or self.service not in SERVICES
            or self.tunnel not in TUNNELS
            or self.provider_outcome not in {"pending", "skipped", "configured"}
            or self.bridge_outcome not in {"pending", "requested", "skipped", "configured"}
            or type(self.guide_dismissed) is not bool
            or (self.completed and (self.step != "review"
                or self.bridge_outcome not in {"skipped", "configured"}
                or "configured" not in {self.provider_outcome, self.bridge_outcome}))
        ):
            raise ValueError("invalid onboarding progress")


def progress_path() -> Path:
    return config_dir() / "vnext" / "onboarding.json"


def load_progress() -> OnboardingProgress | None:
    try:
        value = json.loads(progress_path().read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        dismissed = value.get("guide_dismissed", value.get("completed"))
        if type(dismissed) is not bool:
            return None
        return OnboardingProgress(
            step=value["step"],
            # Older records meant guide dismissal, not verified configuration.
            completed=value["completed"] if "bridge_outcome" in value else False,
            service=value["service"], tunnel=value["tunnel"],
            provider_outcome=value.get("provider_outcome", "pending"),
            bridge_outcome=value.get("bridge_outcome", "pending"),
            guide_dismissed=dismissed,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_progress(progress: OnboardingProgress) -> None:
    """Atomic, private file; no catch-all mapping that could accidentally store a key."""
    # Validate even callers constructing objects outside the normal constructor.
    progress.__post_init__()
    path = progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".onboarding-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, **asdict(progress)}, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_setup_error(error: object) -> str:
    """A Tailscale login URL is a one-time credential, not a diagnostic link."""
    text = re.sub(r"https://login\.tailscale\.com/[^\s<>\"']+", "[Tailscale sign-in link hidden]", str(error))
    return str(redact(text))[:200]
