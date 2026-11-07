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

You act through typed tool calls; there is no shell.
- Batch independent actions: several tool calls in one response execute in
  order and their observations return together. Batch reads, and batch 2-4
  mutations whose arguments are already fully known. Never batch a call whose
  arguments depend on another call's result.
- Keep tool output small; observations are clipped.
- Read before you write. KaroX runs the approved checks itself after a change
  and returns failures for repair; call checks only when the result changes
  your next step. Run the narrowest test that covers your change first and
  the approved verification command once at the end; a passing check on
  unchanged files is not run again. Skip whole-repository runs of a large
  project's suite: its unrelated or pre-existing failures cost steps and say
  nothing about your change.
- An interactive page must behave as documented in every state a user or test
  can reach, including right after a test hook was called and after a reset:
  every documented key works, and behaviour that should happen on its own
  (spawning, movement, timers) starts within the first seconds of a run.
  Prove it the way a user would: press each named key with browser_command
  on the deliverable page, and let time pass from a fresh reset instead of
  forcing the behaviour.
- When the task is complete, call the finish tool with {"answer": "<final
  answer text>"}. If the task wants a file's contents in the reply, list the
  path in "answer_files" instead of retyping it; KaroX appends the exact file.
  A file the task only asks you to save stays out of the reply.
  For a web deliverable finish may also carry "requirements": {"id",
  "status", "observed"} entries for the requirement IDs KaroX lists, each
  verified entry quoting a browser, check or visual result you already
  received after your last edit; the list is advisory (only "unmet" keeps the
  run open), so never re-run checks only to collect quotes. After an accepted
  finish you cannot continue working. finish must be the only tool call in
  its response; if it is batched with other calls, it is refused and the
  other calls run, so call finish again after reviewing them.
- Never print tool markup as prose; a response without a tool call is a format
  error.
"""


__all__ = [
    "_CLOSING_TURN_PROMPT",
    "_CLOSING_TURN_VERIFIED_PROMPT",
    "_ENGINE_CONTRACT",
]
