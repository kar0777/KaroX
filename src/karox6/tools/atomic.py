"""Write a file so that a failed write leaves the old content in place.

Opening a file for writing truncates it first; a full disk or a killed process then
leaves it empty or half written while the tool reports an error. Here the bytes go to a
temporary file next to the target, which then replaces it in one step.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

# Read once: os.umask can only be read by setting it, which is not thread-safe later.
_UMASK = os.umask(0)
os.umask(_UMASK)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path``'s content with ``data``; on error the old file is untouched.

    A symlink is written through (its target is replaced, the link stays); the file
    mode of an existing target is kept."""
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        info = target.stat()
        if not os.access(target, os.W_OK):
            raise PermissionError(13, "Permission denied", str(target))
        if info.st_nlink > 1:
            # A hard link shares the inode; replacing it would split the copies.
            target.write_bytes(data)
            return
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".karox-", suffix=".tmp", dir=target.parent)
    except OSError:
        # The directory forbids new files: write in place, as before.
        target.write_bytes(data)
        return
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if target.exists():
            with contextlib.suppress(OSError):
                shutil.copymode(target, tmp)
        else:
            # mkstemp creates 0600; a new file gets the usual umask-based mode.
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o666 & ~_UMASK)
        try:
            os.replace(tmp, target)
        except PermissionError:
            # Windows refuses to replace a file another process holds open; write in
            # place there, as before.
            if os.name != "nt":
                raise
            target.write_bytes(data)
            tmp.unlink(missing_ok=True)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
