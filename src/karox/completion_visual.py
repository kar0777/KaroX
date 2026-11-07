"""Visual/web proof: intent detection, review listing, render proof and change loading."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Collection, Optional, Sequence
from .web_check import is_scratch_page

from .completion_rules import (
    _active_requirement_texts,
    diagnostic_paths,
    identity,
)


_WEB = {".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".tsx", ".css"}

_VISUAL_ASSETS = _WEB | {".scss", ".sass", ".less", ".vue", ".svelte", ".svg"}

_PAGE_ASSETS = {".js", ".mjs", ".cjs", ".css"}

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

def _page_loads_the_change(
    core: Any,
    page: str,
    changed_files: Sequence[str],
    diagnostic: Collection[str] = frozenset(),
) -> bool:
    """Whether an unchanged page is still the surface that renders the change.

    The completion gate used to accept a visual pass only when the checked page
    itself was in ``changed_files``. A scaffold ``index.html`` that loads an
    edited ``game.js`` never qualified, so a passing web game ended as
    ``unproducible_requirement`` even though the checker had rendered the page
    and the current bytes. Require the page to reference at least one changed
    stylesheet/script in its own directory: that is the page the change is for,
    not an unrelated page elsewhere in the repository. A sibling scratch probe
    (``probe_snake.cjs``) is not loaded by the page and must not block the
    proof. Nor is the run's own ``diagnostic`` harness page: nothing the user
    opens loads it, so its pass proves nothing about the deliverable.
    """

    if is_scratch_page(page) or Path(page).as_posix() in diagnostic:
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
    diagnostic = diagnostic_paths(core, record)

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
            core, target, record.changed_files, diagnostic
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
        if (
            data.get("verdict") == "pass"
            and data.get("method") == "vision"
            and not data.get("page_errors")
            and not data.get("console_errors")
        ):
            return True
    return False
