"""Repository-confined request-only image attachments."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Iterable

from .providers import ImageAttachment

_MAX_IMAGES = 8
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_BYTES = 24 * 1024 * 1024


class ImageAttachmentError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class LoadedImageAttachment:
    path: str
    mime: str
    size: int
    image: ImageAttachment

    def public_dict(self) -> dict[str, Any]:
        return {"path": self.path, "mime": self.mime, "size": self.size}


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageAttachmentError("image must be a real PNG, JPEG, or WebP file")


def _path(repository: Path, value: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ImageAttachmentError("image path must be non-empty text")
    raw = Path(value.strip().strip('"').strip("'"))
    if raw.is_absolute():
        candidate = raw.expanduser().resolve(strict=True)
    else:
        if raw.drive or ".." in raw.parts:
            raise ImageAttachmentError("image path must stay inside the repository")
        candidate = (repository / raw).resolve(strict=True)
    try:
        relative = candidate.relative_to(repository).as_posix()
    except ValueError as exc:
        raise ImageAttachmentError("image path must stay inside the repository") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ImageAttachmentError("image attachment must be a regular non-link file")
    return candidate, relative


def load_image_attachments(
    repository: Path,
    values: Iterable[str],
) -> tuple[LoadedImageAttachment, ...]:
    root = repository.expanduser().resolve(strict=True)
    paths = list(values)
    if len(paths) > _MAX_IMAGES:
        raise ImageAttachmentError(f"at most {_MAX_IMAGES} images may be attached to one turn")
    loaded: list[LoadedImageAttachment] = []
    seen: set[str] = set()
    total = 0
    for value in paths:
        candidate, relative = _path(root, value)
        if relative in seen:
            continue
        seen.add(relative)
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise ImageAttachmentError(f"cannot read image metadata: {relative}") from exc
        if size <= 0 or size > _MAX_IMAGE_BYTES:
            raise ImageAttachmentError(
                f"image {relative} must be between 1 byte and {_MAX_IMAGE_BYTES} bytes"
            )
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise ImageAttachmentError(
                f"image attachments exceed the {_MAX_TOTAL_BYTES}-byte total limit"
            )
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise ImageAttachmentError(f"cannot read image: {relative}") from exc
        mime = _mime(data)
        loaded.append(
            LoadedImageAttachment(
                path=relative,
                mime=mime,
                size=len(data),
                image=ImageAttachment(mime=mime, data=data),
            )
        )
    return tuple(loaded)


__all__ = [
    "ImageAttachmentError",
    "LoadedImageAttachment",
    "load_image_attachments",
]
