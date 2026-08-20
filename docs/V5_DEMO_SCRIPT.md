# KaroX 5 — Video Demo Script

Target length: 45–90 seconds. Everything shown is real product behavior;
no hardcoded or staged output.

Tagline: **"One local runtime for every AI coding agent."**
Support line: **"Your models can change. KaroX remembers."**

## Prerequisites (before recording)

- KaroX 5 installed locally; saved durable bridge (e.g. `chatgpt-dev`) running
  with a stable Tailscale HTTPS endpoint.
- ChatGPT and Adapt both connected to the same KaroX MCP endpoint.
- Two approved projects registered in Working Folders: `KaroX-v5` and
  `faceboooook`.
- No pre-seeded memory: the memory step is performed live.

## Shot list

1. **Launch KaroX** (0:00–0:05)
   Open the TUI. Root screen shows Connections with one physical bridge:
   `KaroX MCP — Working`, its HTTPS endpoint, and connected clients.
2. **One bridge, many clients** (0:05–0:12)
   Open the bridge detail: Clients list shows ChatGPT and Adapt attached to the
   same endpoint. Say: "One local runtime. Every agent connects here."
3. **Working Folders** (0:12–0:20)
   Press Ctrl+W. Show the project list: `KaroX-v5` and `faceboooook`, each with
   its own workstreams. Select `faceboooook`.
4. **Parallel projects** (0:20–0:32)
   Split screen: ChatGPT working in `faceboooook` while Adapt continues a task
   in `KaroX-v5`. Point out that neither waits for the other: no global lock,
   no shared cwd.
5. **USER memory, live** (0:32–0:42)
   In ChatGPT: "Меня зовут Егор". Switch to a fresh Adapt chat: "Как меня
   зовут?" → "Егор". Say: "Memory belongs to KaroX, not the model."
6. **Switch client/model** (0:42–0:50)
   Ask the same through the native API provider (Luna): the answer persists.
7. **Coding task** (0:50–1:05)
   Give a small real task in `faceboooook` through ChatGPT (e.g. fix a UI
   string). KaroX routes the edit to the right project, runs affected tests.
8. **Adaptive output** (1:05–1:15)
   Show the concise result card: `✓ Fixed X · ✓ Tests passed`. Expand Details
   to reveal files, commands, and evidence. Failures are never hidden.
9. **Close** (1:15–1:20)
   Root screen again. Tagline on screen: "One local runtime for every AI
   coding agent. Your models can change. KaroX remembers."

## Rules

- Real repositories, real tests, real memory writes only.
- If a step fails on camera, keep the take honest: failures render with full
  diagnostics by design.
- Do not show tokens, secrets, or the bridge bearer.
