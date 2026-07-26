# Tool providers

Tavily, Cohere, fal and Browser Use are optional tool-provider presets. They are
disabled by default and live in `tools.json`, never in the model selector.
Configuration rejects secret-looking fields: credentials must remain opaque.
Enabling a preset marked `DOCUMENTATION REQUIRED` is blocked.

Use `karox tool presets|list|add|configure|doctor|enable|disable|status`.

