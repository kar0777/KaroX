# Vendored engine notice

This package is a fork of mini-swe-agent 2.4.6 (MIT), vendored as
`karox.engine` from `lab/karo-core` (karo-core 0.5.1).

Upstream: https://github.com/SWE-agent/mini-swe-agent (MIT, Copyright (c) 2025
Kilian Lieret, Carlos E. Jimenez and contributors).

Local changes (the five ports, each with tests):
1. `environments/local.py`: command-output clipping (50k chars).
2. `agents/default.py`: deterministic history compaction (160k est tokens).
3. `agents/default.py`: a prose final answer after the format-error budget is
   a submission, not a crash; text carrying a bash fence is never a finish.
4. `agents/default.py`: four identical action+observation pairs submit the run.
5. `models/litellm_model.py`: a command in exactly one bash fence is executed.
