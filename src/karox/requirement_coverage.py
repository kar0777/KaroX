"""Requirement coverage: a web deliverable is verified clause by clause.

The completion floor proves that checks passed and that a page rendered. It
cannot tell whether each thing the user asked for was exercised: a run could
pass every check, show a good frame and still ship without a feature the task
named in one sentence of a long bullet. Coverage closes that gap with an
attestation the runtime checks deterministically, with no provider call:

* The active ledger is split into atomic clauses -- bullets, sentences and
  semicolon-separated parts -- each with a stable ID derived from its
  requirement (``R7.3``). Headings, labels that only introduce a list, and
  fragments without content are not clauses.
* ``finish`` carries one entry per clause: ``verified`` with text copied from
  a behavioural tool result (browser, check, test or visual review) produced
  after the last deliverable edit, ``not_applicable`` with a reason, or
  ``unmet``.
* A quote is matched after normalization (case, punctuation and spacing), so
  ``score: 37`` read back from ``{"score":37}`` still matches, while text the
  model never received does not.

The attestation is read from the durable transcript -- the finish call's own
arguments -- so it needs no new session state, survives resume, and an edit
after it makes every quote stale by construction.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .requirements import canonical
from .web_check import is_scratch_artifact

#: The engine's finish tool (``engine_provider_bridge.FINISH_TOOL_NAME``). Kept
#: literal so the floor does not import the provider stack.
FINISH_TOOL = "finish"
#: The finish argument that carries the attestation.
ATTESTATION_FIELD = "requirements"
STATUS_VERIFIED = "verified"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_UNMET = "unmet"
#: More clauses than this and the attestation would cost more than it proves;
#: each requirement is then attested as one item.
MAX_CLAUSES = 60
#: Fewer clauses than this and the request is short enough for the visual
#: review and the checks to judge whole; a checklist would only add a turn.
MIN_CLAUSES = 3
#: Tool results that observed the deliverable's behaviour. Reads, searches and
#: Git output describe source text, not behaviour, and never count.
OBSERVATION_TOOLS = frozenset(
    {"browser.command", "checks.run", "checks.run_affected", "tests.run", "visual.check"}
)
#: The shortest quote that can stand for an observation: two tokens and six
#: characters after normalization. ``true`` alone matches almost anything.
_MIN_QUOTE_TOKENS = 2
_MIN_QUOTE_CHARS = 6
_MIN_REASON_WORDS = 3
_CLAUSE_DISPLAY = 160

_BULLET = re.compile(r"(?:^|(?<=\s))[-*\u2022+]\s+(?=\S)")
_BREAK = re.compile(r"(?<=[.!?;])\s+(?=\S)")
_ABBREVIATION = re.compile(
    r"(?:\b(?:e\.g|i\.e|etc|vs|cf|approx|incl|no|fig|mr|mrs|ms|dr|st|т\.е|т\.д|т\.п|др|см|напр)\.|"
    r"(?:^|\s)[A-Za-zА-Яа-яЁё]\.)$",
    re.IGNORECASE,
)
_CODE_SPAN = re.compile(r"`[^`]*`")
#: Text that must not be split: code spans, and parenthesized asides, whose
#: inner ";" or "." belongs to the clause around them.
_UNSPLIT = re.compile(r"`[^`]*`|\([^()]*\)")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_TOKEN = re.compile(r"[0-9a-zа-яё_]+(?:\.[0-9]+)*")
#: JSON string escapes, possibly nested: a result rendered as JSON shows a
#: newline as ``\n``, and without decoding "...spawn\nPASS" would tokenize as
#: "npass" and miss a quote of "PASS".
_ESCAPE = re.compile(r"\\+(u[0-9a-fA-F]{4}|[nrtbf\"'/\\])")


def _protect_code(text: str) -> Tuple[str, List[str]]:
    spans: List[str] = []

    def keep(match: "re.Match[str]") -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    return _UNSPLIT.sub(keep, text), spans


def _restore_code(text: str, spans: Sequence[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def _sentences(chunk: str) -> List[str]:
    pieces: List[str] = []
    for piece in _BREAK.split(chunk):
        if pieces and _ABBREVIATION.search(pieces[-1]):
            pieces[-1] = f"{pieces[-1]} {piece}"
        else:
            pieces.append(piece)
    return pieces


def _is_clause(text: str) -> bool:
    if not text or _HEADING.match(text):
        return False
    if text.endswith(":"):
        # "Stable test ids:" introduces what follows; the list items are the
        # requirements, the label is not.
        return False
    return len(_WORD.findall(text)) >= 2 or bool(_CODE_SPAN.search(text))


def split_clauses(text: str) -> List[str]:
    """One requirement text as its atomic clauses, in the user's words."""

    protected, spans = _protect_code(canonical(text))
    clauses: List[str] = []
    for chunk in _BULLET.split(protected):
        chunk = chunk.strip()
        if not chunk:
            continue
        for piece in _sentences(chunk):
            restored = canonical(_restore_code(piece, spans)).rstrip(";").rstrip()
            if _is_clause(restored):
                clauses.append(restored)
    return clauses


