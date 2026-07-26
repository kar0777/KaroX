# KaroX vNext

KaroX is a local terminal agent and a secure bridge between AI services and a
Git repository. It supports native API providers, PromptQL/OpenAPI, Notion,
generic Streamable HTTP MCP clients, and OAuth remote-MCP connections from
ChatGPT Web or Claude Web through one entry point:

```powershell
karox
```

## Install

From a local checkout on Windows:

```powershell
.\install.karox.ps1
```

On macOS or Linux:

```bash
./install.karox.sh
```

Open a new terminal in the repository you want to work with and run `karox`.
The application binds every session to that repository.

## Terminal client

`karox` opens the full-screen client. Normal text is an agent task; it is not
interpreted as raw command-line syntax.

- On the first launch, choose Russian or English with `1`/`2`, arrows, or Tab.
  KaroX then opens the chat immediately; connection setup is not forced.
- Type `/` to open the command menu. Continue typing to filter it, use arrows
  to select, Tab to complete, and Enter to run a command.
- `/connect` opens a keyboard-first wizard: connect an API, a hosted site, or
  both. For APIs, `F5` discovers available models and advertised token limits;
  `Alt+↑/↓` switches the result, and every value remains editable before
  confirmation. `F10` performs a real minimal request and activates the model
  only after that test succeeds. Provider secrets go to the OS keyring.
- `Ctrl+B` starts ChatGPT Web, Claude Web, PromptQL/OpenAPI, Notion/MCP, or a
  generic MCP bridge. ChatGPT and Claude profiles delegate the complete
  session/OAuth/bridge/Cloudflare lifecycle to the CLI and display the public
  connector URL plus approval password.
- `Tab`/`Shift+Tab`, arrows, Space/Enter, `1`/`2`/`3`, `F5`, and `F10` cover
  the complete setup without a mouse. `/language` changes the UI language.
- `/help` shows human-facing commands; `/quit` exits.

The header always shows the current repository, API model, session, and bridge
state. Agent changes still pass through KaroX Core policy, leases, audit, and
verification; the terminal UI does not bypass those boundaries.

## Automation CLI

Scripts and CI use explicit subcommands:

```text
karox paths | session | credential | provider | model | skill | mcp | bridge
      | pack | tui | agent | migrate | doctor
```

Examples:

```powershell
karox doctor
karox provider list
karox model list
karox session list
karox bridge connect chatgpt-web --repository . --write
karox bridge connect claude-web --repository . --write
```

`karox-vnext` remains a compatibility alias and resolves to the same runtime.
Each `bridge connect` command creates its session and temporary OAuth approval
credential, starts and supervises the local MCP server plus Cloudflare HTTPS
tunnel, and prints the connector URL. `Ctrl+C` stops the managed processes.
Omit `--write` for the read-only default tool set.

## Connectivity

- Native agent → official API providers or any OpenAI-compatible endpoint.
- PromptQL → authenticated KaroX OpenAPI connector.
- Notion and other MCP clients → authenticated Streamable HTTP MCP endpoint.
- ChatGPT Web / Claude Web → one-command, CLI-managed OAuth/DCR/PKCE remote
  MCP endpoint and HTTPS tunnel.
- External MCP servers → selected tools exposed through the same Core boundary.

Native provider presets include routing.run, OmniaKey, APIMaster, Chutes,
EmpirioLabs, Puter, Tinfoil, Vivgrid, Merge Gateway, and OpenRouter. Presets
whose official contract is incomplete remain visibly experimental or
documentation-required; KaroX does not invent their endpoints. Model aliases
`sol`, `fable`, and `kimi` are mapped per provider with `karox models map`.

External agent targets, tool providers, and CI/observability integrations use
separate versioned registries and command groups (`target`, `tool`, and
`integration`). W&B is an observability integration rather than a model
provider. Tools and telemetry are disabled by default. The sponsor line in the
interactive header can be toggled with `/sponsors` or `/sponsors off`.

See [connectivity and setup](docs/vNext/CONNECTIVITY.md),
[security](docs/vNext/SECURITY.md), and
[implementation status](docs/vNext/IMPLEMENTATION_STATUS.md). Provider and
extension details are in [providers](docs/providers/README.md),
[targets](docs/targets/README.md), [tools](docs/tools/README.md), and
[integrations](docs/integrations/README.md).

## Verification

The suite is 536 tests. The runner is `unittest`, which is what CI executes:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

536 is the number `python -m pytest --collect-only -q tests` reports and the
number `unittest` reports as `Ran 536 tests`, so it is reproducible from a clean
checkout. A pass tally from `python -m pytest -q` is not: pytest counts subtests
on top of tests, and how many it counts moves between runs, so no such figure is
published here. Note also that a bare `python -m pytest -q` at the repository
root collects 541, because it picks up the five legacy checks in
`scripts/test_karox4_units.py` alongside the vNext suite.

Two tests skip for platform reasons on Windows. All ten KB-HYBRID release gates
pass. Live paid-provider, PromptQL, ChatGPT Web, and Claude Web product runs
still require the user's own accounts and credentials.
