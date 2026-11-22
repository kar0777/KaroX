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

The same script on Linux (WSL2 Ubuntu on the same PC, Python 3.12.14, a POSIX pty, commit
ccf8412; the times include the test harness's launcher, one extra interpreter start of
~10 ms):

| what | Linux |
|---|---|
| first frame | 44 ms (median of 7); 119 ms with `KAROX_NO_SPLASH=1` |
| ready for input | 148 ms (142 ms without the start-up frame) |
| 6,000 lines streamed and shown | 0.26 s, 6 KB written to the terminal |
| PageUp / Ctrl+Home / Ctrl+End in that session | 23 / 23 / 23 ms |
| key -> echo while a reply streams | median 25 ms, p90 48 ms |
| terminal output while a reply streams | 8 KB/s |

On a POSIX terminal the keys typed before the app reads them are kept as typed only since
6.0.0.dev37 (the frame switches the terminal's echo and line editing off; before that
Enter arrived as Ctrl+J and the "ready for input" figure would have been the terminal's
own echo, not the app's).

6.0.0.dev40 (the new layout: activity line, framed input box, status bar below it, markdown
answers; 2026-11-22, same PC, `python scripts/tui_perf.py --runs 7`):

| what | Windows (ConPTY) | Linux (WSL2 pty) |
|---|---|---|
| first frame | 175 ms (median of 7) | 45 ms |
| without the start-up frame | 368 ms | 122 ms |
| ready for input | 424 ms | 143 ms |
| 6,000 lines streamed and shown | 1.14 s, 9 KB written | 0.28 s, 9 KB written |
| PageUp / Ctrl+Home / Ctrl+End | 24 / 47 / 24 ms | 23 / 23 / 23 ms |
| key -> echo while a reply streams | median 27 ms, p90 30 ms | median 25 ms, p90 48 ms |
| terminal output while a reply streams | 7.7 KB/s | 8.6 KB/s |

`scripts/perf_check.py`: start 116.8 ms, idle RSS 30.0 MiB.

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
