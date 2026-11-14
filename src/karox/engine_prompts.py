"""Model-facing engine prompts and the execution contract.

These strings are appended to the product system prompt and therefore live in
one place, outside the kernel control flow. The neutrality scan treats this
module as model-facing: no task-specific checks or harness vocabulary belong
here.
"""

from __future__ import annotations


_CLOSING_TURN_PROMPT = (
    "KaroX ended the work phase: the visual check budget is spent and no "
    "verdict covers the current page, so this run ends unverified. No further "
    "edits, checks or browser calls will run. Call finish now, as the only tool "
    "call, with your final answer for the user: what was delivered and what is "
    "still unverified. If the task asks for a file's contents in the reply, "
    "list that file in answer_files instead of retyping it; if the reply must "
    "contain only that file, leave answer empty (KaroX reports the unverified "
    "status itself)."
)

#: The same single call when KaroX's own final checks passed the current
#: bytes: the run is verified, only the user's answer is missing.
_CLOSING_TURN_VERIFIED_PROMPT = (
    "KaroX ended the work phase and its final checks passed on the current "
    "files, so the work is verified. No further edits, checks or browser calls "
    "will run. Call finish now, as the only tool call, with your final answer "
    "for the user: what was delivered. If the task asks for a file's contents "
    "in the reply, list that file in answer_files instead of retyping it; if "
    "the reply must contain only that file, leave answer empty."
)

#: The engine execution contract appended to the product system prompt. It is
#: deliberately the last thing the model reads before deciding an action.
#:
#: Batching is part of the contract because it is the one lever the loop can
#: give the model without spending turns: every tool call in a response runs in
#: order and all observations return together, so two independent reads cost one
#: model call instead of two. The product prompt asks for the same thing; this
#: text used to say "prefer exactly one", which contradicted it and taught the
#: model to serialise work. Dependent calls stay serialised: a later argument
#: must not be guessed from a result the same response has not seen yet.
_ENGINE_CONTRACT = """

# KaroX engine execution contract

You act through typed tool calls. Commands run through checks_run in the
forms the approved command set lists; when it approves a shell (for example
bash -lc *), checks_run runs a whole script that way.
- Every response is a full round trip: the whole conversation is read again
  before your next step, so the number of responses, more than the number of
  calls, sets a run's time and cost. Put independent calls (reads, searches,
  edits whose arguments you already know) in one response; they run in order
  and return together. A call whose arguments depend on another call's
  result goes in a later response.
- Observations are clipped: ask for the part you need. After each edit KaroX
  runs quick syntax and lint checks on the changed files; your tests run only
  when you call them, and never a whole test directory of a large project:
  its unrelated failures cost minutes and say nothing about your change. A
  fixed (non-*) approved command runs once at the end. At submission KaroX
  re-runs what is still owed on the final files and returns failures for
  repair. An identical check always runs again in full.
- When the task is complete, call the finish tool with {"answer": "<final
  answer text>"}. If the task wants a file's contents in the reply, list the
  path in "answer_files" instead of retyping it; KaroX appends the exact file.
  A file the task only asks you to save stays out of the reply. After an
  accepted finish you cannot continue working. finish must be the only tool call in
  its response; if it is batched with other calls, it is refused and the
  other calls run, so call finish again after reviewing them.
- Never print tool markup as prose. Every response carries a tool call; end
  the work with finish. A reply with no tool call is a format error that
  costs a whole step.
"""

#: Appended only when the request carries code_run: sessions without write and
#: check rights never get the tool, and a contract naming it there sent the
#: model after a tool it did not have.
_CODE_MODE_CONTRACT = """
- When the next steps depend on each other but need no judgment in between
  (search and read the hits, run a probe and then the covering tests, make
  the same edit in several files), write them as one code_run script and
  print only what you need to read.
"""

#: Appended only when the request carries browser_command (the task reads as a
#: page, UI or game). Python-library tasks used to carry these paragraphs too,
#: and a model reading them hunted for an index.html that did not exist.
_WEB_CONTRACT = """
- A page must behave as documented in every state a user or a test can reach,
  including right after a reset: every named control works, and behaviour that
  should happen on its own starts without help. Prove it the way a user would:
  browser_command `open` the deliverable page, then `click` or `press` each
  named control and read the result in the same response (visual_check cannot
  click); let automatic behaviour run from a fresh load instead of forcing it.
- finish may also carry "requirements": {"id", "status", "observed"} entries
  for the requirement IDs KaroX lists, each verified entry quoting a browser,
  check or visual result received after your last edit. The list is advisory
  (only "unmet" keeps the run open), so never re-run checks only to collect
  quotes.
"""


__all__ = [
    "_CLOSING_TURN_PROMPT",
    "_CLOSING_TURN_VERIFIED_PROMPT",
    "_CODE_MODE_CONTRACT",
    "_ENGINE_CONTRACT",
    "_WEB_CONTRACT",
]
