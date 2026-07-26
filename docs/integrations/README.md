# CI and observability integrations

Sentry, SonarQube Cloud, Buildkite, CircleCI, BrowserStack, Braintrust,
Weights & Biases (W&B), Langfuse, Honeycomb, Convex and Verda have isolated opt-in configuration
records. Every integration starts disabled. `--telemetry-field` is an explicit
allowlist; an empty list sends no telemetry fields.

Use `karox integration presets|list|add|configure|doctor|enable|disable|status`.
These records do not yet imply a live vendor client; experimental status is
reported honestly.
