# KaroX vNext Product Definition

Status: architectural baseline, 2026-07-23. This document distinguishes shipped
legacy behavior from vNext work; it is not a release announcement.

## Product promise

KaroX is one local AI-development runtime that can be driven either by a model
selected by the user or by a compatible hosted client. The runtime, repository
state, permissions, sessions, and evidence remain owned by KaroX rather than by
one provider or website.

The differentiator is interoperability, not a general claim that KaroX is safer
than Claude Code, Codex, OpenCode, or another agent.

## Operating modes

1. **Native Agent** — KaroX calls a configured API or local model and executes
   its tool calls through Core Runtime.
2. **Hosted Bridge** — an authenticated MCP/OpenAPI-compatible hosted client
   calls the same Core Runtime through a restricted bridge profile.
3. **Runtime only** — users and automation operate tools, sessions, and MCP
   endpoints without an embedded model.

Native and hosted modes share tools, policy, audit, session state, and evidence.
Provider adapters and bridge adapters never execute local actions directly.

## Primary workflows

- Configure an OpenAI-compatible endpoint without editing a config file, select
  a model, run an agent task, verify its changes, and receive evidence.
- Start with a hosted client, resume in the native CLI with another model, and
  retain Git/process/test state through a structured handoff.
- Attach explicitly selected external MCP tools to a native or hosted session
  without revealing provider or MCP credentials.
- Discover a Skill lazily or install a Pack whose tools remain subject to Core
  policy and session permissions.

## Compatibility truth table

| Surface | Current evidence | vNext status |
| --- | --- | --- |
| Notion MCP | Provider transformation, profile, transport, doctor, and gateway tests | Tested legacy bridge; must remain green during migration |
| Generic Streamable HTTP MCP | FastMCP endpoint and host-security tests | Tested legacy transport; vNext profile planned |
| PromptQL | Launcher/OpenAPI-oriented files and examples, no dedicated product E2E | Experimental |
| HyperAgent | No verified dedicated implementation or E2E in this repository | Experimental |
| Native model agent | No baseline implementation | In implementation |
| External MCP client/proxy | No baseline implementation | Planned |

“Protocol-compatible” is not equivalent to “tested with a product.” A client is
promoted to **tested** only after a repeatable end-to-end test records its client
version, transport, authentication, capabilities, and limitations.

## Product principles

- One local-action boundary and one session model.
- Single-agent by default; extra roles require measurable value.
- Evidence, not a model’s assertion, determines task success.
- Safe defaults: repository confinement, no push/publish, least capability, and
  explicit permission for desktop, browser, authentication, and proxied MCP.
- Small provider and bridge adapters; no product-specific runtime forks.
- Line mode, JSON automation, and CI mode remain first-class; the TUI is a view.
- No silent fallback that changes provider, cost, privacy boundary, or mutation.
- A feature is not marked complete without end-to-end coverage.

## Non-goals for the first vNext release

- Claiming compatibility with arbitrary websites.
- Shipping every proposed Pack or a marketplace.
- Reimplementing mature browser, accessibility, or build tools.
- Blindly preserving full chat transcripts as task state.
- Treating multi-agent orchestration as an automatic quality improvement.

## Success gates

The first merge-ready vNext must demonstrate the native OpenAI-compatible
vertical slice, durable/resumable sessions, model switching, credential
isolation, Notion regression coverage, one stdio MCP client, selected-tool MCP
proxying, lazy Skills, and a real sample Pack lifecycle. Cross-platform limits
must be stated alongside the test matrix.
