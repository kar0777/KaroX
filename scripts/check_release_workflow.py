#!/usr/bin/env python3
"""Check safety invariants of the stable-release workflow.

GitHub validates YAML syntax when the workflow is loaded. This standard-library
checker validates product invariants that generic YAML parsing would not:
quality and strict gates before publishing, exact-tree validation, safe worktree
creation, artifacts and checksums before tag push, and a smoke-tested wheel for
KaroX 5.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_SNIPPETS = (
    "needs: tests",
    "uses: ./.github/workflows/quality.yml",
    "python scripts/check_v5_release.py --strict",
    "python scripts/check_versions.py",
    "python scripts/check_access_profiles.py",
    "python scripts/check_release_workflow.py",
    "python -m build --wheel",
    'python -m pip install "$WHEEL"',
    'python "$WORKTREE/scripts/portable_uv_pins.py"',
    'python "$WORKTREE/scripts/prepare_portable_uv.py"',
    'gh attestation verify "$UV_ARCHIVE" --repo astral-sh/uv',
    'python "$WORKTREE/scripts/build_portable_bundle.py"',
    "sha256sum --check",
    "Create and push tag only after artifacts pass",
    "gh release create",
    'gh release upload "$TAG" dist/* --clobber',
)

UNIQUE_STEP_NAMES = (
    "- name: Build and smoke-test release artifacts before tagging",
    "- name: Revalidate the exact local release tree",
    "- name: Create and push tag only after artifacts pass",
    "- name: Publish or refresh GitHub Release",
)


def _first_position(text: str, snippet: str, problems: list[str]) -> int:
    count = text.count(snippet)
    if count < 1:
        problems.append(f"release workflow is missing required snippet {snippet!r}")
        return -1
    return text.index(snippet)


def collect_problems(root: Path = ROOT) -> list[str]:
    path = root / ".github" / "workflows" / "release.yml"
    if not path.is_file():
        return [".github/workflows/release.yml is missing"]
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []

    positions = {
        snippet: _first_position(text, snippet, problems)
        for snippet in REQUIRED_SNIPPETS
    }

    for step in UNIQUE_STEP_NAMES:
        count = text.count(step)
        if count != 1:
            problems.append(
                f"release workflow must contain step {step!r} exactly once; found {count}"
            )

    def require_before(first: str, second: str) -> None:
        left = positions.get(first, -1)
        right = positions.get(second, -1)
        if left >= 0 and right >= 0 and left >= right:
            problems.append(f"release workflow must place {first!r} before {second!r}")

    require_before("uses: ./.github/workflows/quality.yml", "gh release create")
    require_before("python scripts/check_v5_release.py --strict", "python -m build --wheel")
    require_before("python scripts/check_versions.py", "python -m build --wheel")
    require_before("python scripts/check_access_profiles.py", "python -m build --wheel")
    require_before("python scripts/check_release_workflow.py", "python -m build --wheel")
    require_before("python -m build --wheel", "Create and push tag only after artifacts pass")
    require_before(
        'python -m pip install "$WHEEL"',
        "Create and push tag only after artifacts pass",
    )
    for portable_marker in (
        'python "$WORKTREE/scripts/portable_uv_pins.py"',
        'python "$WORKTREE/scripts/prepare_portable_uv.py"',
        'gh attestation verify "$UV_ARCHIVE" --repo astral-sh/uv',
        'python "$WORKTREE/scripts/build_portable_bundle.py"',
    ):
        require_before(portable_marker, "Create and push tag only after artifacts pass")
    require_before("sha256sum --check", "Create and push tag only after artifacts pass")
    require_before("Create and push tag only after artifacts pass", "gh release create")
    require_before("gh release create", 'gh release upload "$TAG" dist/* --clobber')

    revalidate_step = text.find("- name: Revalidate the exact local release tree")
    build_step = text.find("- name: Build and smoke-test release artifacts before tagging")
    tag_step = text.find("- name: Create and push tag only after artifacts pass")
    publish_step = text.find("- name: Publish or refresh GitHub Release")
    if min(revalidate_step, build_step, tag_step, publish_step) >= 0:
        if not revalidate_step < build_step < tag_step < publish_step:
            problems.append(
                "release steps must be ordered revalidate → build/smoke → tag → publish"
            )

    if "REQUIRE_WHEEL=false" not in text or 'if [[ "$VERSION" == 5.* ]]' not in text:
        problems.append(
            "release workflow does not condition the wheel requirement on KaroX 5"
        )
    if 'test "$RUNTIME_VERSION" = "$VERSION"' not in text:
        problems.append(
            "KaroX 5 release does not require packaged and shipping versions to match"
        )
    if 'test "$INSTALLED_VERSION" = "$VERSION"' not in text:
        problems.append(
            "release wheel smoke test does not verify the installed version"
        )

    if 'WORKTREE_ROOT="$(mktemp -d)"' not in text:
        problems.append("release workflow does not create a private worktree root")
    if 'WORKTREE="$WORKTREE_ROOT/tree"' not in text:
        problems.append(
            "release workflow must add Git worktree below, not at, the existing temp root"
        )
    if 'WORKTREE="$(mktemp -d)"' in text:
        problems.append(
            "release workflow passes an already-existing mktemp directory to git worktree add"
        )
    if 'git worktree add --detach "$WORKTREE" "$REF"' not in text:
        problems.append("release workflow does not build the wheel from the selected ref")
    if "cleanup_worktree" not in text or "git worktree remove --force" not in text:
        problems.append("release workflow does not clean up its temporary Git worktree")

    if "git tag -a" not in text:
        problems.append("release workflow does not create an annotated tag")
    expected_push = 'git push origin "${{ steps.meta.outputs.tag }}"'
    if expected_push not in text:
        problems.append("release workflow does not push the resolved tag explicitly")

    build_marker = positions.get("python -m build --wheel", -1)
    for marker in ('git push origin "$TAG"', "git push origin '$TAG'"):
        position = text.find(marker)
        if position >= 0 and build_marker >= 0 and position < build_marker:
            problems.append(f"release workflow pushes the tag before artifacts: {marker}")

    if "cd dist" not in text or "sha256sum --check" not in text:
        problems.append("checksum creation and verification must run in dist")
    if "RELEASE.json" not in text or '"status": "published"' not in text:
        problems.append("release workflow does not record verified published assets")
    if 'gh release create "$TAG" --verify-tag' not in text:
        problems.append("GitHub Release creation does not require an existing tag")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    problems = collect_problems(ROOT)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    elif problems:
        print("release workflow safety contract failed:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("release workflow safety ordering is intact")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
