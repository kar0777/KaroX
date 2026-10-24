---
name: debug-failing-test
version: 1.0.0
description: Find and fix the cause of a failing test or crash with a minimal change.
when_to_use: a test fails, a traceback, a crash, "it does not work", a regression.
triggers: [pytest, traceback, error, fail]
---
# Debug recipe
1. Reproduce: run the single failing test or command and keep the exact error text.
2. Locate: read the traceback from the bottom frame that is in this repo. Read only those functions.
3. Hypothesis: write one sentence: "X fails because Y". Check it with one targeted read, search or small command.
4. Fix the cause, not the symptom. Do not weaken or delete the test, add sleeps, catch-all excepts or skips to make it pass.
5. Re-run the same test, then the nearby tests of the same module.
6. If the hypothesis was wrong twice, widen the view: git log/diff of the area, callers of the function, recent changes.
7. Report: the cause in one sentence, the fix (path:line), the tests you ran and their result.
