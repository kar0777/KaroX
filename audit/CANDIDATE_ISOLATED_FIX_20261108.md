# KaroX 5 Linux candidate — isolated interpreter correction (2026-11-08)

## Why the first lab adapter was insufficient

The first offline lab adapter used `PYTHONPATH` to make a repaired wheel
take precedence over a baseline KaroX lab venv. This was NOT sufficient:
approved KaroX checker subprocesses run `python -I -m karox.web_check`,
and `-I` ignores `PYTHONPATH`. They could therefore use the *baseline*
checker while the main CLI imported the repaired candidate.

**Do not use the old Python-path-overlay adapter for scored runs.**

## Correction and verified evidence

- Created a **separate WSL lab-only virtualenv**
  `~/.cache/karox-fair-linux-20261108/karox-candidate-isolated-20261108`;
  the official baseline venv has not been replaced.
- Verified the wheel's SHA-256 and all its RECORD entries, placed its
  actual Python code in the candidate virtualenv's site-packages.
- Reused dependency wheels through a local `.pth` pointing only at the
  existing lab virtualenv's dependency environment. This is **not**
  a hermetic all-dependencies clean installation.
- Candidate Karo CLI wrapper uses a new venv shebang with **`-I`**;
  all candidate runtime/checker modules are loaded from that venv,
  including by checker child processes.
- Tested the main CLI help with a forged `PYTHONPATH` and
  a task-written fake `karox/web_check.py`: no marker executed.
  Real checker accepted legitimate HTML with challenge echo and
  rejected deliberately invalid HTML.
- Adapter `audit/triad_linux_candidate.py` now validates **all**
  Karo Python package files against the signed-by-hash wheel,
  verifies interpreter and checker origins in `-I` mode, and chooses
  the lab candidate executable for Karo. It does not change the
  official OpenCode V2/DSH commands or their settings.
- Offline AST-level preflight executed both adapter functions, checked
  exact three-arm routing and environment non-mutation. No real model
  or provider call occurred.

Records:
`audit/LINUX_CANDIDATE_ISOLATED_VENV_20261108.json`,
`audit/LINUX_CANDIDATE_ISOLATED_ENTRYPOINT_20261108.json`,
`audit/LINUX_CANDIDATE_ADAPTER_PREFLIGHT_20261108.json`.

Wheel SHA-256:
`485c8ebfbcff994941c09375eda7907c29ad99fd74bedb542b448c2b0affbdc0`.

This is a **DEV transport readiness correction**, not a new competitor run.
It does not change grader v2, source task, user prompt, provider endpoint,
DeepSeek model ID, previous results or any scoring verdict.

## Remaining limitations

The original Linux evaluator lacks Playwright/Chromium; the frozen Windows
Chromium external grader remains required for fair 3D acceptance.
The existing common WSL baseline venv dependencies remain a shared
dependency layer; KaroX code itself is isolated to the candidate venv.
The API trial remains blocked by the environment unless later explicitly
allowed; no claim of three-way leadership is supported. No installation
over the user's Karo, git push or release was performed.
