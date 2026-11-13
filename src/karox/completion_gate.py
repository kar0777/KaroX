"""Deterministic completion floor; no provider calls or new persistent state.

Uses the session's changed/read paths and existing evidence metadata. Unsupported
validators are not invented. Unknown proof is missing proof, never success.
"""
from __future__ import annotations

from .completion_rules import (  # noqa: F401
    _TEXT as _TEXT,
    NO_PRODUCER as NO_PRODUCER,
    _DOC_DIRS as _DOC_DIRS,
    _ROOT_DOC_PREFIXES as _ROOT_DOC_PREFIXES,
    _BEHAVIOR as _BEHAVIOR,
    _deliverable as _deliverable,
    _active_requirement_texts as _active_requirement_texts,
    _is_test_path as _is_test_path,
    is_doc as is_doc,
    docs_only as docs_only,
    _tracked_at_head as _tracked_at_head,
    _changed_existing_tests as _changed_existing_tests,
    _TEST_CHANGE_REQUEST as _TEST_CHANGE_REQUEST,
    _NEGATED_TEST_CHANGE as _NEGATED_TEST_CHANGE,
    _SENTENCE_BREAK as _SENTENCE_BREAK,
    _CONDITIONAL_REQUEST as _CONDITIONAL_REQUEST,
    _TESTS_OUT_OF_SCOPE as _TESTS_OUT_OF_SCOPE,
    _TEST_EXECUTION_IN_MATCH as _TEST_EXECUTION_IN_MATCH,
    _FORBIDS_TEST_EDITS as _FORBIDS_TEST_EDITS,
    _explicit_test_change_required as _explicit_test_change_required,
    _test_edit_ban as _test_edit_ban,
    diagnostic_paths as diagnostic_paths,
    _created_then_deleted as _created_then_deleted,
    identity as identity,
)
from .completion_visual import (  # noqa: F401
    _WEB as _WEB,
    _VISUAL_ASSETS as _VISUAL_ASSETS,
    _PAGE_ASSETS as _PAGE_ASSETS,
    _VISUAL as _VISUAL,
    _NEGATED_VISUAL as _NEGATED_VISUAL,
    _WEB_INTENT as _WEB_INTENT,
    _VISUAL_DIRECT as _VISUAL_DIRECT,
    _VISUAL_OBJECT as _VISUAL_OBJECT,
    _VISUAL_OBSERVABLE as _VISUAL_OBSERVABLE,
    _VISUAL_CONTRACT_ONLY as _VISUAL_CONTRACT_ONLY,
    _VISUAL_CODE_LITERAL as _VISUAL_CODE_LITERAL,
    _VISUAL_IMPLEMENTATION as _VISUAL_IMPLEMENTATION,
    _VISUAL_REVIEW_LIMIT as _VISUAL_REVIEW_LIMIT,
    visual_intent as visual_intent,
    web_render_intent as web_render_intent,
    visual_required as visual_required,
    visual_review_entries as visual_review_entries,
    visual_producer_available as visual_producer_available,
    _page_loads_the_change as _page_loads_the_change,
    visual_proven as visual_proven,
)
from .completion_review import (  # noqa: F401
    same_run_inputs as same_run_inputs,
    command_classes as command_classes,
    browser_proof as browser_proof,
    _complete_change_review as _complete_change_review,
    _review_skips_a_change as _review_skips_a_change,
)

from pathlib import Path
import re
import subprocess
from typing import Any, Optional, Sequence

from .auto_verify import _select_checks
from . import check_baseline, git_baseline, requirement_coverage
from .web_check import default_pages, default_pictures, is_scratch_page

# Prefix of a floor reason naming an owed proof class Core has no command for.
# Markdown is only documentation where documentation lives. A .md inside a code
# tree can be runtime data: KaroX itself loads prompts/*.md and SKILL.md.
# What a visual requirement can be about: the page and everything that styles or
# renders it. A Python change is never graded by a picture.
_RUNTIME = {"__main__.py", "main.py", "app.py", "server.py", "cli.py", "package.json"}
# Assets a page loads by relative path. A canvas game often edits only
# ``game.js`` and leaves the scaffold ``index.html`` untouched; the visual check
# still rendered that page, so its bytes are the proof surface.