def clauses(entries: Sequence[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """The attestation checklist: ``(id, text)`` for every active clause.

    A requirement with one clause keeps its own ID; one with several numbers
    them ``R7.1, R7.2, ...`` in document order. The split is a pure function
    of the ledger, so the IDs are stable across steps, resume and repair.
    """

    active = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and str(entry.get("status", "active")) == "active"
        and entry.get("id")
    ]
    split = [(str(entry["id"]), split_clauses(str(entry.get("text", "")))) for entry in active]
    if sum(len(parts) for _, parts in split) > MAX_CLAUSES:
        return [(entry_id, " ".join(parts)) for entry_id, parts in split if parts]
    listed: List[Tuple[str, str]] = []
    for entry_id, parts in split:
        if len(parts) == 1:
            listed.append((entry_id, parts[0]))
            continue
        listed.extend((f"{entry_id}.{index}", part) for index, part in enumerate(parts, start=1))
    return listed


def _unescape(text: str) -> str:
    def decode(match: "re.Match[str]") -> str:
        code = match.group(1)
        if code[0] == "u":
            return chr(int(code[1:], 16))
        return " " if code in "nrtbf" else code

    previous = None
    while previous != text:
        previous, text = text, _ESCAPE.sub(decode, text)
    return text


def normalize(text: str) -> str:
    """Case-, punctuation-, escape- and spacing-free tokens for quote matching."""

    return " ".join(_TOKEN.findall(_unescape(str(text)).lower()))


def parse_attestation(value: Any) -> Dict[str, Dict[str, str]]:
    """The model's attestation as ``{id: {"status", "observed"}}``.

    Lenient about shape -- a list of entries or an ``{id: entry}`` map, with
    ``evidence``/``reason`` accepted for ``observed`` -- and strict about
    content, which :func:`gaps` judges. The last entry for an ID wins.
    """

    items: List[Tuple[str, Any]] = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if isinstance(value, Mapping):
        items = [(str(key), entry) for key, entry in value.items()]
    elif isinstance(value, list):
        items = [(str(entry.get("id", "")), entry) for entry in value if isinstance(entry, Mapping)]
    parsed: Dict[str, Dict[str, str]] = {}
    for key, entry in items:
        key = key.strip().upper()
        if not key:
            continue
        if isinstance(entry, Mapping):
            status = str(entry.get("status") or "").strip().lower().replace("-", "_")
            status = status.replace(" ", "_")
            observed = entry.get("observed") or entry.get("evidence") or entry.get("reason") or ""
        else:
            status, observed = STATUS_VERIFIED, entry
        parsed[key] = {"status": status, "observed": canonical(str(observed))}
    return parsed


def _finish_call_index(history: Sequence[Mapping[str, Any]]) -> Tuple[int, Any]:
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if entry.get("role") != "assistant":
            continue
        for call in reversed(entry.get("tool_calls") or ()):
            if isinstance(call, Mapping) and call.get("name") == FINISH_TOOL:
                raw = call.get("raw_arguments")
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    arguments = {}
                if not isinstance(arguments, Mapping):
                    arguments = {}
                return index, arguments.get(ATTESTATION_FIELD)
    return -1, None


def _changed_paths(data: Mapping[str, Any]) -> List[str]:
    paths: List[str] = []
    post_state = data.get("post_state")
    if isinstance(post_state, Mapping):
        paths.extend(str(path) for path in post_state)
    for key in ("path", "target"):
        if isinstance(data.get(key), str) and data.get(key):
            paths.append(str(data[key]))
    return paths


def last_deliverable_edit(history: Sequence[Mapping[str, Any]]) -> int:
    """Index of the last successful edit of a non-scratch file, or -1.

    A probe page written or deleted after the last real edit does not change
    what the user receives, so it does not make earlier observations stale.
    """

    last = -1
    for index, entry in enumerate(history):
        result = entry.get("result")
        if entry.get("role") != "tool" or not isinstance(result, Mapping):
            continue
        data = result.get("data")
        if not result.get("ok") or not isinstance(data, Mapping) or data.get("changed") is not True:
            continue
        paths = _changed_paths(data)
        if not paths or any(not is_scratch_artifact(path) for path in paths):
            last = index
    return last


def fresh_observations(
    history: Sequence[Mapping[str, Any]], *, before: Optional[int] = None
) -> List[str]:
    """Normalized behavioural results received after the last deliverable edit."""

    end = len(history) if before is None or before < 0 else before
    start = last_deliverable_edit(history[:end]) + 1
    seen: List[str] = []
    for entry in history[start:end]:
        if entry.get("role") != "tool" or entry.get("core_name") not in OBSERVATION_TOOLS:
            continue
        if not isinstance(entry.get("result"), Mapping):
            continue
        text = normalize(str(entry.get("content") or ""))
        if text:
            seen.append(f" {text} ")
    return seen


def _display(text: str) -> str:
    return text if len(text) <= _CLAUSE_DISPLAY else text[: _CLAUSE_DISPLAY - 3] + "..."


def judge(
    record: Any, attestation: Any = None, *, use_transcript: bool = True
) -> List[Tuple[str, str, str]]:
    """Every clause the attestation does not discharge: ``(id, text, problem)``.

    With ``use_transcript`` the attestation is the latest finish call in the
    durable transcript and quotes must predate it; otherwise ``attestation``
    is the call being made now and every result so far is available to it.
    """

    checklist = clauses(getattr(record, "requirements", None) or ())
    if not checklist:
        return []
    history = list(getattr(record, "provider_history", None) or ())
    before: Optional[int] = None
    if use_transcript:
        index, attestation = _finish_call_index(history)
        if index < 0:
            return [(cid, text, "not attested") for cid, text in checklist]
        if last_deliverable_edit(history) > index:
            return [(cid, text, "attested before the last edit") for cid, text in checklist]
        before = index
    attested = parse_attestation(attestation)
    observations = fresh_observations(history, before=before)
    problems: List[Tuple[str, str, str]] = []
    for cid, text in checklist:
        item = attested.get(cid)
        if item is None:
            problems.append((cid, text, "not attested"))
            continue
        status, observed = item["status"], item["observed"]
        if status == STATUS_VERIFIED:
            quote = normalize(observed)
            if len(quote.split()) < _MIN_QUOTE_TOKENS or len(quote) < _MIN_QUOTE_CHARS:
                problems.append((cid, text, "observed text too short to identify a result"))
            elif not any(f" {quote} " in seen for seen in observations):
                problems.append(
                    (
                        cid,
                        text,
                        "observed text is not in any browser, check or visual result after the last edit",
                    )
                )
        elif status == STATUS_NOT_APPLICABLE:
            if len(_WORD.findall(observed)) < _MIN_REASON_WORDS:
                problems.append((cid, text, "not_applicable needs a reason"))
        elif status == STATUS_UNMET:
            problems.append((cid, text, "reported unmet"))
        else:
            problems.append((cid, text, f"unknown status {status or 'missing'}"))
    return problems


def floor_reasons(record: Any) -> List[str]:
    """The completion-floor form: one reason per open clause, or one summary."""

    problems = judge(record)
    if not problems:
        return []
    if all(problem == "not attested" for _, _, problem in problems):
        ids = [cid for cid, _, _ in problems]
        shown = ", ".join(ids[:12]) + (", ..." if len(ids) > 12 else "")
        return [f"missing requirement attestation: {len(ids)} requirements ({shown})"]
    return [f"requirement {cid} {problem}: {_display(text)}" for cid, text, problem in problems]


def refusal_message(problems: Sequence[Tuple[str, str, str]]) -> str:
    """What the model reads when finish arrives without a complete attestation."""

    lines = [
        "finish was not accepted: a web deliverable is verified requirement by "
        'requirement. Call finish again, alone, with "requirements": one entry '
        "per ID below, each",
        '{"id": "<ID>", "status": "verified", "observed": "<text copied exactly '
        "from a browser_command, checks_run or visual_check result you received "
        'after your last edit>"}',
        'Use "not_applicable" with the reason in "observed" only for a line that '
        'asks nothing of the deliverable (a process note or context), and "unmet" '
        "for a requirement that is not met. Exercise or fix a requirement before "
        "you attest it; one browser call can return several facts at once.",
        "",
        "Open requirements:",
    ]
    lines.extend(f"{cid} [{problem}] {_display(text)}" for cid, text, problem in problems)
    return "\n".join(lines)
