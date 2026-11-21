# Devin report (branch `devin/big`, bench branch `devin/bench`)

Status: IN PROGRESS. Stages are added as they finish; details and every run's
pre-registration and result are in `PROGRESS.md` ("Devin iteration").

## Stage 1 (2026-11-21 18:10-18:40 UTC): setup, analysis of saved runs, dev35

- Worktrees `D:\проекты\karox6-wt-devin` (devin/big) and `D:\проекты\bench-wt-devin`
  (devin/bench); own venv with pyte, pywinpty, build (commit e03012f).
- Analysis of saved runs, no new runs (PROGRESS.md 18:30; scripts in the bench repo
  `scripts/analysis/`): the "1.6x reasoning" is replies cut while thinking on Dahl's
  8192-token cap; on long tasks KaroX's cost is context (compaction at 320k vs OpenCode's
  ~171k), and every recorded compaction dropped the provider's prompt cache.
- 6.0.0.dev35 (commit d5d3000): cache-aware compaction, thinking-only cut notice and
  effort easing, Retry-After as a floor, daily allowance = quota, empty reply resent once,
  stalled stream = timeout. 46 new tests (tests/test_retry.py, tests/test_session_limits.py).
- Bench (60fd856): dev35 wheel, `trio_report.py` with agent time and cost-equivalent, a
  scoring fix (the "waited and timed out" clause never fired for KaroX).
- Runs pre-registered: devd1 (Dahl, mechanism), devs1 (ReformBoss smoke), devt1/devt2.

## Stage 2 (18:45-20:50 UTC): the chat in a real terminal, fixes, features, speed

Method: the real `karox6` process in a ConPTY/pty, screen read through pyte
(`tests/term.py`), scripted local model (`tests/fake_model.py`). Commits 9daab3d, 0a616e2,
1326c89, 7096d91 (version 6.0.0.dev36).

Bugs found and fixed (each has a test):
1. First frame took ~450 ms; the welcome card popped in one frame after the status bar.
   Now painted before the heavy imports (`splash.py`), 170 ms, same cells as the app's frame.
2. A streamed paragraph was re-parsed and re-wrapped whole on every delta: 30,000
   characters cost 11 s of CPU (30 s for dim reasoning). Now 10-17 ms.
3. Colour of a multi-line write was lost after its first line.
4. Text pasted without bracketed paste sent every line as its own message.
5. Provider errors were raw JSON, printed twice; now one sentence plus a next step.
6. A message sent while a turn runs was queued invisibly (status bar now counts it).
7. Status bar lost the approval mode on narrow windows; the welcome card wrapped.
8. A full-height scrollbar was drawn when nothing could scroll.
9. `/status` and the status bar disagreed on context size before the first turn.
10. A refused command printed the whole refusal text in the tool line.
11. Tab stops counted characters, not cells (wrong after wide characters).
12. "[completed]" was printed before the turn had ended: Ctrl+C right after it stopped
    "the turn" instead of arming the exit.

Features added: `/undo` + `/redo` (workspace snapshots in a private git store; the user's
repository is never touched; newer user edits are never overwritten), `/fork`, `--fork`,
`-c/--continue`, `/init` (AGENTS.md), pictures (`@shot.png`, dragged path, clipboard via
`/paste` or Alt+V; all four provider kinds), `/mcp` screen, change preview under each
edit, "did you mean" for mistyped commands.

Tests: `tests/test_terminal.py` (15 flows in a real terminal), `test_transcript.py`,
`test_tui_text.py`, `test_chat_features.py`, `test_checkpoint.py`. Suite: 1103 passed, 5 skipped.

Speed (docs/perf.md, `python scripts/tui_perf.py`): first frame 453 -> 170 ms; ready for
input 414 ms (interpreter + imports; not improved); 6,000 lines shown in 1.3 s with 8 KB
of terminal output; key echo while streaming: median 29 ms; PageUp 25 ms.