# A conditional or template item is not an explicit request. A SWE issue quotes
# the upstream PR checklist ("- [ ] Include new tests or update existing tests
# when applicable."); read as a demand it overrode the task's own "Do not modify
# tests": the gate refused the finish with "explicit test change requested" and
# the model edited the protected test file (sandbox 2026-11-04,
# swe-pytest-10356).
# A scope instruction puts tests out of bounds ("changes go in the source
# tree, not in the test files"). The negation follows the change verb, so the
# prefix rules above cannot see it; the whole sentence is the unit of intent.












#: A sentence that keeps the existing tests as they are: "Do not modify tests",
#: "don't touch the test files", "не меняйте тесты". Only change verbs count, so
#: "do not add tests" alone does not forbid repairing an existing test. The
#: noun must be plural or a test-file phrase: "do not change the test command"
#: is not a ban.
















def coverage_required(core: Any, record: Any) -> bool:
    """Whether finishing owes a requirement-by-requirement attestation.

    Only a web deliverable owes it: its behaviour is observable through the
    browser tools, and its spec is a feature list that a frame and a green
    check cannot cover item by item. A change to code without a page, a
    documentation edit, a probe page, and a request too short to need a
    checklist (fewer than ``requirement_coverage.MIN_CLAUSES`` clauses) owe
    nothing new.
    """

    if not getattr(record, "requirements", None):
        return False
    diagnostic = diagnostic_paths(core, record)
    deliverables = [p for p in record.changed_files if _deliverable(p, diagnostic)]
    if not any(Path(p).suffix.lower() in _VISUAL_ASSETS for p in deliverables):
        return False
    if all(is_doc(p) for p in deliverables) and not _BEHAVIOR.search(record.task):
        return False
    if len(requirement_coverage.clauses(record.requirements)) < requirement_coverage.MIN_CLAUSES:
        return False
    return bool(default_pages(core.repository))




























def _approved_visual_producer_for_change(
    core: Any, record: Any, pages: Sequence[str], diagnostic: set[str] | frozenset[str]
) -> bool:
    """An approved visual.check can *attempt* browser proof of the changed page.

    This only marks the evidence *producible*; it never marks evidence *passed*.
    An unrelated page, a scratch probe, or an unavailable visual tool cannot
    discharge runtime proof. The actual visual result must still be successful,
    current to the changed bytes, and accepted by the existing evidence chain.
    """
    if visual_producer_available(core) is not True:
        return False
    changed = list(getattr(record, "changed_files", ()) or ())
    return any(
        (page in changed or _page_loads_the_change(core, page, changed, diagnostic))
        for page in pages
        if not is_scratch_page(page) and page not in diagnostic
    )


