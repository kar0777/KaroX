"""Engine tool-environment constants and shared policy tables."""

from __future__ import annotations

import re
from .engine_tool_bodies import (  # noqa: E402,F401
    _FENCE_LANGUAGES as _FENCE_LANGUAGES,
    fenced_file as fenced_file,
    layout_problem as layout_problem,
    layout_refusal_message as layout_refusal_message,
    _counts_as_repository_change as _counts_as_repository_change,
    _REPO_DELETE_ACTIONS as _REPO_DELETE_ACTIONS,
    _patch_header_path as _patch_header_path,
    _pure_delete_targets as _pure_delete_targets,
    scratch_delete_paths as scratch_delete_paths,
    absent_delete_paths as absent_delete_paths,
    _requested_dry_run as _requested_dry_run,
    _EDIT_ALIASES as _EDIT_ALIASES,
    _normalize_edit_file as _normalize_edit_file,
    _normalize_repo_command as _normalize_repo_command,
    _batch_tail as _batch_tail,
    _stable_result as _stable_result,
    _progress_key as _progress_key,
    PYTEST_MISSING_HINT as PYTEST_MISSING_HINT,
    _PYTEST_MISSING as _PYTEST_MISSING,
    _pytest_missing as _pytest_missing,
    _read_lines_body as _read_lines_body,
    _visual_check_body as _visual_check_body,
    DIFF_ECHO_MAX_CHARS as DIFF_ECHO_MAX_CHARS,
    _HUNK as _HUNK,
    _compact_diff as _compact_diff,
    _mutation_body as _mutation_body,
    _checks_body as _checks_body,
    _failed_check_report as _failed_check_report,
)


_READABLE_TOOLS = frozenset({"repo.read_file", "repo.read_lines"})

_TEST_TOOLS = frozenset({"checks.run", "checks.run_affected", "tests.run"})

_PROOF_TOOLS = frozenset({"checks.run", "visual.check", "git.status", "git.diff"})

#: Proof bookkeeping, not an observation. ``completion_identity`` carries the
#: whole task text and every file hash; the completion gate reads it from the
#: durable result, so putting it in the model-facing body only makes every later
#: request re-send an 8 KB echo of the brief. Six visual checks paid about
#: 640k prompt tokens for that in the aquarium run.
_BOOKKEEPING_KEYS = frozenset({"completion_identity", "completion_target"})

#: A finish call found in a batch with other calls. It is rewritten to this
#: marker so the other calls still execute and every call gets its observation;
#: the model must then submit again with finish alone.
FINISH_BATCH_MARKER = "__karox_finish_not_alone__"

#: finish argument naming files whose exact contents belong in the reply.
ANSWER_FILES_FIELD = "answer_files"

ANSWER_FILES_LIMIT = 8

ANSWER_FILES_MAX_BYTES = 1_000_000

#: A repair edit is followed by exactly the tests that failed, once, inside a
#: budget that cannot turn one edit into a long detour.
AFFECTED_TESTS_TIMEOUT_SECONDS = 20.0

AFFECTED_TEST_LIMIT = 4

#: Tools whose result says "a file changed". The model needs the diff, the
#: quick-check verdict and the affected-test verdict; hashes, previous hashes,
#: secret flags and evidence ids stay in the durable result but do not ride in
#: every later request's history.
_MUTATION_TOOLS = {"repo.edit_file", "repo.write_file", "repo.apply_patch", "repo.command"}

#: A refused call is not progress, and a model that keeps reshaping the same
#: rejected arguments spends a whole run on it: one benchmark run sent 37
#: repo.command variants and 2.03M tokens to delete two scratch files. After
#: this many consecutive refusals of one tool the observation starts telling the
#: model to stop; after the higher number the tool is disabled for the run.
REFUSAL_GUIDANCE_AFTER = 2

REFUSAL_DISABLE_AFTER = 6

#: Some tools are cheap to stop early: a patch that fails to parse twice will
#: not parse on the third try, and ``tests.run`` with a target that is not a
#: test file keeps the same bad path. In the sympy run these two shapes used
#: seven turns. The refusal message names the working alternative.
TOOL_FAILURE_LIMITS = {"repo.apply_patch": 2, "tests.run": 2}

_TOOL_ALTERNATIVE = {
    "repo.apply_patch": (
        "Use repo.edit_file with old_string/new_string, or repo.write_file for "
        "a whole file. To delete a file this session created, use repo_command "
        '{"action": "batch", "payload": {"operations": [{"op": "delete", '
        '"path": "<file>"}]}}.'
    ),
    "tests.run": (
        "Use checks.run with the approved command, or point targets at "
        "existing test files (test_*.py, *_test.py, or files under a tests/ "
        "directory)."
    ),
}

#: A browser can turn verification into an unbounded exploration: the two 3D
#: re-runs used 57 and 61 browser calls, fixed the actual defects, then hit the
#: wall clock without finishing their proof. Warn at the lower number, stop
#: accepting browser calls at the higher one. The model can still read the
#: repository and ask visual.check; it just cannot keep probing.
BROWSER_CALLS_NOTICE = 20

BROWSER_CALLS_EXHAUSTED = 36

