# KaroX v3.15.0 — Localized Notion setup wizard

- Adds one guided `karox notion setup` flow for Windows, macOS, and Linux.
- Reads the language selected in KaroX and shows Russian or English instructions automatically.
- Explicitly explains that Tailscale must be running and show `Connected`.
- Tries to start the Tailscale service and desktop application automatically.
- Opens or triggers Tailscale sign-in when the device is not connected.
- Waits for the stable `*.ts.net` hostname before continuing.
- Prints exact Notion Custom MCP fields and the complete connection sequence.
- Warns users to place the Bearer key only in Notion's protected Token field, never in chat.
- Reminds users to keep Tailscale and the `karox notion` session running while Notion works with files.
- Routes `setup`, `connection`, `status`, `rotate-key`, and `reset-connection` through the same localized wizard.
- Requires and validates the new wizard in installation diagnostics.
