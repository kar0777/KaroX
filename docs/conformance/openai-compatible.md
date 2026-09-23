# Generic OpenAI-compatible live conformance

integration: openai-compatible
status: pending
release_blocker: true
verified_at_utc: pending
karox_version: 5.0.0rc4
karox_commit: pending
platform: pending
external_version: pending
account_tier: pending
evidence: pending

## Required scenario

Choose one named third-party or local OpenAI-compatible endpoint with an official
API contract. Make a minimal streaming request, receive a tool call, execute it
through Core, return the result, and receive a final response. Record endpoint
and model IDs, protocol differences, usage availability, and limitations. Do not
represent this single run as compatibility with every vendor.

## Result

Partial user-authorized StepFun run on 2026-09-23: `step-5-preview` on
`https://api.stepfun.ai/step_plan/v1` returned HTTP 200 and a complete minimal
SSE response. Standard `/v1` returned 402 because its allowance is separate.
The complete Core tool-loop runner was blocked by automatic execution review;
the required scenario remains pending. See
`../evidence/release-hardening-2026-09-23.md` for exact scope and limitations.

## Limitations

No claim for other vendors, provider tools, writable orchestration or billing
savings follows from the minimal streaming request. The StepFun presets remain
experimental until the full scenario is executed and recorded.