#: Input actions stay available after the observation budget: the model may
#: spend 36 calls looking and only then reach the restart it must press. One
#: click is cheap; the churn was repeated open/screenshot/wait. A separate cap
#: keeps a click loop from replacing the observation loop.
BROWSER_INPUT_LIMIT = 12

#: Key presses have their own cap. A line that names keys is verified only by
#: pressing each of them on the deliverable after the last edit; a platformer's
#: controls name nine keys, so counting presses against the click cap (or the
#: observation budget) made honest verification of one line cost the whole
#: input budget. A press loop is still bounded.
BROWSER_PRESS_LIMIT = 30

#: A web deliverable's finish may attest each requirement against results
#: observed after the last edit. A finish is refused for a missing or stale
#: attestation at most this many times; after that the submission is accepted
#: and the completion floor reports every line left unattested, so the run
#: ends honestly unverified instead of buying quotes. Zero: the attestation is
#: advisory. In the diag1 sandbox round (2026-11-05) the refusals cost 57 model
#: steps across seven browser tasks -- the most expensive steps of each run,
#: at full context -- and no web deliverable changed after any of them.
COVERAGE_REFUSAL_LIMIT = 0

#: A page checked at a viewport and changed afterwards is checked again before
#: finish: at most this many of those viewports, most recent first, and the
#: finish is refused at most ``LAYOUT_REFUSAL_LIMIT`` times when one of them now
#: scrolls sideways. In the r2b sandbox round (2026-11-05) a game page fitted a
#: 390x844 window when the run measured it; a later sizing edit made it 678 px
#: wide there, the run re-checked only 1440x900, and the delivered page failed
#: its mobile layout requirement.
LAYOUT_RECHECK_VIEWPORTS = 3

LAYOUT_REFUSAL_LIMIT = 1

#: Page width and the element that sticks out furthest, read after layout and
#: any load-time sizing script have settled.
LAYOUT_PROBE_JS = """async () => {
  await new Promise((resolve) => setTimeout(resolve, 300));
  const root = document.documentElement;
  const width = window.innerWidth;
  const out = {scroll_width: root.scrollWidth, viewport_width: width};
  if (root.scrollWidth <= width + 1) return out;
  let worst = null;
  let right = width + 1;
  for (const el of document.querySelectorAll('body *')) {
    const box = el.getBoundingClientRect();
    if (box.width > 0 && box.right > right) { right = box.right; worst = el; }
  }
  if (worst) {
    const box = worst.getBoundingClientRect();
    let name = worst.tagName.toLowerCase();
    if (worst.id) name += '#' + worst.id;
    else if (worst.classList.length) name += '.' + Array.from(worst.classList).slice(0, 2).join('.');
    out.widest = {element: name, left: Math.round(box.left), right: Math.round(box.right), width: Math.round(box.width)};
  }
  return out;
}"""

#: Each refused finish reopens a little browser budget: the attestation asks
#: for fresh observations, and a run that spent its probes before its last
#: edit must still be able to produce them.
COVERAGE_BROWSER_GRANT = 6

COVERAGE_INPUT_GRANT = 3

_BROWSER_INPUT_ACTIONS = frozenset(
    {"click", "dblclick", "drag", "fill", "select", "type", "hover"}
)

#: Once this little wall time remains, every observation carries one line: stop
#: exploring and finish. The kernel also reserves this window for final proof.
TIME_LEFT_NOTICE_SECONDS = 300.0

#: The completion gate requires a real input for a task that names one of these
#: controls. The model must hear that before it spends the visual budget, not
#: only when it tries to finish.
_INTERACTION_TASK = re.compile(r"\b(?:restart|hotspot)\b", re.IGNORECASE)

#: A reset is exercised where it is hard: while something still moves. A
#: measured 3d-viewer run (f3) copied the camera home on reset but left the
#: orbit controls' damping running; the user's check clicked reset about two
#: seconds after an orbit, the camera kept drifting and the next hotspot never
#: held still, so its click failed. The model's own reset check ran in the
#: next model turn, about ten seconds after its drag, when the damping had died
#: out, and read a perfect initial state. The passing f2 page stopped all
#: motion on reset.
RESET_IN_MOTION_HINT = (
    "Test a reset or restart while something is still moving: drag or play, "
    "then click it in the same response and read the state at once -- "
    "momentum or damping that survives the reset carries the page away from "
    "its initial state."
)

#: A code task is not a reading exercise. On swe-requests-6028 the model spent
#: about two dozen model steps downloading and comparing three upstream
#: releases before its first edit; one such run cost 2.1M tokens with only one
#: failed tool. These milestones put a direct "stop reading, make the change"
#: line into the observation. They are advice, not a hard stop: a genuinely
#: unfamiliar repository can ignore the first one and keep going.
RESEARCH_BUDGET_STEPS = 12

RESEARCH_HARD_BUDGET_STEPS = 24

#: The budget only speaks to tasks whose deliverable is a change. A read-only
#: question may legitimately survey twenty files; telling it to edit would be
#: wrong. Plan/ideate and maintenance runs also never mutate.
_CHANGE_INTENT = re.compile(
    r"\b(?:build|create|implement|fix|bug|refactor|add|update|change|remove|"
    r"delete|migrate|rename|repair|patch)\b"
    r"|исправ|добав|созда|реализ|почини|переимен|обнови|удали",
    re.IGNORECASE,
)
