"""Workspace snapshots for ``/undo``: a private git object store beside the sessions.

Before a chat turn the workspace is recorded as a git tree (``git add -A`` + ``git
write-tree``) in a repository of KaroX's own under ``<home>/snapshots/<id>``. The user's
repository, index, branches and stash are never touched: every command runs with
``GIT_DIR`` and ``GIT_INDEX_FILE`` pointing at the private store and the workspace only as
the work tree. Undo makes the work tree match a recorded tree again: files changed or
deleted since then are written back, files created since then are removed.

Snapshots are byte-exact (line-ending conversion and filters are switched off for the
private store, whatever the project's ``.gitattributes`` or the user's git config say) and
respect the project's ``.gitignore``; dependency and cache directories are left out even
when the project does not ignore them. Not covered: ignored files, files outside the
workspace, the contents of nested git repositories.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from pathlib import Path

FIRST_SNAPSHOT_TIMEOUT_S = 60.0  # the first one reads every file
SNAPSHOT_TIMEOUT_S = 20.0
_EXCLUDE = """\
# KaroX snapshots leave these out (dependencies and caches are not the agent's work).
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.tox/
.gradle/
.next/
.cache/
.karox6/
"""
# Byte-exact blobs: no line-ending conversion, no clean/smudge filters, no keyword
# expansion. $GIT_DIR/info/attributes outranks every .gitattributes file.
_ATTRIBUTES = "* -text -eol -filter -ident -working-tree-encoding\n"
_CONFIG = (
    ("core.autocrlf", "false"),
    ("core.safecrlf", "false"),
    ("core.quotepath", "false"),
    ("core.longpaths", "true"),
    ("core.fsmonitor", "false"),
    ("gc.auto", "0"),
    ("advice.addEmbeddedRepo", "false"),
)


class CheckpointError(Exception):
    pass


class Checkpoints:
    def __init__(self, home: Path, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        key = os.path.normcase(str(self.workspace)).encode("utf-8", "surrogateescape")
        self.git_dir = home / "snapshots" / hashlib.sha256(key).hexdigest()[:16]
        self.disabled: str | None = None  # why snapshots are off for this run

    # -- public -------------------------------------------------------------------------

    def snapshot(self) -> str | None:
        """The workspace as it is now, as a tree id; None when snapshots are unavailable
        (no git, or the workspace is too large to record in time)."""
        if self.disabled:
            return None
        try:
            fresh = self._prepare()
            limit = FIRST_SNAPSHOT_TIMEOUT_S if fresh else SNAPSHOT_TIMEOUT_S
            self._git("add", "-A", "--", ".", timeout=limit)
            return self._git("write-tree", timeout=limit).strip()
        except subprocess.TimeoutExpired:
            self.disabled = "recording the project took too long"
        except (OSError, CheckpointError) as exc:
            self.disabled = str(exc) or type(exc).__name__
        return None

    def changes(self, before: str, after: str) -> list[tuple[str, str]]:
        """(status, path) for every file that differs between two snapshots: ``A`` exists
        only in ``after``, ``D`` only in ``before``, ``M`` in both with other content."""
        out = self._git(
            "diff-tree", "-r", "-z", "--no-renames", "--name-status", before, after, timeout=60
        )
        parts = out.split("\0")
        return [(parts[i][:1], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]

    def restore(self, target: str, current: str) -> list[str]:
        """Make the workspace match snapshot ``target``. ``current`` is a snapshot of the
        workspace as it is now (take it right before). Returns the paths that changed."""
        changed = self.changes(target, current)
        rewrite = [path for status, path in changed if status != "A"]
        remove = [path for status, path in changed if status == "A"]
        if rewrite:
            listing = "\0".join(rewrite).encode("utf-8", "surrogateescape")
            self._git(
                "checkout",
                target,
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                timeout=120,
                stdin=listing,
            )
        for rel in remove:
            path = self.workspace / rel
            with contextlib.suppress(OSError):
                path.unlink()
            self._drop_empty_parents(path.parent)
        # The private index now describes ``target``: the next snapshot starts from it.
        self._git("read-tree", target, timeout=60)
        return [path for _, path in changed]

    # -- git ----------------------------------------------------------------------------

    def _prepare(self) -> bool:
        """Create the private store on first use; True when it was just created."""
        if (self.git_dir / "HEAD").is_file():
            return False
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "--quiet", timeout=30)
        for name, value in _CONFIG:
            self._git("config", name, value, timeout=30)
        info = self.git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text(_EXCLUDE, encoding="utf-8", newline="\n")
        (info / "attributes").write_text(_ATTRIBUTES, encoding="utf-8", newline="\n")
        (self.git_dir / "workspace").write_text(str(self.workspace), encoding="utf-8")
        return True

    def _git(self, *args: str, timeout: float, stdin: bytes | None = None) -> str:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") or key in ("GIT_EXEC_PATH", "GIT_SSL_NO_VERIFY")
        }
        env.update(
            GIT_DIR=str(self.git_dir),
            GIT_WORK_TREE=str(self.workspace),
            GIT_INDEX_FILE=str(self.git_dir / "index"),
            # The user's and the system's git config do not apply to the private store
            # (autocrlf, hooks, global ignore files, fsmonitor daemons...).
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
        )
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.workspace,
                env=env,
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise CheckpointError("git is not installed") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise CheckpointError(f"git {args[0]} failed: {detail[-1] if detail else 'no output'}")
        return proc.stdout.decode("utf-8", "surrogateescape")

    def _drop_empty_parents(self, folder: Path) -> None:
        """Remove directories a deleted file left empty (never the workspace itself)."""
        while folder != self.workspace and self.workspace in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                return
            folder = folder.parent
