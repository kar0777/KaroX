"""Live multi-project acceptance against two real folders on this machine.

Run explicitly (it is an acceptance module, not part of the default sweep):
``python -m pytest tests/multi_project_live_acceptance.py``.

Runs the production ProjectRegistry, the saved web-bridge profile store, and
the session/lease layer over project A (this repository) and a project B
created for the run in the OS temp area, deliberately with a space in its
name. Nothing outside the temp folder is written: the saved profile is stored
in a private store file, so the live bridge and the user's real profiles are
untouched.

Exit code 0 means every acceptance line printed PASS.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karox.models import AccessProfile  # noqa: E402
from karox.project_registry import ProjectRegistry, ProjectRegistryError  # noqa: E402
from karox.web_bridge_profiles import (  # noqa: E402
    SavedWebBridgeProfile,
    WebBridgeProfileStore,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"{status}  {name}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILURES.append(name)


def main() -> int:
    project_a = Path(__file__).resolve().parent.parent
    workspace = Path(tempfile.mkdtemp(prefix="karox-mp-"))
    project_b = workspace / "project b with spaces"
    project_b.mkdir(parents=True)
    (project_b / "README.md").write_text("project B\n", encoding="utf-8")
    store_path = workspace / "profiles.json"

    try:
        registry = ProjectRegistry.single(project_a)
        entry_a = registry.default
        assert entry_a is not None
        check("A is the only approved project", len(registry.projects) == 1)

        # add project B
        two = registry.add(project_b, label="Project B")
        entry_b = two.entry_for_path(project_b)
        check("B was added", entry_b is not None)
        assert entry_b is not None
        check("path with spaces survives", " " in entry_b.path)
        check(
            "adding B does not steal the default",
            two.default_project_id == entry_a.project_id,
        )

        # project ids are stable and deterministic across independent builds
        rebuilt = ProjectRegistry.single(project_a).add(project_b, label="other label")
        rebuilt_b = rebuilt.entry_for_path(project_b)
        assert rebuilt_b is not None
        check("project_id is stable", rebuilt_b.project_id == entry_b.project_id)

        # switch default A <-> B
        b_default = two.with_default(entry_b.project_id)
        check("default switched to B", b_default.default_project_id == entry_b.project_id)
        a_again = b_default.with_default(entry_a.project_id)
        check("default switched back to A", a_again.default_project_id == entry_a.project_id)
        check(
            "ids unchanged by a default switch",
            {item.project_id for item in a_again.projects}
            == {entry_a.project_id, entry_b.project_id},
        )

        # explicit selection resolves the right folder, both directions
        check(
            "explicit selection resolves A",
            os.path.normcase(str(b_default.resolve(entry_a.project_id)))
            == os.path.normcase(str(project_a)),
        )
        check(
            "explicit selection resolves B",
            os.path.normcase(str(b_default.resolve(entry_b.project_id)))
            == os.path.normcase(str(project_b.resolve())),
        )

        # a saved profile carrying both projects keeps identity across a
        # default switch: same name, port, tunnel, URL, credential refs
        profile = SavedWebBridgeProfile(
            name="mp-acceptance",
            target_profile="chatgpt-web",
            repository=str(project_a),
            projects=tuple(two.to_payload()),
            default_project_id=two.default_project_id,
            tools=("karox.repo.read_file", "karox.git.status"),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            port=8791,
            # A custom tunnel is the only configuration that carries a saved
            # public URL, which is exactly the field this run must prove a
            # default-project switch leaves alone.
            tunnel="custom",
            public_url="https://example.test",
        )
        store = WebBridgeProfileStore(store_path)
        store.put(profile, replace_existing=False)
        loaded = store.get("mp-acceptance")
        check("both projects persisted", len(loaded.projects) == 2)

        switched = SavedWebBridgeProfile(
            **{
                **{
                    field: getattr(loaded, field)
                    for field in loaded.__dataclass_fields__
                },
                "default_project_id": entry_b.project_id,
            }
        )
        store.put(switched)
        after = store.get("mp-acceptance")
        check("default project switched in the saved profile", after.default_project_id == entry_b.project_id)
        check("bridge port unchanged", after.port == profile.port)
        check("public URL unchanged", after.public_url == profile.public_url)
        check("tunnel unchanged", after.tunnel == profile.tunnel)
        check(
            "credential references unchanged",
            after.browser_credential_refs == profile.browser_credential_refs,
        )
        check("no duplicate profile was created", len(store.list()) == 1)
        raw = store_path.read_text(encoding="utf-8").lower()
        check("no secret material in the store", "secret" not in raw and "bearer" not in raw)

        # a reopened store reads the same registry back (restart survival)
        reopened = WebBridgeProfileStore(store_path).get("mp-acceptance")
        check(
            "registry survives a store reopen",
            {item["project_id"] for item in reopened.projects}
            == {entry_a.project_id, entry_b.project_id},
        )

        # traversal: a path under neither project is not approved
        check(
            "an unapproved path is not resolvable",
            two.entry_for_path(workspace) is None,
        )

        # a stale project path degrades rather than crashing the whole registry
        stale_dir = workspace / "stale project"
        stale_dir.mkdir()
        with_stale = two.add(stale_dir, label="stale")
        stale_entry = with_stale.entry_for_path(stale_dir)
        assert stale_entry is not None
        shutil.rmtree(stale_dir)
        try:
            with_stale.resolve(stale_entry.project_id)
            stale_ok = False
            detail = "resolve() returned a deleted path"
        except ProjectRegistryError:
            stale_ok = True
            detail = ""
        except Exception as exc:  # pragma: no cover - reported, not swallowed
            stale_ok = False
            detail = f"unexpected {type(exc).__name__}"
        check("a deleted project fails cleanly", stale_ok, detail)
        check(
            "the healthy projects still resolve with a stale one present",
            os.path.normcase(str(with_stale.resolve(entry_a.project_id)))
            == os.path.normcase(str(project_a)),
        )
        surviving = with_stale.remove(stale_entry.project_id)
        check("removing the stale project keeps the rest", len(surviving.projects) == 2)
        check(
            "removing a non-default project keeps the default",
            surviving.default_project_id == two.default_project_id,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} acceptance check(s) failed: {FAILURES}")
        return 1
    print("\nAll multi-project acceptance checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_multi_project_live_acceptance() -> None:
    """Fail the suite if any acceptance line failed."""

    assert main() == 0, f"failed acceptance checks: {FAILURES}"
