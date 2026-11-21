# Speed of the chat UI: how it is measured, and the numbers

`python scripts/tui_perf.py` starts the real `karox6 chat` in a pseudo-terminal (ConPTY on
Windows, a pty elsewhere) against a scripted local model and reads the screen through a
terminal emulator, so each time is what a user sees, from process start or key press to
the cells on screen. Nothing is mocked inside KaroX.

Measured 2026-11-21 on the development PC (Windows 11, Python 3.12, ConPTY, 100x30 and
120x40 windows), commit 1326c89+:

| what | before (dev34) | now |
|---|---|---|
| first frame (welcome card, status bar, prompt) | 453 ms | **170 ms** (median of 7) |
| ready for input (a key typed at once is echoed) | ~415 ms | 414 ms |
| 6,000 lines streamed and shown | 1.04 s | 1.29 s, 8 KB written to the terminal |
| PageUp / Ctrl+Home / Ctrl+End in that session | <= 30 ms | 25 / 51 / 26 ms |
| key -> echo while a reply streams | median 20 ms | median 29 ms, p90 56 ms |
| terminal output while a reply streams | 8 KB/s | 7 KB/s (cell diffs, no full redraws) |
| one paragraph of 30,000 characters streamed (CPU in the UI) | 11.2 s (29.8 s dim) | 10 ms (17 ms) |
| 20,000 lines: resize (full re-wrap) | 211 ms | 111 ms |

What the numbers mean:

* First frame. Python needs ~65 ms to start here and the imports the app needs (asyncio
  ~90 ms, prompt_toolkit ~130 ms) come after that, so the app itself cannot draw before
  ~400 ms. `karox6/splash.py` paints the same frame with plain writes before those
  imports; the app then redraws the same cells (the test compares both frames). Keys typed
  in between wait in the terminal's queue: "ready for input" is unchanged, and that is
  the honest limit of this stack on this machine. `KAROX_NO_SPLASH=1` switches it off.
* The long paragraph was the one real hang: every streamed piece re-parsed and re-wrapped
  the whole line. Reasoning shown with `/reasoning show` is one long dim paragraph, the
  worst case.
* The remaining figures did not need work; they are here so a regression shows.

The in-process part (`_TranscriptControl`) is covered by `tests/test_transcript.py`
(including a time bound on the long paragraph); the terminal part by
`tests/test_terminal.py`.
