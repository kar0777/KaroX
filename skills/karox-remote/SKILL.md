# KaroX Remote Workspace

You are reasoning inside an Ellipsis sandbox that does not contain the user's
project. The selected Git repository exists only on the user's local computer
and is reachable only through the installed `karox-remote` command.

## Mandatory workflow

1. Run `karox-remote preflight` before any other project action.
2. Treat the returned local session, access profile, tool list, and command
   allowlists as authoritative.
3. Use `karox-remote context`, `list`, `read`, and `search` to inspect local
   state. Never inspect the sandbox filesystem for project files.
4. Use `patch` or `write` only after reading the target and use a stable
   idempotency key for every mutation.
5. Run only commands allowed by the local session. Use `check` for verification
   and managed process commands for dev servers.
6. Use browser and screenshot commands only for localhost applications.
7. Inspect `git-status`, `diff`, and `report` before claiming completion.
8. Separate verified tool evidence from assumptions in the final response.

## Prohibited actions

- Do not search for, clone, reconstruct, upload, download, or create a copy of
  the project in the Ellipsis sandbox.
- Do not use GitHub, a Git remote, a pull request, or a cloud repository to move
  changes between Ellipsis and KaroX.
- Do not run the user's project, tests, build, dev server, browser, or Git
  commands in the Ellipsis sandbox.
- Do not bypass a denied KaroX operation or emulate a blocked tool with shell
  commands.
- Never run push, publish, deploy, release, package publication, or
  authentication actions.
- Do not print, inspect, persist, or pass through connection environment
  variables. The client reads them internally.
- Do not ask the user to copy files from the cloud. All accepted changes must
  already exist in the local working tree.

## Useful commands

```text
karox-remote preflight
karox-remote context
karox-remote list
karox-remote read <path>
karox-remote search <query> --pattern "**/*.py"
karox-remote patch <path> --old <text> --new <text> --expected-sha256 <digest>
karox-remote check '["python","-m","pytest","-q"]'
karox-remote process-start '["npm","run","dev"]' --process-id dev-server
karox-remote process-status dev-server
karox-remote browser http://127.0.0.1:3000 --actions <json>
karox-remote screenshot http://127.0.0.1:3000 artifacts/page.png
karox-remote git-status
karox-remote diff
karox-remote report
```

The initial local checkpoint is created by KaroX before Ellipsis starts.
Rollback is intentionally unavailable to the remote agent and requires a
separate explicit command from the user in the local Karo CLI.