def floor(core: Any, record: Any) -> list[str]:
    """Missing/failed proof reasons for the current state; [] means proven.

    P4 baseline probes are deliberately not synthesized: an exception class alone
    cannot distinguish a requested missing API from a broken harness. Where no
    trusted behavior producer exists we report missing behavior evidence.
    """
    if not record.changed_files:
        return []
    current = identity(core, record)
    if not current:
        return ["relevant state unreadable"]
    docs = all(is_doc(p) for p in record.changed_files)
    behavioral = not docs or bool(_BEHAVIOR.search(record.task))
    pages = default_pages(core.repository)
    # A standalone SVG is rendered and judged like a page, so a picture task
    # owes -- and can produce -- the same browser proof.
    browser = (
        behavioral
        and bool(pages or default_pictures(core.repository))
        and any(Path(p).suffix.lower() in _VISUAL_ASSETS for p in record.changed_files)
    )
    runtime = behavioral and (
        browser or any(_is_runtime_entry(core, p) for p in record.changed_files)
    )
    persisted = {e.get("evidence_id"): e for e in record.evidence if isinstance(e, dict)}
    #: The run's own unreferenced harness pages: like scratch probes, their
    #: verdicts are diagnostics, neither owed failures nor proof.
    harness_pages = diagnostic_paths(core, record)
    latest: dict[tuple[str, ...], tuple[bool, set[str]]] = {}
    failed_ever: dict[tuple[str, ...], None] = {}
    structural: dict[str, bool] = {}
    #: Pages with a pass on the current identity. A later aesthetic re-judgment
    #: of the same bytes must not erase it; the checker is a model, not a
    #: deterministic measurement. A later result that actually saw page/console
    #: errors still wins.
    visual_passes: set[str] = set()
    reasons, outdated = [], check_baseline.superseded(record)  # tests the task itself contradicts
    if (
        _explicit_test_change_required(record)
        and not any(_is_test_path(path) for path in record.changed_files)
    ):
        reasons.append("explicit test change requested but no test file changed")
    ban = _test_edit_ban(record)
    if ban:
        touched = _changed_existing_tests(core, record)
        if touched:
            quoted = ban if len(ban) <= 120 else ban[:117] + "..."
            reasons.append(
                f'existing test files changed although the task says "{quoted}": '
                f"{', '.join(touched[:4])} (restore their original content)"
            )
    else:
        from . import original_tests

        try:
            hidden = original_tests.regressions(core, record)
        except Exception:  # noqa: BLE001 - a comparison that cannot run proves nothing
            hidden = None
        open_cases = original_tests.undeclared(hidden or [], outdated)
        if open_cases:
            reasons.append(original_tests.reason(open_cases))
    #: The most recent successful ``git.diff`` is the review the run is
    #: handing over. It counts only when the trusted Core recorded a complete
    #: scope against the session's own start revision (see
    #: ``_complete_change_review``): a path-filtered, staged-only or
    #: unstaged-only diff carries the current identity too, but it did not see
    #: the whole change, and a ``HEAD``-relative diff no longer names the span
    #: the run began with once an elevated commit moved HEAD.
    review: Optional[dict[str, Any]] = None
    for entry in record.provider_history:
        if entry.get("core_name") != "git.diff":
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        data = result.get("data")
        if isinstance(data, dict):
            review = data
    if (
        review is None
        or review.get("completion_identity") != current
        or not _complete_change_review(record, review)
        or _review_skips_a_change(core, record, review)
    ):
        reasons.append("missing current change review")
        if (start := getattr(record, "git_baseline", None)) and isinstance(start, dict) and start.get("reason") == git_baseline.REASON_NOT_A_REPOSITORY:
            reasons.append(NO_PRODUCER + "current change review")  # no start revision: a repair loop (4 min, aquarium 11-12) can never review
    for entry in record.provider_history:
        result = entry.get("result")
        if entry.get("role") != "tool" or not isinstance(result, dict):
            continue
        if result.get("requested_by") not in (None, "harness_final"):
            continue
        data = result.get("data") or {}
        name = entry.get("core_name")
        if data.get("changed") is True and result.get("ok"):
            checks = data.get("quick_checks")
            # An atomic patch reports one digest per written path in
            # ``post_state`` and none in ``content_sha256``. Reading only the
            # single-file fields left every patched file without "current
            # structural evidence", even after a green compile. On
            # swe-sympy-24213 the model cleaned its scratch code out of si.py
            # with apply_patch and the correct patch ended verification_failed.
            digests: dict[str, str] = {}
            post_state = data.get("post_state")
            if isinstance(post_state, dict):
                digests.update(
                    {
                        str(relative): str(digest)
                        for relative, digest in post_state.items()
                        if isinstance(digest, str)
                    }
                )
            path = data.get("path")
            digest = data.get("content_sha256") or data.get("sha256")
            if isinstance(path, str) and isinstance(digest, str):
                digests.setdefault(path, digest)
            for relative, current_digest in digests.items():
                if (
                    relative in current["files"]
                    and current_digest == current["files"][relative]
                    and isinstance(checks, list)
                ):
                    structural[relative] = bool(checks) and all(
                        c.get("ok") is True
                        and not str(c.get("summary", "")).startswith("skipped")
                        for c in checks
                    )
        if name not in {"checks.run", "visual.check"}:
            continue
        if name == "checks.run" and _is_environment_setup(data.get("argv") or []):
            # Installing tools is not a claim about the change. In an offline
            # sandbox it fails by design (swer5-k3 cbi: pip install flake8 ...)
            # and must not stay owed as a "failed check" on a correct patch.
            continue
        if name == "checks.run" and (data.get("tool_unavailable") or data.get("timed_out")):
            continue  # a tool the image lacks, or a cut-off run: no evidence either way (HARD12 gha4)
        if name == "checks.run" and _pytest_ran_nothing(data):
            # pytest exit 4 (usage error) / 5 (no tests collected) ran no test at
            # all: neither proof nor a failure of the change. A suite that the
            # image cannot import skips itself (swer5-k7 sentry: aws_lambda
            # without boto3, exit 5) and must not stay owed on a correct patch.
            continue
        if name == "checks.run" and data.get("verification_eligible") is True:
            # A failure on ANY state stays owed until the same argv passes on the
            # current state. An edit, a wider read set or another green check cannot
            # discharge it, and neither can a pass on older bytes.
            ran = tuple(data.get("argv") or [])
            ok = result.get("ok") is True and data.get("exit_code") == 0 and not data.get("timed_out")
            # Pre-existing failures only (check_baseline) are not owed; neither is
            # a red `python -c` diagnostic snippet, which is never re-run verbatim.
            ok = ok or data.get("preexisting_only") is True or check_baseline.only_superseded(data, outdated)
            if not ok and _inline_probe(ran):
                pass
            elif not (ok and ("browser" not in command_classes(ran) or browser_proof(data))):
                failed_ever[ran] = None
        proof = data.get("completion_identity")
        if proof != current or not proof:
            continue
        evidence = result.get("evidence") or []
        if not evidence or not all(
            e.get("evidence_id") in persisted
            and persisted[e["evidence_id"]].get("metadata", {}).get("completion_identity") == current
            for e in evidence
        ):
            continue
        if name == "visual.check":
            target = str(data.get("completion_target", ""))
            if is_scratch_page(target) or Path(target).as_posix() in harness_pages:
                # A failed probe page is diagnostic, not a failed deliverable.
                # It must not stay owed, and a pass on it must not count either.
                continue
            verdict = data.get("verdict")
            if verdict not in {"pass", "needs_work"}:
                # `unavailable`/`limit` is the budget speaking, not a judgment
                # about the page. It can leave visual evidence missing, but it
                # is not an active failed check.
                continue
            errors = bool(data.get("page_errors") or data.get("console_errors"))
            passed = verdict == "pass" and not errors
            visual_key = (name, target)
            if passed:
                visual_passes.add(target)
                latest[visual_key] = (True, {"visual"})
            elif errors or target not in visual_passes:
                latest[visual_key] = (False, {"visual"})
            continue
        if data.get("verification_eligible") is not True:
            continue
        argv = tuple(data.get("argv") or [])
        # Core-approved checks retain generic authority, not specialized proof.
        classes = command_classes(argv) or {"behavioral"}
        passed = result.get("ok") is True and data.get("exit_code") == 0 and not data.get("timed_out")
        passed = passed or data.get("preexisting_only") is True or check_baseline.only_superseded(data, outdated)
        if "browser" in classes:
            passed = passed and browser_proof(data)
        if not passed and _inline_probe(argv):
            continue  # diagnostic snippet: neither owed nor proof when red
        latest[argv] = (passed, classes)
    for argv, (passed, _) in latest.items():
        if not passed:
            reasons.append("active failed check: " + " ".join(argv))
    for argv in failed_ever:
        if argv not in latest and not _moot_scratch_check(core, argv):
            reasons.append("unresolved failed check: " + " ".join(argv))
    passed_classes = set().union(*(classes for passed, classes in latest.values() if passed)) if latest else set()
    # A vision-passed visual.check on the changed page is browser-runtime proof:
    # it loaded the page in Chromium, recorded console/page errors, measured the
    # viewport resize and frame motion, and a model judged the frames. For a
    # static page that is the same fact `karox.web_check --render` reports, so
    # the obligation is discharged instead of ending a correct run unverified.
    visual_pages: set[str] = set()
    for key, (passed, classes) in latest.items():
        # Check keys are argv tuples of any length; only the (name, target)
        # shape belongs to visual.check.
        if (
            len(key) == 2
            and key[0] == "visual.check"
            and passed
            and "visual" in classes
            and Path(key[1]).as_posix() not in harness_pages
            and (
                key[1] in record.changed_files
                or _page_loads_the_change(core, key[1], record.changed_files)
            )
        ):
            visual_pages.add(key[1])
    if browser and visual_pages:
        passed_classes.add("browser")
    if visual_required(record) and not visual_proven(core, record):
        reasons.append("missing visual evidence")
        if visual_producer_available(core) is False:
            reasons.append(NO_PRODUCER + "visual evidence")
    commands = core.verification_command_order or sorted(core.verification_commands)
    # What Core could produce at all. Prefix grants are permissions, not producers.
    producible: set[str] = set()
    for argv in commands:
        if "*" not in argv:
            producible |= command_classes(argv) or {"behavioral"}
        elif list(argv[-1:]) == ["*"] and len(argv) == 2 and Path(argv[0]).stem.lower().startswith("python"):
            # `python *` lets the model run `python -m pkg --help` itself, so
            # runtime proof is producible; the obligation stays, but the run
            # asks for it instead of stopping as if no approval could supply it.
            producible |= {"runtime", "behavioral"}
    # An explicitly advertised visual.check on the actual changed page is an
    # approved browser-proof *producer*. The old floor looked only at shell
    # verification commands, called the browser duty impossible, and aborted
    # before the finishing visual.check could run. Do not count it as a PASS:
    # only the trusted tool's subsequent current-byte evidence can do that.
    if browser and _approved_visual_producer_for_change(
        core, record, pages, harness_pages
    ):
        producible.update({"browser", "behavioral"})
    if not docs:
        for argv in commands:
            if "*" in argv:
                continue  # Prefix grants are permissions, not an obligation to run every variant.
            classes = command_classes(argv)
            if classes & {"tests", "structural"} and tuple(argv) not in latest:
                reasons.append("missing current check: " + " ".join(argv))
        if "structural" not in passed_classes:
            for relative in record.changed_files:
                if not _deliverable(relative, harness_pages):
                    # A diagnostic file is not a deliverable and owes no
                    # structural proof of its own.
                    continue
                path = core.safe_path(relative)
                if path.is_file() and _select_checks(path) and not structural.get(relative):
                    reasons.append("missing current structural evidence: " + relative)
    behavior_classes = {"behavioral", "tests", "runtime", "browser"}
    if behavioral and not passed_classes & behavior_classes:
        reasons.append("missing behavioral evidence")
        if not producible & behavior_classes:
            reasons.append(NO_PRODUCER + "behavioral evidence")
    # An obligation with no approved producer stays owed: the honest outcome is
    # UNVERIFIED. The remedy is approving a producer, never deleting the duty.
    # Naming the absent producer lets the run stop and say so instead of asking
    # the model to repair something only a new approval can supply.
    needed = {"browser"} if browser else {"runtime"}
    if runtime and not passed_classes & needed:
        owed = "browser runtime evidence" if browser else "runtime smoke evidence"
        reasons.append("missing " + owed)
        if not producible & needed:
            reasons.append(NO_PRODUCER + owed)
    if coverage_required(core, record):
        # Last, so the repair turn reads the failed checks first: a fix
        # changes the bytes, and every attestation quote must then be renewed.
        reasons.extend(
            requirement_coverage.floor_reasons(
                record,
                diagnostic=harness_pages,
                gating_only=not requirement_coverage.ATTESTATION_GATES_VERIFICATION,
            )
        )
    return list(dict.fromkeys(reasons))


