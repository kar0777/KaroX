# Pre-registration: dev59 mechanism pilot (run A), 2026-11-24

Candidate: steps/dev59 = dev56 + one bash-tool-description rule (bash is for running programs; read/search/
modify files with read/grep/glob/edit/write). Wheel karox6-6.0.0.dev59.
Council 10 (Codex gpt-6.2-sol): approved with modifications below.

- Tasks: the 6 mine tasks of run wait57 (night ids 28,29,32,33,41,42; chosen earlier for wait57, not by outcome),
  which have dev56 logs for comparison. Arm karox6 = dev59 only, 1 attempt, upstream anthropic (Haiku 5.5).
- Expected $0.06-0.10.
- PASS iff (1) >= 4/6 trials make repository changes successfully through edit or write tools, AND
  (2) <= 2/6 trials attempt any direct file edit through bash (sed -i, echo/heredoc/printf redirection into
  repository files, python/perl rewriting files; failed attempts count; writes of test/build output and /tmp
  excluded). A trial may count in both.
- Solves reported per task, not part of the rule.
- FAIL -> stop, no wording escalation. PASS -> pool5 A/B dev56 vs dev59, rule pre-registered before it.
