# Development providers

These routes are for local development, benchmarks and manual testing only.
They are **not** product defaults, are not shipped in a wheel, and are never
read unless the corresponding environment variables are set.

## DashScope compatible mode (current development route)

The owner-provided key is free for development, so live runs are cheap. Keep it
in a local file, not in the repository.

| field | value |
|---|---|
| Base URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| API format | OpenAI chat completions |
| Model | `deepseek-v4.1-flash-1017` |
| Context window | 1,000,000 tokens |
| Max output | 384,000 tokens |
| Reasoning | `reasoning_effort: "max"` (also accepts `enable_thinking: true`) |

Local file (git-ignored through `.karox/`):

```text
.karox/dev-providers.env
```

```sh
export KAROX_DEV_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export KAROX_DEV_API_KEY="<owner-provided key>"
export KAROX_DEV_MODEL="deepseek-v4.1-flash-1017"
export KAROX_DEV_CONTEXT="1000000"
export KAROX_DEV_MAX_OUTPUT="384000"
```

Load it in a shell with:

```sh
set -a; . .karox/dev-providers.env; set +a
```

Run Karo directly against it:

```sh
karox agent run --repository . --task "..." \
  --model "$KAROX_DEV_MODEL" --base-url "$KAROX_DEV_BASE_URL" \
  --api-key-env KAROX_DEV_API_KEY \
  --access-profile workspace_write --effort-level ultra \
  --context-window 1000000 --max-output-tokens 384000 \
  --verification-command '["python","-m","pytest","-q"]' \
  --stream-json --json
```

The arena at `lab/kernel-duel-20261025/arena/` reads the same file. Its dsh
profile is `arena/dsh-home/profiles/bench-dash/cordis.patch.yml`; its OpenCode
config is generated into `arena/.state/opencode.json` at run time.

## W&B inference (previous development route)

| field | value |
|---|---|
| Base URL | `https://api.inference.wandb.ai/v1` |
| Model | `deepseek-ai/DeepSeek-V4.1-Flash` |
| Env | `WANDB_API_KEY` |
| Owner key file | `D:\проекты\KaroX-v5\wandb-api-key.txt` (outside the repository) |

The dsh W&B profile remains at
`lab/kernel-duel-20261025/live-bench-20261026/dsh-home/profiles/bench/`.
It is kept for comparison, not used by default.

## Rules

- Never commit, paste or print a raw key. `docs/` and tests refer to the
  environment variable, never to the value.
- Never bake these routes into default provider lists, presets or a release
  wheel. A development route is opt-in through `--model/--base-url` or an
  explicit provider config.
- A key that appears in a log, a transcript or a patch is compromised: rotate
  it and update the git-ignored file.
- The free route can rate-limit under parallel benchmark load. That is a
  benchmark failure mode, not a reason to lower the reasoning effort silently.