_SETUP_SUBCOMMANDS = {"install", "download", "uninstall"}


def _is_environment_setup(argv: Sequence[str]) -> bool:
    """`python -m pip install ...` / `pip install ...`: provisioning, not proof."""
    parts = [str(p) for p in argv]
    if not parts:
        return False
    exe = Path(parts[0]).stem.lower()
    if exe.startswith("python") and len(parts) > 3 and parts[1] == "-m" and parts[2] in {"pip", "pip3"}:
        return parts[3] in _SETUP_SUBCOMMANDS
    if exe in {"pip", "pip3"} and len(parts) > 1:
        return parts[1] in _SETUP_SUBCOMMANDS
    return False


_MAIN_GUARD = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)


def _is_runtime_entry(core: Any, relative: str) -> bool:
    """Whether a changed file is a surface someone launches, not a library module.

    ``__main__.py``, ``package.json`` and anything under ``bin/`` always are. A
    conventional entry name (main.py, app.py, server.py, cli.py) at the repository
    root, or one that carries a ``__main__`` guard, is too. The same name inside an
    importable package without a guard is a library module (``ninja/main.py``
    holds the NinjaAPI class): demanding a launch smoke for it would end a correct,
    tested change as UNVERIFIED (swer5-k3). Unreadable files fail closed.
    """
    path = Path(relative)
    name = path.name
    if "bin" in path.parts or name in {"__main__.py", "package.json"}:
        return True
    if name not in _RUNTIME:
        return False
    if len(path.parts) == 1:
        return True
    try:
        target = core.safe_path(relative)
        if not target.is_file():
            return False  # deleted: nothing left to launch
        if not (target.parent / "__init__.py").is_file():
            return True  # a loose script directory, not a package
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True
    return bool(_MAIN_GUARD.search(text))


