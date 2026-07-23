# KaroX Pack SDK

Status: design contract. The SDK is not complete until the sample Pack passes
installation, doctor, tool execution, permission, and removal tests.

## Purpose

A Pack is a versioned extension for a development domain. It can contribute MCP
servers, Skills, Core-registered tools, project detectors, commands, templates,
tests, validators, health checks, and documentation. It cannot bypass Core or
session policy.

## Manifest

`karox-pack.toml` contains:

- stable name, version, description, authors, license;
- required KaroX version range and supported platforms;
- entry points and contributed asset paths;
- tools and exact required capabilities;
- MCP processes/transports and their requested grants;
- Skills and referenced files;
- detectors, commands, templates, tests, and health checks;
- install-time and runtime permissions;
- content hashes and optional publisher signature.

Unknown fields are rejected for the current manifest major version. Paths must
remain inside the Pack. A manifest cannot request unrestricted environment or a
provider credential.

## Lifecycle

1. `pack create` produces a minimal, testable template.
2. `pack install` validates schema, compatibility, paths, hashes, and requested
   permissions in staging before atomic activation.
3. `pack doctor` checks dependencies without mutating the machine.
4. `pack enable` grants only the user-approved subset for a session/profile.
5. `pack disable` stops supervised resources and removes registrations.
6. `pack remove` deletes the installed immutable copy after confirming it is not
   active; user project files and evidence are retained.

Setup steps are declarative. Arbitrary setup scripts are not run implicitly.
External package installation or authentication requires a visible approval.

## Tool integration

Pack tools implement versioned schemas and receive a restricted Core context.
Every call is attributed to `pack:<name>@<version>` and re-authorized. MCP tools
are namespaced and proxied through the same policy. Namespace collisions fail at
installation rather than selecting an arbitrary winner.

## Compatibility and upgrades

Installed Pack versions are immutable. Upgrade stages a new version, runs its
test harness, compares requested permissions, and activates atomically. An
increase in permission requires approval. Session state records the exact Pack
version; resume fails visibly if a compatible version is unavailable.

## Required sample and harness

The first SDK ships a small `project-inspector` Pack that detects a project,
registers one read-only metadata tool and a Skill, emits evidence, and needs no
network or external key. The harness verifies create/install/inspect/doctor/
enable/call/deny/disable/remove, path traversal rejection, compatibility, and
permission intersection on Windows, Linux, and macOS.
