#!/usr/bin/env python3
"""Validate KaroX access-profile policy and its public documentation.

The stable UI labels are friendly names over durable policy identifiers:
Observe/read_only, Build/workspace_write, and Advanced/elevated. This checker
loads the real policy table and prevents documentation from granting more
authority than the runtime does.

Standard library only; KaroX's models and policy modules have no third-party
runtime dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from karox import policy  # noqa: E402
from karox.models import AccessProfile, Capability  # noqa: E402

EXPECTED_IDENTIFIERS = {
    "Observe": AccessProfile.READ_ONLY,
    "Build": AccessProfile.WORKSPACE_WRITE,
    "Advanced": AccessProfile.ELEVATED,
}

REQUIRED_DOCS = (
    "README.md",
    "README_RU.md",
    "QUICKSTART.md",
    "SECURITY.md",
    "docs/V5_RELEASE_SCOPE.md",
)

# Documents that describe the capability model itself, rather than summarising it
# for a new user. Only these have to spell the browser tier out: SECURITY.md said
# for several releases that browser capability was Advanced-only while the runtime
# granted it under Build, and nothing here could tell. Requiring the identifiers
# in QUICKSTART.md as well would buy noise rather than accuracy.
CAPABILITY_MODEL_DOCS = (
    "SECURITY.md",
    "docs/V5_RELEASE_SCOPE.md",
)


def _profile_capabilities() -> dict[AccessProfile, frozenset[Capability]]:
    """Read the internal policy table without importing its private name."""
    table = getattr(policy, "_PROFILE_CAPABILITIES", None)
    if not isinstance(table, dict):
        raise TypeError("karox.policy._PROFILE_CAPABILITIES is not a dictionary")
    return table


def collect_problems(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    profiles = _profile_capabilities()

    read_only = profiles[AccessProfile.READ_ONLY]
    build = profiles[AccessProfile.WORKSPACE_WRITE]
    advanced = profiles[AccessProfile.ELEVATED]

    # Reviewed 5.0 baselines. ``browser.read`` and ``browser.input`` were added
    # to the tiers below deliberately, to close a desync in which the TUI's own
    # checkbox put browser tools on the allowlist and passed ``--write``, while
    # the policy granted BROWSER_INPUT only from ELEVATED -- so the bridge exited
    # with code 2 ("session profile does not allow browser.input") on a selection
    # the product itself offered.
    #
    # The threat-model review this gate asks for, recorded rather than repeated:
    # ``browser_session`` refuses any non-localhost URL, ``browser.read`` is
    # snapshot/get_text/screenshot/console/network_failures with no navigation,
    # so it observes without mutating and belongs with repo.read in Observe.
    # ``browser.input`` (open/click/fill/select/press/close) drives that local UI
    # and is therefore a workspace-scoped mutation that belongs behind --write.
    # ``desktop.input`` and ``network`` are a different tier entirely -- they
    # leave the workspace -- and stay Advanced-only, which the assertions below
    # now pin rather than assume.
    if read_only != frozenset(
        {
            Capability.REPO_READ,
            Capability.GIT_READ,
            Capability.DIAGNOSTICS_READ,
            Capability.BROWSER_READ,
        }
    ):
        problems.append(
            "read_only policy changed; review the Observe product promise "
            "and threat model"
        )

    required_build = {
        Capability.REPO_READ,
        Capability.REPO_WRITE,
        Capability.DISK_READ,
        Capability.PROCESS_RUN,
        Capability.CHECKS_RUN,
        Capability.GIT_READ,
        Capability.DIAGNOSTICS_READ,
        Capability.MCP_CALL,
        Capability.BROWSER_READ,
        Capability.BROWSER_INPUT,
    }
    if set(build) != required_build:
        problems.append(
            "workspace_write policy changed; review the Build product promise "
            "and release scope"
        )
    if Capability.GIT_COMMIT in build:
        problems.append("workspace_write/Build must not grant git.commit")

    # Observing a local UI is Observe-tier; driving it is not. Pinning the
    # absence matters as much as pinning the presence: widening the read tier by
    # one entry is how the input tier would quietly follow.
    if Capability.BROWSER_INPUT in read_only:
        problems.append(
            "read_only/Observe must not grant browser.input; observing a local "
            "UI is non-mutating, driving it is a workspace mutation"
        )

    if Capability.GIT_COMMIT not in advanced:
        problems.append("elevated/Advanced must contain guarded local git.commit")

    # Capabilities that reach beyond the workspace stay in Advanced only.
    for elevated_only in (Capability.DESKTOP_INPUT, Capability.NETWORK):
        for profile, capabilities in (
            (AccessProfile.READ_ONLY, read_only),
            (AccessProfile.WORKSPACE_WRITE, build),
        ):
            if elevated_only in capabilities:
                problems.append(
                    f"{profile.value} unexpectedly grants {elevated_only.value}; "
                    "it reaches outside the workspace and belongs to Advanced only"
                )
    for forbidden in (
        Capability.GIT_PUSH,
        Capability.PACKAGE_PUBLISH,
        Capability.AUTH_COMMAND,
    ):
        if any(forbidden in capabilities for capabilities in profiles.values()):
            problems.append(
                f"stable access profile unexpectedly grants {forbidden.value}; "
                "the 5.0 scope requires it to remain outside every profile"
            )

    for friendly, profile in EXPECTED_IDENTIFIERS.items():
        if not profile.value:
            problems.append(
                f"{friendly} access profile has an empty durable identifier"
            )

    for relative in REQUIRED_DOCS:
        path = root / relative
        if not path.is_file():
            problems.append(f"access-profile document is missing: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for friendly, profile in EXPECTED_IDENTIFIERS.items():
            if friendly not in text:
                problems.append(f"{relative} does not name {friendly}")
            if profile.value not in text:
                problems.append(
                    f"{relative} does not map {friendly} to {profile.value!r}"
                )

        lowered = text.lower()
        if "build" in lowered and "git.commit" not in lowered:
            problems.append(
                f"{relative} does not explicitly state the Build/git.commit boundary"
            )
        has_local_commit = (
            "local commit" in lowered or "локальный commit" in lowered
        )
        if "advanced" in lowered and not has_local_commit:
            problems.append(
                f"{relative} does not state that local commit belongs to Advanced"
            )
        if "git push" not in lowered or "package publishing" not in lowered:
            problems.append(
                f"{relative} does not state the stable push/publish boundary"
            )

    for relative in CAPABILITY_MODEL_DOCS:
        path = root / relative
        if not path.is_file():
            continue
        lowered = path.read_text(encoding="utf-8").lower()
        for identifier in (
            Capability.BROWSER_READ.value,
            Capability.BROWSER_INPUT.value,
        ):
            if identifier not in lowered:
                problems.append(
                    f"{relative} does not state where {identifier} sits in the "
                    "profile tiers, which is how the runtime and this document "
                    "drifted apart before"
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    profiles = _profile_capabilities()
    problems = collect_problems(ROOT)
    payload = {
        "ok": not problems,
        "profiles": {
            friendly: {
                "identifier": profile.value,
                "capabilities": sorted(item.value for item in profiles[profile]),
            }
            for friendly, profile in EXPECTED_IDENTIFIERS.items()
        },
        "problems": problems,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif problems:
        print("access-profile contract failed:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print(
            "access profiles agree: Observe=read_only, Build=workspace_write, "
            "Advanced=elevated; push/publish remain outside stable profiles"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
