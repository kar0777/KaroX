"""Deterministic completion floor; no provider calls or new persistent state.

Uses the session's changed/read paths and existing evidence metadata. Unsupported
validators are not invented. Unknown proof is missing proof, never success.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Optional, Sequence

from .auto_verify import _select_checks
from .requirements import ledger_digest
from .web_check import default_pages, is_scratch_page

_TEXT = {".md", ".rst"}
# Prefix of a floor reason naming an owed proof class Core has no command for.
NO_PRODUCER = "no approved producer for "
# Markdown is only documentation where documentation lives. A .md inside a code
# tree can be runtime data: KaroX itself loads prompts/*.md and SKILL.md.
_DOC_DIRS = {"docs", "doc", "documentation"}
_ROOT_DOC_PREFIXES = (
    "readme", "changelog", "contributing", "license", "security",
    "support", "code_of_conduct", "quickstart", "release_notes",
)
_WEB = {".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".tsx", ".css"}
# What a visual requirement can be about: the page and everything that styles or
# renders it. A Python change is never graded by a picture.
_VISUAL_ASSETS = _WEB | {".scss", ".sass", ".less", ".vue", ".svelte", ".svg"}
_RUNTIME = {"__main__.py", "main.py", "app.py", "server.py", "cli.py", "package.json"}
# Assets a page loads by relative path. A canvas game often edits only
# ``game.js`` and leaves the scaffold ``index.html`` untouched; the visual check
# still rendered that page, so its bytes are the proof surface.
_PAGE_ASSETS = {".js", ".mjs", ".cjs", ".css"}
_BEHAVIOR = re.compile(r"\b(?:implement|bugfix|refactor|runtime|behavior|behaviour|execute|launch)\b|исправ.*ошиб|поведен|запуск", re.I)
_VISUAL = re.compile(
    r"\b(?:appearance|aesthetic|visually|visible|colour|color|layout|animation|animate|typography)\b"
    r"|\b(?:heading|background|button|text)\b.{0,40}\b(?:blue|red|green|aligned|centered)\b"
    r"|визуаль|внешн.*вид|выгляд|цвет|анимац|макет|видим", re.I
)
_NEGATED_VISUAL = re.compile(r"(?:without|do not|don't|no|не|без)\s+(?:changing\s+|change\s+|изменения\s+)?(?:the\s+)?(?:appearance|layout|visual\w*|внешн\w*|визуаль\w*)", re.I)
_WEB_INTENT = re.compile(
    r"(?:\.html?\b|\b(?:frontend|web\s*page|website|browser|three\.js|canvas|webgl)\b"
    r"|(?:веб|браузер|страниц\w*|three\.js))",
    re.I,
)
_VISUAL_DIRECT = re.compile(
    r"\b(?:visible|visibly|visual|look|looks|realistic|recognizable|polished|"
    r"color|colour|gradient|transparent|opacity|lighting|illuminated|shadow|"
    r"camera|screenshot|perspective|highlight|glossy|calming|cinematic)\w*\b"
    r"|визуаль|видим|выгляд|реалист|цвет|свет|тень|камер",
    re.I,
)
_VISUAL_OBJECT = re.compile(
    r"\b(?:scene|aquarium|tank|glass|water|clownfish|fish|body|tail|fin|eye|pupil|"
    r"mouth|band|stripe|marking|bubble|gravel|stone|plant|blade|frame|panel|"
    r"surface|ripple|background)\w*\b"
    r"|аквариум|рыб|вода|стекл|пузыр|растен|камн|фон|полос|плавник|глаз",
    re.I,
)
_VISUAL_OBSERVABLE = re.compile(
    r"\b(?:visible|visibly|clearly|recognizable|realistic|polished|look|looks|"
    r"silhouette|color|colour|orange|white|black|blue|green|transparent|opacity|"
    r"glossy|highlight|rounded|natural|cinematic|balanced|readable|perspective|"
    r"screenshot|lit|lighting|shadow)\w*\b"
    r"|визуаль|видим|выгляд|реалист|цвет|прозрач|свет|тень|полос",
    re.I,
)
_VISUAL_CONTRACT_ONLY = re.compile(
    r"\b(?:procedurally|constructed\s+from|implemented\s+using|module\s+imports?|"
    r"object\s+pooling|delta[- ]?time|requestanimationframe|reusable\s+(?:functions?|classes?)|"
    r"single\s+html|self-contained|shader|geometry|class|function)\b",
    re.I,
)
_VISUAL_CODE_LITERAL = re.compile(
    r"(?:\brenderer\.[A-Za-z_]|\bTHREE\.[A-Za-z_]|setPixelRatio\s*\(|"
    r"shadowMap\.[A-Za-z_]|toneMapping\s*=)",
    re.I,
)
_VISUAL_IMPLEMENTATION = re.compile(
    r"\b(?:geometry|shader|class|function|script|module|import|dependency|"
    r"delta[- ]?time|object pooling|requestanimationframe|resize|console error)\b",
    re.I,
)
_VISUAL_REVIEW_LIMIT = 40

_TEST_CHANGE_REQUEST = re.compile(
    r"\b(?:add|write|create|update|modify|change|extend|adjust|revise)\w*\b"
    r".{0,80}(?<![-_.])\b(?:tests?|test\s+cases?|specs?)\b"
    r"|(?:добав\w*|напиш\w*|созда\w*|обнов\w*|измен\w*|расшир\w*|допол\w*)"
    r".{0,80}(?:тест\w*|спек\w*)",
    re.I,
)
_NEGATED_TEST_CHANGE = re.compile(
    r"(?:\b(?:(?:do|does|should|must|may|will|would|can|could)\s+not|"
    r"can't|cannot|don't|dont|never|without|avoid)\b"
    r".{0,45}\b(?:add(?:ing)?|writ(?:e|ing)|creat(?:e|ing)|updat(?:e|ing)|"
    r"modify(?:ing)?|chang(?:e|ing)|extend(?:ing)?)\w*\b"
    r".{0,45}\b(?:tests?|test\s+cases?|specs?)\b)"
    r"|(?:(?:\bне\b|\bбез\b).{0,45}"
    r"(?:добав\w*|пис\w*|созда\w*|обнов\w*|измен\w*|меня\w*|расшир\w*|допол\w*)"
    r".{0,45}(?:тест\w*|спек\w*))",
    re.I,
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+")
# A scope instruction puts tests out of bounds ("changes go in the source
# tree, not in the test files"). The negation follows the change verb, so the
# prefix rules above cannot see it; the whole sentence is the unit of intent.
_TESTS_OUT_OF_SCOPE = re.compile(
    r"(?:\b(?:not|never)\b\s+(?:(?:in|to|into|inside|within)\s+)?"
    r"(?:the\s+)?(?:tests?|test\s+files?|specs?)\b"
    r"|(?:rather|instead)\s+(?:than|of)\s+(?:in\s+|inside\s+|within\s+)?"
    r"(?:the\s+)?(?:tests?|test\s+files?|specs?)\b"
    r"|outside\s+(?:of\s+)?(?:the\s+)?(?:tests?|test\s+files?)\b)",
    re.I,
)
_TEST_EXECUTION_IN_MATCH = re.compile(
    r"\b(?:run|execute|rerun|re-run)\w*\b|(?:запуст\w*|выполн\w*)",
    re.I,
)


def _active_requirement_texts(record: Any) -> list[str]:
    entries = [
        entry
        for entry in record.requirements
        if isinstance(entry, dict) and entry.get("status", "active") == "active"
    ]
    texts = [
        str(entry.get("text", "")).strip()
        for entry in entries
        if str(entry.get("text", "")).strip()
    ]
    return texts or [str(record.task)]


def _explicit_test_change_required(record: Any) -> bool:
    for text in _active_requirement_texts(record):
        # Judge one sentence at a time. A scope sentence must not borrow the
        # word "tests" from the next sentence, and a negated sentence must not
        # swallow a following positive request.
        for sentence in _SENTENCE_BREAK.split(text):
            if _TESTS_OUT_OF_SCOPE.search(sentence):
                continue
            cleaned = _NEGATED_TEST_CHANGE.sub("", sentence)
            for match in _TEST_CHANGE_REQUEST.finditer(cleaned):
                # "change code, then run tests" is verification work, not a request
                # to edit tests. The broad matcher deliberately understands varied
                # phrasing, so reject matches whose path to the test noun crosses
                # an execution verb.
                if _TEST_EXECUTION_IN_MATCH.search(match.group(0)):
                    continue
                return True
    return False


def _is_test_path(relative: str) -> bool:
    path = Path(relative)
    parts = {part.lower() for part in path.parts[:-1]}
    if parts & {"test", "tests", "spec", "specs", "__tests__"}:
        return True
    name = path.name.lower()
    stem = path.stem.lower()
    return (
        name.startswith(("test_", "test-", "spec_", "spec-"))
        or stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
    )


def is_doc(relative: str) -> bool:
    path = Path(relative)
    if path.suffix.lower() not in _TEXT:
        return False
    parts = path.parts
    if parts and parts[0].lower() in _DOC_DIRS:
        return True
    if len(parts) != 1:
        return False
    stem = path.stem.lower()
    # Root markdown is not automatically documentation. Agent instructions,
    # prompt resources and skill files often live there too and affect runtime
    # behavior. Only conventional human-facing documentation names get the
    # cheap docs-only path.
    return any(stem.startswith(prefix) for prefix in _ROOT_DOC_PREFIXES)


def docs_only(record: Any) -> bool:
    return bool(record.changed_files) and all(is_doc(p) for p in record.changed_files) and not _BEHAVIOR.search(record.task)


def visual_required(record: Any) -> bool:
    # A picture can only grade a web deliverable: the page, or a stylesheet or
    # script that renders into one. Visual words on a Python change demand nothing.
    if not any(Path(p).suffix.lower() in _VISUAL_ASSETS for p in record.changed_files):
        return False
    return visual_intent(record)


def visual_intent(record: Any) -> bool:
    """Whether the admitted task contract contains an affirmative visual goal."""

    return any(
        _VISUAL.search(_NEGATED_VISUAL.sub("", text))
        for text in _active_requirement_texts(record)
    )


def web_render_intent(record: Any) -> bool:
    """Whether a render-capable web producer is predictably needed up front.

    Unlike visual_required, this is usable before the deliverable exists. It lets
    the CLI close the verification producer set before spending model calls on a
    page it would later be unable to verify.
    """

    texts = _active_requirement_texts(record)
    return visual_intent(record) and any(_WEB_INTENT.search(text) for text in texts)


def visual_review_entries(record: Any, limit: int = _VISUAL_REVIEW_LIMIT) -> list[dict[str, Any]]:
    """Bound the screenshot checklist to requirements a frame can actually judge.

    The durable ledger stays lossless. This projection is intentionally smaller:
    sending 100+ implementation requirements to a vision turn is expensive and
    makes the grader spend output on "NOT VISIBLE" instead of defects the image
    can prove. High-signal appearance requirements win; implementation-only
    requirements are omitted unless they also state a visible outcome.
    """

    direct_entries: list[tuple[int, dict[str, Any]]] = []
    source_candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(record.requirements):
        if not isinstance(entry, dict) or entry.get("status", "active") != "active":
            continue
        text = str(entry.get("text", "")).strip()
        if not text or set(text) <= {"=", "-", "_", " "}:
            continue

        source = str(entry.get("source", ""))
        if not source.startswith("file:"):
            # Preserve the pre-existing fail-closed contract for direct user and
            # continuation requirements: a nonvisual direct requirement still
            # gets NOT VISIBLE rather than silently disappearing. Only the
            # explicit "read this spec" wrapper is metadata, not a screenshot ask.
            if not entry.get("delegates_source"):
                direct_entries.append((index, entry))
            continue

        if _VISUAL_CODE_LITERAL.search(text):
            continue
        cleaned = _NEGATED_VISUAL.sub("", text)
        direct = bool(_VISUAL_DIRECT.search(text) or _VISUAL.search(cleaned))
        observable = bool(_VISUAL_OBSERVABLE.search(text))
        objects = len(_VISUAL_OBJECT.findall(text))
        if _VISUAL_CONTRACT_ONLY.search(text) and not observable:
            # "Generate procedurally", "use shader X", "single HTML", etc. are
            # source-code obligations. A screenshot cannot prove them even when
            # the sentence happens to name fish/plants/animation.
            continue
        if not direct and not observable and not objects:
            continue
        score = (
            (7 if observable else 0)
            + (3 if direct else 0)
            + min(objects, 4) * 2
        )
        # Short atomic visible nouns ("Tail fin", "Two eyes") are useful checker
        # criteria even when the parent heading carried the word "visible".
        if objects and len(text) <= 80:
            score += 3
        # Quantified scene contents are high-value acceptance facts: they catch
        # missing fish, sparse gravel/plants and absent bubble streams.
        if objects and re.search(
            r"\b(?:exactly|at\s+least|approximately|one|two|three|four|five|\d+)\b",
            text,
            re.I,
        ):
            score += 2
        if _VISUAL_IMPLEMENTATION.search(text) and not observable:
            score -= 5
        if text.endswith(":"):
            score -= 2
        if score <= 0:
            continue
        source_candidates.append((score, index, entry))

    source_limit = max(1, int(limit))
    if len(source_candidates) > source_limit:
        source_candidates = sorted(
            source_candidates, key=lambda item: (-item[0], item[1])
        )[:source_limit]
    selected = direct_entries + [
        (index, entry) for _, index, entry in source_candidates
    ]
    return [entry for _, entry in sorted(selected, key=lambda item: item[0])]


def visual_review_listing(record: Any, limit: int = _VISUAL_REVIEW_LIMIT) -> str:
    return "\n".join(
        f"{entry.get('id', '')}. {entry.get('text', '')}"
        for entry in visual_review_entries(record, limit)
    )


def visual_producer_available(core: Any) -> Optional[bool]:
    """Whether this runtime exposes a semantic visual producer.

    None means a lightweight/test core does not expose a tool catalogue, so
    callers preserve the historical unknown behavior. A real CoreRuntime
    returns a definite answer: visual.check is advertised only when the active
    model can actually inspect images.
    """

    tools = getattr(core, "tools", None)
    if not callable(tools):
        return None
    try:
        return any(getattr(item, "name", None) == "visual.check" for item in tools())
    except Exception:
        return None


def _page_loads_the_change(core: Any, page: str, changed_files: Sequence[str]) -> bool:
    """Whether an unchanged page is still the surface that renders the change.

    The completion gate used to accept a visual pass only when the checked page
    itself was in ``changed_files``. A scaffold ``index.html`` that loads an
    edited ``game.js`` never qualified, so a passing web game ended as
    ``unproducible_requirement`` even though the checker had rendered the page
    and the current bytes. Require the page to reference at least one changed
    stylesheet/script in its own directory: that is the page the change is for,
    not an unrelated page elsewhere in the repository. A sibling scratch probe
    (``probe_snake.cjs``) is not loaded by the page and must not block the
    proof.
    """

    if is_scratch_page(page):
        return False
    if page in changed_files:
        return True
    page_path = Path(page)
    assets = [
        item
        for item in changed_files
        if Path(item).suffix.lower() in _PAGE_ASSETS
        and Path(item).parent == page_path.parent
    ]
    if not assets:
        return False
    try:
        text = core.safe_path(page).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return any(Path(item).name in text for item in assets)


def visual_proven(core: Any, record: Any) -> bool:
    """Whether the current bytes have a clean visual verdict.

    Both visual paths count: the legacy in-agent render review and the isolated
    visual.check checker. The latter keeps frame bytes out of the main transcript,
    so visual tasks can improve quality without making every later model request
    pay for screenshots again.
    """

    current = identity(core, record)
    if not current:
        return False

    for evidence in reversed(record.evidence):
        if not isinstance(evidence, dict) or evidence.get("kind") != "render_review":
            continue
        metadata = evidence.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if (
            metadata.get("clean") is True
            and metadata.get("completion_identity") == current
        ):
            return True

    persisted = {
        item.get("evidence_id"): item
        for item in record.evidence
        if isinstance(item, dict) and item.get("evidence_id")
    }
    for entry in reversed(record.provider_history):
        if entry.get("core_name") != "visual.check":
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        if result.get("requested_by") not in (None, "harness_final"):
            continue
        data = result.get("data")
        if not isinstance(data, dict) or data.get("completion_identity") != current:
            continue
        target = str(data.get("completion_target") or "")
        if not target or not _page_loads_the_change(
            core, target, record.changed_files
        ):
            # A pass on a scratch probe or an unrelated page is not evidence
            # about the deliverable, even when its bytes are current.
            continue
        attached = result.get("evidence") or []
        if not attached or not all(
            item.get("evidence_id") in persisted
            and persisted[item["evidence_id"]].get("metadata", {}).get(
                "completion_identity"
            )
            == current
            for item in attached
            if isinstance(item, dict)
        ):
            continue
        return bool(
            data.get("verdict") == "pass"
            and data.get("method") == "vision"
            and not data.get("page_errors")
            and not data.get("console_errors")
        )
    return False


def identity(core: Any, record: Any) -> dict[str, Any]:
    """Hash only known task inputs (reads + changes), not generated tree noise.

    Missing files have a stable deletion marker. Unreadable/outside paths fail
    closed. Checks also compare before/after identities to reject concurrent edits.
    """
    paths = set(record.changed_files)
    for requirement in record.requirements:
        if (
            not isinstance(requirement, dict)
            or requirement.get("status", "active") != "active"
        ):
            continue
        source_path = requirement.get("source_path")
        if isinstance(source_path, str) and source_path:
            paths.add(source_path)
    for entry in record.provider_history:
        if entry.get("core_name") in {"repo.read_file", "repo.read_lines"}:
            result = entry.get("result") or {}
            path = (result.get("data") or {}).get("path")
            if result.get("ok") and isinstance(path, str):
                paths.add(path)
    files = {}
    for relative in sorted(paths):
        try:
            path = core.safe_path(relative)
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        except Exception:
            return {}
    return {"files": files, "requirements": ledger_digest(record.requirements), "task": record.task}


def command_classes(argv: Sequence[str]) -> set[str]:
    """Exact runner positions, never a substring of arbitrary python -c text."""
    parts = list(argv)
    if not parts:
        return set()
    exe = Path(parts[0]).stem.lower()
    if exe == "git" and parts[1:] == ["diff", "--check"]:
        return {"structural"}
    if exe.startswith("python") and len(parts) > 2 and parts[1] == "-m":
        module = parts[2]
        if module in {"pytest", "unittest"}:
            return {"tests"}
        if module in {"ruff", "mypy", "py_compile", "compileall", "build"}:
            return {"structural"}
        if module == "karox.web_check":
            return {"structural", "browser"} if "--render" in parts else {"structural"}
    if "--help" in parts and "-c" not in parts:
        return {"runtime"}
    if exe in {"pytest"}:
        return {"tests"}
    if exe in {"ruff", "mypy", "tsc"} or (exe == "node" and "--check" in parts):
        return {"structural"}
    if exe in {"npm", "pnpm", "yarn"}:
        script = parts[2] if len(parts) > 2 and parts[1] == "run" else parts[1] if len(parts) > 1 else ""
        if script in {"test:smoke", "test:e2e"}:
            return {"tests", "runtime"}
        if script == "test":
            return {"tests"}
        if script in {"build", "lint", "typecheck", "type-check", "check:types"}:
            return {"structural"}
    if exe in {"cargo", "go"} and len(parts) > 1:
        if parts[1] == "test":
            return {"tests"}
        if parts[1] in {"build", "check", "vet"}:
            return {"structural"}
    return set()


def browser_proof(data: dict[str, Any]) -> bool:
    # An exact same-bytes/same-argv PASS may be replayed from persisted evidence
    # without rerunning the browser process. The cache writer sets this bit only
    # after the original stdout itself passed this function, so the compact replay
    # keeps proof semantics without copying a large render payload into history.
    if (
        data.get("proof_cache_hit") is True
        and data.get("cached_browser_proof") is True
    ):
        return True
    try:
        payload = json.loads(data.get("stdout", ""))
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("vacuous"):
        return False
    renders = payload.get("render")
    if not isinstance(renders, dict) or not renders:
        return False
    return all(
        isinstance(r, dict) and not any(r.get(k) for k in ("skipped", "error", "page_errors", "console_errors", "problems"))
        and r.get("sampled", 0) > 0
        for r in renders.values()
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
    browser = (
        behavioral
        and bool(pages)
        and any(Path(p).suffix.lower() in _VISUAL_ASSETS for p in record.changed_files)
    )
    runtime = behavioral and (
        browser or any(_is_runtime_entry(core, p) for p in record.changed_files)
    )
    persisted = {e.get("evidence_id"): e for e in record.evidence if isinstance(e, dict)}
    latest: dict[tuple[str, ...], tuple[bool, set[str]]] = {}
    failed_ever: dict[tuple[str, ...], None] = {}
    structural: dict[str, bool] = {}
    reasons = []
    if (
        _explicit_test_change_required(record)
        and not any(_is_test_path(path) for path in record.changed_files)
    ):
        reasons.append("explicit test change requested but no test file changed")
    reviews = [e.get("result", {}).get("data", {}).get("completion_identity")
               for e in record.provider_history if e.get("core_name") == "git.diff"
               and isinstance(e.get("result"), dict) and e["result"].get("ok")]
    if not reviews or reviews[-1] != current:
        reasons.append("missing current change review")
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
            if not (ok and ("browser" not in command_classes(ran) or browser_proof(data))):
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
            if is_scratch_page(target):
                # A failed probe page is diagnostic, not a failed deliverable.
                # It must not stay owed, and a pass on it must not count either.
                continue
            latest[(name, target)] = (
                data.get("verdict") == "pass" and not data.get("page_errors") and not data.get("console_errors"),
                {"visual"},
            )
            continue
        if data.get("verification_eligible") is not True:
            continue
        argv = tuple(data.get("argv") or [])
        # Core-approved checks retain generic authority, not specialized proof.
        classes = command_classes(argv) or {"behavioral"}
        passed = result.get("ok") is True and data.get("exit_code") == 0 and not data.get("timed_out")
        if "browser" in classes:
            passed = passed and browser_proof(data)
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
    if not docs:
        for argv in commands:
            if "*" in argv:
                continue  # Prefix grants are permissions, not an obligation to run every variant.
            classes = command_classes(argv)
            if classes & {"tests", "structural"} and tuple(argv) not in latest:
                reasons.append("missing current check: " + " ".join(argv))
        if "structural" not in passed_classes:
            for relative in record.changed_files:
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
