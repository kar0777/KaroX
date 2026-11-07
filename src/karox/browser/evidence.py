"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional
from ..security import redact
from .errors import (
    BrowserError,
    BrowserSecurityError,
    classify_browser_error,
)
from .netpolicy import (
    validate_browser_url,
)
from .guards import (
    _STALE_MESSAGE,
)
from .contract import (
    _ARTIFACT_NAME,
    CancelToken,
    _DownloadEntry,
)


def _has_html_body(page: Any) -> bool:
    """Whether the active document has an HTML body. Never raises."""

    try:
        return bool(page.evaluate("() => !!document.body"))
    except Exception:
        return True


class BrowserEvidenceMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

    def _screenshot(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        return self._capture_screenshot(payload, deadline_seconds, region=False)

    def _screenshot_region(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        return self._capture_screenshot(payload, deadline_seconds, region=True)

    def _capture_screenshot(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        *,
        region: bool,
    ) -> dict[str, Any]:
        # Shared, *unwrapped* implementation: both public actions above are
        # wrapped for owner-thread/cancel/recovery handling, and a wrapped
        # method must never call another wrapped method with extra kwargs.
        page = self._active_page()
        full_page = bool(payload.get("full_page", not region))
        if full_page and not _has_html_body(page):
            # Playwright's full-page capture of a document without a body (a
            # standalone SVG or other XML document) never resolves: a 30 s
            # timeout cost a measured drawing run one step. The viewport holds
            # the whole picture.
            full_page = False
        name = payload.get("name") or "screenshot"
        if not isinstance(name, str) or not _ARTIFACT_NAME.fullmatch(name):
            raise BrowserError(
                "name may contain 1-128 letters, digits, dots, hyphens, or underscores"
            )
        try:
            if region:
                locator = self._locator(payload, page)
                png = locator.screenshot(type="png", timeout=self._timeout_ms(deadline_seconds))
            else:
                png = page.screenshot(full_page=full_page, type="png")
        except Exception as exc:
            message = str(exc)
            if _STALE_MESSAGE.search(message):
                raise BrowserError(
                    "stale_element: screenshot target was replaced; re-snapshot and retry"
                ) from None
            raise BrowserError(f"screenshot_capture_failed: {redact(message)[:300]}") from None
        if not isinstance(png, (bytes, bytearray)) or not png:
            raise BrowserError("screenshot_capture_failed: no bytes were produced")
        record = self._artifacts.put(bytes(png), name=name, mime="image/png")
        viewport = page.viewport_size or {}
        return {
            "artifact_id": record.artifact_id,
            "name": record.name,
            "mime": record.mime,
            "size": record.size,
            "sha256": record.sha256,
            "width": viewport.get("width"),
            "height": viewport.get("height"),
            "full_page": full_page if not region else None,
            "region": region,
            "tab_id": self._ensure_open().active_tab_id,
        }

    def _resolve_upload_paths(self, payload: Mapping[str, Any]) -> list[Path]:
        artifact_id = payload.get("artifact_id")
        raw_paths = payload.get("path") or payload.get("paths")
        paths: list[Path] = []
        if artifact_id is not None:
            if not isinstance(artifact_id, str):
                raise BrowserError("artifact_id must be a string")
            _data, record = self._artifacts.read(artifact_id)
            blob = self._artifacts.root / f"{record.artifact_id}.bin"
            if not blob.is_file():
                raise BrowserError("artifact blob is no longer available")
            paths.append(blob)
        if raw_paths is not None:
            candidates = (
                [raw_paths] if isinstance(raw_paths, str) else list(raw_paths or [])
            )
            if not all(isinstance(item, str) for item in candidates):
                raise BrowserError("upload paths must be strings")
            if len(candidates) > 10:
                raise BrowserError("upload accepts at most 10 files")
            roots = self._upload_roots
            if not roots:
                raise BrowserSecurityError(
                    "upload is not available: no upload roots are configured for "
                    "this browser session"
                )
            for item in candidates:
                candidate = Path(item)
                resolved = (
                    candidate.resolve()
                    if candidate.is_absolute()
                    else (roots[0] / candidate).resolve()
                )
                if not any(
                    resolved == root.resolve()
                    or root.resolve() in resolved.parents
                    for root in roots
                ):
                    raise BrowserSecurityError(
                        "upload path escapes the configured upload roots"
                    )
                if not resolved.is_file():
                    raise BrowserError(f"upload path is not a file: {resolved.name}")
                paths.append(resolved)
        if not paths:
            raise BrowserError("upload requires path, paths, or artifact_id")
        return paths

    def _upload(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        paths = self._resolve_upload_paths(payload)
        page = self._active_page()
        timeout = self._timeout_ms(deadline_seconds)
        trigger = payload.get("trigger_selector")
        if trigger is not None:
            trigger_locator = page.locator(self._selector(trigger)).first
            try:
                with page.expect_file_chooser(timeout=timeout) as chooser_info:
                    trigger_locator.click(timeout=timeout)
                chooser = chooser_info.value
                chooser.set_files([str(item) for item in paths])
            except Exception as exc:
                raise BrowserError(
                    f"upload failed: {classify_browser_error(str(exc))}"
                ) from None
        else:
            locator = self._locator(payload, page)
            metadata = self._locator_metadata(locator)
            if metadata.get("type") not in {"file", "input"}:
                raise BrowserError(
                    "upload target must be an input[type=file] element or use "
                    "trigger_selector for a file-chooser button"
                )
            try:
                locator.set_input_files([str(item) for item in paths], timeout=timeout)
            except Exception as exc:
                raise BrowserError(
                    f"upload failed: {classify_browser_error(str(exc))}"
                ) from None
        return {
            "uploaded": True,
            "count": len(paths),
            "file_names": [item.name[:200] for item in paths],
            "target": str(payload.get("selector") or payload.get("ref") or trigger),
        }

    def _download_start(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        if not self.policy.downloads:
            raise BrowserSecurityError(
                "downloads are not enabled for this browser session"
            )
        page = self._active_page()
        timeout = self._timeout_ms(deadline_seconds, default=30.0)
        url = payload.get("url")
        selector = payload.get("selector")
        if url is None and selector is None:
            raise BrowserError("download_start requires url or selector")
        try:
            with page.expect_download(timeout=timeout) as download_info:
                if url is not None:
                    validated = validate_browser_url(url, self.policy)
                    try:
                        self._navigate(page, validated, deadline_seconds)
                    except Exception:
                        # A navigation that turns into a download is aborted by
                        # the browser; expect_download is the authority here.
                        pass
                else:
                    locator = self._locator(payload, page)
                    locator.click(timeout=timeout)
            download = download_info.value
        except BrowserSecurityError:
            raise
        except Exception as exc:
            raise BrowserError(
                f"download_failure: no download started ({classify_browser_error(str(exc))})"
            ) from None
        download_id = f"dl-{uuid.uuid4().hex[:12]}"
        suggested = str(download.suggested_filename or "download")[:200]
        handle = self._ensure_open()
        entry = _DownloadEntry(
            download_id=download_id,
            status="saving",
            suggested_filename=str(redact(suggested)),
        )
        handle.downloads[download_id] = entry
        try:
            tmp_path = self._browser_root / "downloads" / f"{download_id}.part"
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(tmp_path))
            # Refuse an oversized file before reading it into memory. The
            # artifact store enforces the same cap, but only after the bytes
            # are already in RAM; a hostile or accidental multi-gigabyte
            # download must fail on the temp file, not on the process.
            size = tmp_path.stat().st_size
            if size > self._artifact_byte_limit():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                entry.status = "failed"
                entry.error = (
                    f"download is {size} bytes, over the {self._artifact_byte_limit()} "
                    "byte artifact limit"
                )
                raise BrowserError(f"download_failure: {entry.error}")
            data = tmp_path.read_bytes()
            record = self._artifacts.put(
                data,
                name=re.sub(r"[^A-Za-z0-9._-]", "_", suggested)[:128] or "download.bin",
                mime="application/octet-stream",
            )
            try:
                tmp_path.unlink()
            except OSError:
                pass
            entry.status = "completed"
            entry.artifact_id = record.artifact_id
            entry.size = record.size
            entry.sha256 = record.sha256
        except Exception as exc:
            entry.status = "failed"
            entry.error = str(redact(str(exc)))[:300]
            raise BrowserError(f"download_failure: {entry.error}") from None
        if cancel is not None:
            cancel.raise_if_cancelled()
        return {
            "download_id": download_id,
            "status": entry.status,
            "suggested_filename": entry.suggested_filename,
            "artifact_id": entry.artifact_id,
            "size": entry.size,
            "sha256": entry.sha256,
        }

    def _download_status(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        download_id = payload.get("download_id")
        if not isinstance(download_id, str):
            raise BrowserError("download_status requires a download_id string")
        entry = handle.downloads.get(download_id)
        if entry is None:
            raise BrowserError(f"download_failure: unknown download_id {download_id[:40]}")
        return {
            "download_id": entry.download_id,
            "status": entry.status,
            "suggested_filename": entry.suggested_filename,
            "artifact_id": entry.artifact_id,
            "size": entry.size,
            "sha256": entry.sha256,
            "error": entry.error,
        }