def _inline_probe(argv: Sequence[str]) -> bool:
    """``python -c CODE``: an inline snippet, never a deliverable check."""
    parts = [str(p) for p in argv]
    return len(parts) >= 3 and Path(parts[0]).stem.lower().startswith("python") and parts[1] == "-c"


def _pytest_ran_nothing(data: dict[str, Any]) -> bool:
    """A finished pytest run that executed no test: exit 4 or 5, not timed out."""
    argv = [str(a) for a in (data.get("argv") or [])]
    if data.get("timed_out") or data.get("exit_code") not in (4, 5) or not argv:
        return False
    exe = Path(argv[0]).stem.lower()
    if exe in {"pytest", "py.test"}:
        return True
    return exe.startswith("python") and len(argv) > 2 and argv[1] == "-m" and argv[2] == "pytest"


def _moot_scratch_check(core: Any, argv: Sequence[str]) -> bool:
    """A failed `python <script>` whose script is gone and was never tracked.

    The run wrote a throwaway probe, ran it while the bug was still there, and
    then deleted it as it was told to. That failure can never be re-run, so
    keeping it owed only ends a correct change as UNVERIFIED (swer5-k2 ninja).
    The exemption is deliberately narrow: exactly an interpreter plus one
    repository-relative .py path, the file must be absent now, and Git must not
    know it at HEAD -- deleting a tracked test never discharges its failure.
    """
    parts = list(argv)
    if len(parts) != 2 or not Path(parts[0]).stem.lower().startswith("python"):
        return False
    script = parts[1]
    if not script.endswith(".py") or script.startswith("-"):
        return False
    try:
        path = core.safe_path(script)
    except Exception:
        return False
    if path.exists():
        return False
    try:
        relative = path.relative_to(Path(core.repository).resolve()).as_posix()
        tracked = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{relative}"],
            cwd=str(core.repository), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    return tracked.returncode != 0


def only_attestation(reasons: Sequence[str]) -> bool:
    """True when every remaining gap is the requirement attestation itself.

    Checks, structure and runtime proof are all present; what is missing is
    the model's own quote per requirement. Once the bounded refusals are spent
    a repair turn only re-sends the checklist, so the run ends UNVERIFIED and
    names the open requirements instead.
    """
    return bool(reasons) and all(
        r.startswith("requirement ") or r.startswith("missing requirement attestation")
        for r in reasons
    )


def only_unproducible(reasons: Sequence[str]) -> bool:
    """True when every remaining gap is proof no approved command can produce.

    A repair turn cannot discharge such a gap, and Core refuses any substitute
    command, so the run should end UNVERIFIED with the reason rather than loop.
    """
    owed = {r[len(NO_PRODUCER):] for r in reasons if r.startswith(NO_PRODUCER)}
    if not owed:
        return False
    allowed = {NO_PRODUCER + item for item in owed} | {"missing " + item for item in owed}
    return all(r in allowed for r in reasons)