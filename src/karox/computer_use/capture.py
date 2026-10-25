"""Background window capture with quality gates and honest freshness.

Capture renders only the pinned HWND (``PrintWindow`` / ``WM_PRINT``); it never
reads screen regions, so an overlapping foreign window cannot leak into a
KaroX session and the target is never activated, moved or focused. When a
frame is blank or repeats the previous digest — Electron apps can hand back a
cached composition surface — the fallback chain tries window-owned paint
surfaces and large child windows before reporting a stale frame honestly.
"""

from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import pngtools
from .deadline import Deadline
from .errors import CaptureError
from .identity import WindowIdentity

_MAX_CHILD_ATTEMPTS = 3


@dataclass
class CaptureResult:
    png: bytes
    width: int
    height: int
    method: str
    sha256: str
    quality: dict[str, Any]
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _encode(bits: bytes, width: int, height: int, max_dim: int) -> tuple[bytes, int, int, str]:
    """Downscale when needed and PNG-encode; Pillow is used when installed."""

    scaler = "none"
    if max(width, height) > max_dim > 0:
        try:
            from PIL import Image  # type: ignore[import-untyped]

            image = Image.frombuffer("RGB", (width, height), bits, "raw", "BGRX", 0, 1)
            image.thumbnail((max_dim, max_dim))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), image.size[0], image.size[1], "pillow"
        except ImportError:
            bits, width, height = pngtools.downscale_bgrx(bits, width, height, max_dim)
            scaler = "stdlib"
    return pngtools.encode_png(width, height, bits), width, height, scaler


def _child_candidates(probe: Any, ident: WindowIdentity) -> list[Any]:
    rows = [
        raw
        for raw in probe.enum_child_windows(ident.hwnd)
        if raw.rect[2] > raw.rect[0] and raw.rect[3] > raw.rect[1]
    ]
    minimum_area = max(1, int(ident.width * ident.height * 0.35))
    rows = [raw for raw in rows if (raw.rect[2] - raw.rect[0]) * (raw.rect[3] - raw.rect[1]) >= minimum_area]
    rows.sort(
        key=lambda raw: (raw.rect[2] - raw.rect[0]) * (raw.rect[3] - raw.rect[1]), reverse=True
    )
    return rows[:_MAX_CHILD_ATTEMPTS]


def capture_window(
    probe: Any,
    ident: WindowIdentity,
    *,
    deadline: Deadline,
    max_dim: int = 1800,
    history: Optional[dict[str, Any]] = None,
    app_id: Optional[str] = None,
) -> CaptureResult:
    """Capture the pinned window off-screen and return PNG bytes + diagnostics."""

    deadline.check("desktop capture")
    width, height = ident.width, ident.height
    if width <= 0 or height <= 0:
        raise CaptureError("attached window has invalid capture bounds")

    fallbacks: list[dict[str, Any]] = []

    def attempt(hwnd: int, w: int, h: int, backend: str) -> Optional[tuple[bytes, dict[str, Any]]]:
        deadline.check("desktop capture")
        try:
            bits = probe.capture_bits(hwnd, w, h, backend)
        except Exception as exc:  # a refused paint must not kill the chain
            fallbacks.append({"backend": f"win32-{backend}", "error": str(exc)[:240]})
            return None
        if bits is None:
            fallbacks.append({"backend": f"win32-{backend}", "error": "paint request refused"})
            return None
        quality = pngtools.frame_quality(bits, w, h)
        return bits, quality

    primary = attempt(ident.hwnd, width, height, "printwindow")
    if primary is None:
        alt = attempt(ident.hwnd, width, height, "wm-print")
        if alt is None:
            raise CaptureError(
                "the application refused every background capture backend; "
                "screen-region capture is intentionally not used"
            )
        bits, quality = alt
        method, capture_w, capture_h = "win32-wm-print", width, height
    else:
        bits, quality = primary
        method, capture_w, capture_h = "win32-printwindow", width, height

    digest = hashlib.sha256(bits).hexdigest()
    previous = (history or {}).get(app_id or "") if app_id else None
    previous_digest = str(previous.get("sha256", "")) if isinstance(previous, dict) else ""
    repeated = bool(previous_digest) and previous_digest == digest

    # A blank or byte-identical frame may be a cached composition surface.
    # Try only window-owned paint surfaces — never the screen.
    if not quality.get("meaningful") or repeated:
        fallbacks.append(
            {
                "backend": f"win32-{method.removeprefix('win32-')}",
                "sha256": digest,
                "quality": quality,
                "reason": "repeated_frame" if repeated else "blank_frame",
            }
        )
        alt = attempt(ident.hwnd, width, height, "wm-print")
        if alt is not None:
            alt_bits, alt_quality = alt
            alt_digest = hashlib.sha256(alt_bits).hexdigest()
            fallbacks.append(
                {
                    "backend": "win32-wm-print",
                    "sha256": alt_digest,
                    "different_from_primary": alt_digest != digest,
                    "quality": alt_quality,
                }
            )
            if alt_quality.get("meaningful") and alt_digest != digest:
                bits, quality, digest = alt_bits, alt_quality, alt_digest
                method = "win32-wm-print"
        if not quality.get("meaningful"):
            for child in _child_candidates(probe, ident):
                child_w = child.rect[2] - child.rect[0]
                child_h = child.rect[3] - child.rect[1]
                got = attempt(child.hwnd, child_w, child_h, "printwindow")
                if got is None:
                    continue
                child_bits, child_quality = got
                child_digest = hashlib.sha256(child_bits).hexdigest()
                fallbacks.append(
                    {
                        "backend": "win32-child-printwindow",
                        "window_id": child.hwnd,
                        "sha256": child_digest,
                        "quality": child_quality,
                    }
                )
                if child_quality.get("meaningful") and child_digest != digest:
                    bits, quality, digest = child_bits, child_quality, child_digest
                    method, capture_w, capture_h = "win32-child-printwindow", child_w, child_h
                    break

    png, out_w, out_h, scaler = _encode(bits, capture_w, capture_h, max_dim)
    captured_at = time.time()
    if repeated:
        repeated_count = int((previous or {}).get("repeated_count", 1)) + 1
        unchanged_since = float((previous or {}).get("unchanged_since", captured_at))
        freshness = "repeated_frame"
    else:
        repeated_count = 1
        unchanged_since = captured_at
        freshness = "changed" if previous else "first_sample"
    diagnostics = {
        "sha256": digest,
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "capture_method": method,
        "scaler": scaler,
        "captured_at": captured_at,
        "freshness": freshness,
        "repeated_count": repeated_count,
        "unchanged_since": unchanged_since,
        "unchanged_for_seconds": round(max(0.0, captured_at - unchanged_since), 3),
        "fallbacks": fallbacks,
        "interpretation": (
            "identical frames do not by themselves prove the app is frozen; "
            "combine capture freshness with window/process liveness signals"
        ),
    }
    if history is not None and app_id:
        history[app_id] = diagnostics
    return CaptureResult(
        png=png,
        width=out_w,
        height=out_h,
        method=method,
        sha256=digest,
        quality=quality,
        fallbacks=fallbacks,
        diagnostics=diagnostics,
    )


__all__ = ["CaptureResult", "capture_window"]
