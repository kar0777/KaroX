"""Which KaroX code is actually running.

A user must immediately be able to tell "this is old installed code" from
"this is the current dogfood build". The owner-reported stale slash menu
after a restart is exactly this failure mode: the launcher imported an
installed copy while the source checkout moved on. Everything here is read
from the running process and the filesystem; a fact that cannot be
established is reported as ``"unknown"``, never guessed.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BuildIdentity:
    """Low-noise diagnostics for the code that answered the prompt."""

    #: Installed distribution version, or "unknown" outside a wheel install.
    version: str
    #: Directory the running ``karox`` package was imported from.
    package_path: str
    #: "source-checkout", "installed-package", or "unknown".
    layout: str
    #: Short git commit of the checkout; "unknown" for installed copies.
    commit: str
    #: Newest module mtime in the running package (UTC ISO), or "unknown".
    source_mtime: str

    def summary(self) -> str:
        return f"{self.version} \u00b7 {self.layout} \u00b7 {self.commit}"


def _detect_layout(package_dir: Path) -> str:
    """Classify where the running package lives, without guessing.

    An installed copy sits under site-packages/dist-packages; a source
    checkout is ``src/karox`` with ``pyproject.toml`` two levels up. Anything
    else is honestly unknown rather than forced into a bucket.
    """

    parts = {part.lower() for part in package_dir.parts}
    if "site-packages" in parts or "dist-packages" in parts:
        return "installed-package"
    if (
        package_dir.name == "karox"
        and package_dir.parent.name == "src"
        and (package_dir.parent.parent / "pyproject.toml").exists()
    ):
        return "source-checkout"
    return "unknown"


def _detect_commit(package_dir: Path) -> str:
    """Short HEAD commit read from ``.git`` files directly, no subprocess."""

    git_dir = package_dir.parent.parent / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if head.startswith("ref: "):
        ref = head[5:].strip()
        try:
            value = (git_dir / ref).read_text(encoding="utf-8").strip()
        except OSError:
            try:
                packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
            except OSError:
                return "unknown"
            for line in packed.splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:12]
            return "unknown"
        return value[:12] if value else "unknown"
    return head[:12] if head else "unknown"


def _newest_module_mtime(package_dir: Path) -> str:
    newest: Optional[float] = None
    try:
        for item in package_dir.glob("*.py"):
            mtime = item.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    except OSError:
        return "unknown"
    if newest is None:
        return "unknown"
    return datetime.fromtimestamp(newest, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def build_identity() -> BuildIdentity:
    """Identity of the running package. Never raises."""

    try:
        import karox

        package_dir = Path(karox.__file__).resolve().parent
    except Exception:
        return BuildIdentity("unknown", "unknown", "unknown", "unknown", "unknown")
    try:
        version = importlib.metadata.version("karox")
    except Exception:
        version = "unknown"
    layout = _detect_layout(package_dir)
    commit = _detect_commit(package_dir) if layout == "source-checkout" else "unknown"
    return BuildIdentity(
        version=version,
        package_path=str(package_dir),
        layout=layout,
        commit=commit,
        source_mtime=_newest_module_mtime(package_dir),
    )


__all__ = ["BuildIdentity", "build_identity"]
