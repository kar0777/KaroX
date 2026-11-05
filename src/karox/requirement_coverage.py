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
  model never received does not. A longer quote passes when it carries a run
  of ``COPIED_RUN_TOKENS`` consecutive tokens of one fresh result: models
  narrate around what they copy ("PASS lane-switch -- lane=1 / PASS jump ..."),
  and a measured run lost every attestation to the narration alone.

The attestation is read from the durable transcript -- the finish call's own
arguments -- so it needs no new session state, survives resume, and an edit
after it makes every quote stale by construction.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from .requirements import canonical
from .web_check import is_scratch_artifact, is_scratch_page

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
#: A quote of at least this many tokens is discharged by this many consecutive
#: tokens copied from one fresh result; a shorter quote must match whole. In
#: the measured run 37 of 37 honest attestations carried such a run while only
#: 14 matched whole; text the model never received carries none.
COPIED_RUN_TOKENS = 4
_MIN_REASON_WORDS = 3
_CLAUSE_DISPLAY = 160
#: The two ways a ``verified`` quote misses. They need different repairs: a
#: stale quote needs a new observation, a paraphrase only an exact copy.
NOT_QUOTED = "observed text is not in any browser, check or visual result after the last edit"
STALE_QUOTE = "quotes a result received before the last edit; observe it again"
#: Window of a closest-result hint, in tokens, and its display bound.
_HINT_TOKENS = 14
_HINT_CHARS = 180
_HINT_MIN_LINE_WORDS = 2
_HINT_STOP = frozenset(
    "the and for with that this from are was were not but all any has have its "
    "into than then when also only each data true false action evidence result "
    "must should does can will one its their there".split()
)


def _content_tokens(text: str) -> set[str]:
    return {
        token for token in normalize(text).split() if len(token) > 2 and token not in _HINT_STOP
    }

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
#: The same tokens located in original-case text, for copyable hints.
_TOKEN_ANY_CASE = re.compile(_TOKEN.pattern, re.IGNORECASE)
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


# --------------------------------------------------------------------------- #
# Named keys                                                                  #
# --------------------------------------------------------------------------- #
#
# A requirement that names keys ("Left / Right arrows or A / D: run") is about
# what the page does when those keys are pressed. Two benchmark runs attested
# such a line with evidence from other keys or from synthetic events on a probe
# page; the grader then pressed D and the game ignored it. A verified entry for
# a line that names keys therefore needs a real browser press of every named
# key on the deliverable page after the last edit. Detection is deliberately
# conservative: a single capital letter is a key only in a key context ("P:",
# "A / D", "**R**", "press W", "W key"), so "A HUD shows ..." names no key.

_ARROW_SYMBOLS = {"←": "arrowleft", "→": "arrowright", "↑": "arrowup", "↓": "arrowdown"}
_DIRECTIONS = {"left": "arrowleft", "right": "arrowright", "up": "arrowup", "down": "arrowdown"}
_NAMED_KEYS = {
    "space": "space",
    "spacebar": "space",
    "enter": "enter",
    "return": "enter",
    "esc": "escape",
    "escape": "escape",
    "tab": "tab",
    "backspace": "backspace",
    "shift": "shift",
}
_KEY_DISPLAY = {
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "space": "Space",
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "Backspace",
    "shift": "Shift",
}
_KEY_CONTEXT = re.compile(
    r"\b(?:keys?|keyboard|press(?:es|ed|ing)?|arrows?|wasd|controls?)\b", re.I
)
_PRESS_WORDS = frozenset({"press", "presses", "pressing", "pressed", "hit", "tap", "hold"})
_ARROW_PHRASE = re.compile(
    r"\b((?:left|right|up|down)(?:\s*(?:/|,|or|and|&)\s*(?:left|right|up|down))*)"
    r"[\s-]*arrows?\b",
    re.I,
)
_ARROW_NAME = re.compile(r"\bArrow(Left|Right|Up|Down)\b")
_ARROW_KEYS = re.compile(r"\barrow[\s-]+keys\b", re.I)
_KEY_TOKEN = re.compile(r"\*\*|[A-Za-z]+|[←→↑↓]|[/,:]")
_KEY_SEPARATORS = frozenset({"/", ",", "**", "or"})


def _key_text(text: str) -> str:
    """Clause text with short code spans unwrapped and long ones dropped."""

    def unwrap(match: "re.Match[str]") -> str:
        inner = match.group(0)[1:-1]
        return f" {inner} " if len(inner) <= 12 else " "

    return _CODE_SPAN.sub(unwrap, text)


def named_keys(text: str) -> Tuple[str, ...]:
    """Canonical names of the keys a requirement clause names, in order."""

    source = _key_text(str(text or ""))
    found: List[str] = []

    def add(name: str) -> None:
        if name not in found:
            found.append(name)

    tokens = _KEY_TOKEN.findall(source)
    lowered = [token.lower() for token in tokens]
    arrow_keys: List[int] = []
    for index, token in enumerate(tokens):
        if token not in _ARROW_SYMBOLS:
            continue
        before = lowered[index - 1] if index > 0 else ""
        after = lowered[index + 1] if index + 1 < len(tokens) else ""
        if (
            before in _KEY_SEPARATORS
            or after in _KEY_SEPARATORS
            or before in _ARROW_SYMBOLS
            or after in _ARROW_SYMBOLS
            or before in _PRESS_WORDS
            or after in {"key", "keys"}
        ):
            arrow_keys.append(index)
    context = bool(_KEY_CONTEXT.search(source)) or bool(arrow_keys)
    covered: List[Tuple[int, int]] = []
    for match in _ARROW_PHRASE.finditer(source):
        covered.append(match.span())
        for direction in re.findall(r"left|right|up|down", match.group(1), re.I):
            add(_DIRECTIONS[direction.lower()])
    for match in _ARROW_KEYS.finditer(source):
        # "Arrow keys: change direction" names all four.
        if not any(start <= match.start() < end for start, end in covered):
            for arrow in ("arrowup", "arrowdown", "arrowleft", "arrowright"):
                add(arrow)
    for match in _ARROW_NAME.finditer(source):
        add(_DIRECTIONS[match.group(1).lower()])
    if re.search(r"\bWASD\b", source):
        for letter in "wasd":
            add(letter)
    for index, token in enumerate(tokens):
        before = lowered[index - 1] if index > 0 else ""
        after = lowered[index + 1] if index + 1 < len(tokens) else ""
        if index in arrow_keys:
            add(_ARROW_SYMBOLS[token])
            continue
        if len(token) == 1 and "A" <= token <= "Z":
            first = index == 0 or (index == 1 and before == "**")
            if (
                (first and after == ":")
                or (before == "**" and after == "**")
            ) or (
                context
                and (
                    after in {":", "/", "key", "keys"}
                    or before == "/"
                    or (before == "**" and after == "**")
                    or before in _PRESS_WORDS
                    or (before in {",", "or"} and after in {":", ",", "/", "**"})
                )
            ):
                add(token.lower())
            continue
        name = _NAMED_KEYS.get(token.lower())
        if name is None:
            continue
        if (
            before in _PRESS_WORDS
            or after in {"key", "keys", "bar"}
            or (
                context
                and token[0].isupper()
                and (
                    after in {":", "/", ",", "or"}
                    or before in {"/", ",", "**", "or"}
                )
            )
        ):
            add(name)
    return tuple(found)


def _pressed_key(raw: Any) -> str:
    """A browser press result's key in the canonical form of `named_keys`."""

    key = str(raw or "")
    final = key.split("+")[-1] if key not in ("+",) else key
    if final == " ":
        return "space"
    if len(final) == 1:
        return final.lower()
    lowered = final.lower()
    code = re.fullmatch(r"key([a-z])", lowered)
    if code:
        return code.group(1)
    digit = re.fullmatch(r"digit([0-9])", lowered)
    if digit:
        return digit.group(1)
    if lowered in {"numpadenter", "return"}:
        return "enter"
    if lowered in {"esc"}:
        return "escape"
    if lowered in {"spacebar"}:
        return "space"
    return lowered


def key_display(name: str) -> str:
    return _KEY_DISPLAY.get(name, name.upper() if len(name) == 1 else name)


def _probe_url(url: str, diagnostic: Collection[str]) -> bool:
    """Whether a browser URL shows a scratch or harness page, not the deliverable."""

    try:
        path = unquote(urlparse(url).path or "")
    except ValueError:
        return False
    name = PurePosixPath(path).name
    if not name:
        return False
    if is_scratch_page(name):
        return True
    normalized = path.replace("\\", "/")
    return any(
        normalized == item or normalized.endswith("/" + item.lstrip("/"))
        for item in (str(entry).replace("\\", "/") for entry in diagnostic)
        if item
    )


def fresh_presses(
    history: Sequence[Mapping[str, Any]],
    *,
    before: Optional[int] = None,
    diagnostic: Collection[str] = (),
) -> set[str]:
    """Keys really pressed on a deliverable page after the last deliverable edit.

    The page a press reached is the last URL a browser result reported before
    it; a press on a probe or harness page proves nothing about the
    deliverable. A press whose page is unknown counts: the page was opened
    before this window, and the edit window already bounds it.
    """

    end = len(history) if before is None or before < 0 else before
    edit = last_deliverable_edit(history[:end], diagnostic)
    pressed: set[str] = set()
    current_url = ""
    for index, entry in enumerate(history[:end]):
        if entry.get("role") != "tool" or entry.get("core_name") != "browser.command":
            continue
        result = entry.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            continue
        data = result.get("data")
        if not isinstance(data, Mapping):
            continue
        url = data.get("url")
        if isinstance(url, str) and url:
            current_url = url
        if index <= edit or data.get("pressed") is not True:
            continue
        if current_url and _probe_url(current_url, diagnostic):
            continue
        key = _pressed_key(data.get("key"))
        if key:
            pressed.add(key)
    return pressed


def browser_worked(history: Sequence[Mapping[str, Any]]) -> bool:
    """Whether this run could press keys: one browser call succeeded."""

    for entry in history:
        if entry.get("role") != "tool" or entry.get("core_name") != "browser.command":
            continue
        result = entry.get("result")
        if isinstance(result, Mapping) and result.get("ok") is True:
            return True
    return False


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


def _is_deliverable(path: str, diagnostic: Collection[str]) -> bool:
    return not is_scratch_artifact(path) and path.replace("\\", "/") not in diagnostic


def last_deliverable_edit(
    history: Sequence[Mapping[str, Any]], diagnostic: Collection[str] = ()
) -> int:
    """Index of the last successful edit of a non-scratch file, or -1.

    A probe page written or deleted after the last real edit does not change
    what the user receives, so it does not make earlier observations stale;
    neither does an edit of a ``diagnostic`` page (the run's own unreferenced
    harness, see ``web_check.diagnostic_pages``).
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
        if not paths or any(_is_deliverable(path, diagnostic) for path in paths):
            last = index
    return last


def fresh_observations(
    history: Sequence[Mapping[str, Any]],
    *,
    before: Optional[int] = None,
    diagnostic: Collection[str] = (),
) -> List[str]:
    """Normalized behavioural results received after the last deliverable edit."""

    end = len(history) if before is None or before < 0 else before
    start = last_deliverable_edit(history[:end], diagnostic) + 1
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


def _observation_texts(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    return [
        str(entry.get("content") or "")
        for entry in entries
        if entry.get("role") == "tool"
        and entry.get("core_name") in OBSERVATION_TOOLS
        and isinstance(entry.get("result"), Mapping)
    ]


def stale_observations(
    history: Sequence[Mapping[str, Any]],
    *,
    before: Optional[int] = None,
    diagnostic: Collection[str] = (),
) -> List[str]:
    """Normalized behavioural results received before the last deliverable edit."""

    end = len(history) if before is None or before < 0 else before
    start = last_deliverable_edit(history[:end], diagnostic) + 1
    return [f" {text} " for text in map(normalize, _observation_texts(history[:start])) if text]


def _display(text: str) -> str:
    return text if len(text) <= _CLAUSE_DISPLAY else text[: _CLAUSE_DISPLAY - 3] + "..."


def _copied_runs(observations: Sequence[str]) -> set[str]:
    """Every run of ``COPIED_RUN_TOKENS`` consecutive tokens in the observations."""

    runs: set[str] = set()
    for seen in observations:
        tokens = seen.split()
        for start in range(len(tokens) - COPIED_RUN_TOKENS + 1):
            runs.add(" ".join(tokens[start : start + COPIED_RUN_TOKENS]))
    return runs


def _quoted(quote: str, observations: Sequence[str], runs: set[str]) -> bool:
    if any(f" {quote} " in seen for seen in observations):
        return True
    tokens = quote.split()
    return any(
        " ".join(tokens[start : start + COPIED_RUN_TOKENS]) in runs
        for start in range(len(tokens) - COPIED_RUN_TOKENS + 1)
    )


def judge(
    record: Any,
    attestation: Any = None,
    *,
    use_transcript: bool = True,
    diagnostic: Collection[str] = (),
    require_presses: Optional[bool] = None,
) -> List[Tuple[str, str, str]]:
    """Every clause the attestation does not discharge: ``(id, text, problem)``.

    With ``use_transcript`` the attestation is the latest finish call in the
    durable transcript and quotes must predate it; otherwise ``attestation``
    is the call being made now and every result so far is available to it.
    ``diagnostic`` names pages whose edits do not make observations stale.
    ``require_presses`` (default: whether a browser call succeeded in this
    run) makes a clause that names keys owe a real press of each of them.
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
        if last_deliverable_edit(history, diagnostic) > index:
            return [(cid, text, "attested before the last edit") for cid, text in checklist]
        before = index
    attested = parse_attestation(attestation)
    observations = fresh_observations(history, before=before, diagnostic=diagnostic)
    runs = _copied_runs(observations)
    stale: Optional[List[str]] = None
    stale_runs: set[str] = set()
    if require_presses is None:
        require_presses = browser_worked(history if before is None else history[:before])
    pressed = (
        fresh_presses(history, before=before, diagnostic=diagnostic)
        if require_presses
        else set()
    )
    problems: List[Tuple[str, str, str]] = []
    for cid, text in checklist:
        item = attested.get(cid)
        if item is None:
            problems.append((cid, text, "not attested"))
            continue
        status, observed = item["status"], item["observed"]
        keys = named_keys(text) if require_presses else ()
        if status == STATUS_VERIFIED:
            quote = normalize(observed)
            missing = [key for key in keys if key not in pressed]
            if len(quote.split()) < _MIN_QUOTE_TOKENS or len(quote) < _MIN_QUOTE_CHARS:
                problems.append((cid, text, "observed text too short to identify a result"))
            elif not _quoted(quote, observations, runs):
                if stale is None:
                    stale = stale_observations(history, before=before, diagnostic=diagnostic)
                    stale_runs = _copied_runs(stale)
                stale_copy = bool(stale) and _quoted(quote, stale, stale_runs)
                problems.append((cid, text, STALE_QUOTE if stale_copy else NOT_QUOTED))
            elif missing:
                listed = ", ".join(key_display(key) for key in missing)
                problems.append(
                    (
                        cid,
                        text,
                        f"names key(s) {listed} that were not pressed with "
                        "browser_command press on the deliverable page after the last edit",
                    )
                )
        elif status == STATUS_NOT_APPLICABLE:
            if keys:
                problems.append(
                    (
                        cid,
                        text,
                        "names keys the page must handle, so it is not not_applicable: "
                        "press them, or report unmet",
                    )
                )
            elif len(_WORD.findall(observed)) < _MIN_REASON_WORDS:
                problems.append((cid, text, "not_applicable needs a reason"))
        elif status == STATUS_UNMET:
            problems.append((cid, text, "reported unmet"))
        else:
            problems.append((cid, text, f"unknown status {status or 'missing'}"))
    return problems


def floor_reasons(record: Any, *, diagnostic: Collection[str] = ()) -> List[str]:
    """The completion-floor form: one reason per open clause, or one summary."""

    problems = judge(record, diagnostic=diagnostic)
    if not problems:
        return []
    if all(problem == "not attested" for _, _, problem in problems):
        ids = [cid for cid, _, _ in problems]
        shown = ", ".join(ids[:12]) + (", ..." if len(ids) > 12 else "")
        return [f"missing requirement attestation: {len(ids)} requirements ({shown})"]
    return [f"requirement {cid} {problem}: {_display(text)}" for cid, text, problem in problems]


def closest_results(
    record: Any,
    attestation: Any,
    problems: Sequence[Tuple[str, str, str]],
    *,
    diagnostic: Collection[str] = (),
) -> Dict[str, str]:
    """For each paraphrased ``verified`` entry, the fresh result text nearest to it.

    The 1017 runs narrated what they saw ("Status overlay text changed with
    state: READY, then RUNNING ...") instead of copying it, and each refusal
    sent them back to the browser for results they already had. The hint is
    a verbatim slice of a fresh result -- the window sharing the most words
    with the entry and its requirement -- so the repair is a copy, and the
    model still decides whether that result supports the line.
    """

    wanted = [(cid, text) for cid, text, problem in problems if problem == NOT_QUOTED]
    if not wanted:
        return {}
    history = list(getattr(record, "provider_history", None) or ())
    start = last_deliverable_edit(history, diagnostic) + 1
    located = []
    for raw in _observation_texts(history[start:]):
        raw = _unescape(raw)
        spans = [(m.start(), m.end(), m.group().lower()) for m in _TOKEN_ANY_CASE.finditer(raw)]
        if len(spans) >= COPIED_RUN_TOKENS:
            located.append((raw, spans))
    if not located:
        return {}
    attested = parse_attestation(attestation)
    hints: Dict[str, str] = {}
    for cid, text in wanted:
        observed = (attested.get(cid) or {}).get("observed", "")
        line_words = _content_tokens(text)
        said_words = _content_tokens(observed) - line_words
        best_score, best_text = 0, ""
        for raw, spans in located:
            width = min(_HINT_TOKENS, len(spans))
            for index in range(len(spans) - width + 1):
                window = {token for _, _, token in spans[index : index + width]}
                on_line = len(window & line_words)
                # The window must share words with the requirement itself, not
                # only with the narration: a hint is a candidate, never filler.
                if on_line < max(_HINT_MIN_LINE_WORDS, min(3, len(line_words) // 4)):
                    continue
                score = 2 * on_line + len(window & said_words)
                if score > best_score:
                    first, last = spans[index][0], spans[index + width - 1][1]
                    best_score, best_text = score, raw[first:last]
        if best_text:
            best_text = " ".join(best_text.split())
            if len(best_text) > _HINT_CHARS:
                best_text = best_text[:_HINT_CHARS].rsplit(" ", 1)[0]
            hints[cid] = best_text
    return hints


def checklist_block(entries: Sequence[Mapping[str, Any]]) -> str:
    """The finish checklist, shown with the task when a web deliverable is expected.

    Without it the IDs arrived in the first refusal, after the model had
    already finished, and the closing pass started from scratch: in the
    measured 1017 runs 11 and 19 more steps, about a third of each run's
    tokens. Listing them up front lets the model plan one closing pass.
    """

    listed = clauses(entries)
    if len(listed) < MIN_CLAUSES:
        return ""
    lines = [
        "<finish-checklist>",
        "KaroX reports this web deliverable requirement by requirement. finish "
        'may carry "requirements": {"id", "status", "observed"} entries for the '
        'IDs below -- "verified" with text copied from a browser_command, '
        "checks_run or visual_check result you already received after your last "
        'edit (four or more consecutive words of it), "not_applicable" with a '
        'reason for a line that only describes the process or context, or '
        '"unmet". Lines without a quote are reported as unverified; do not run '
        "extra checks only to collect quotes.",
    ]
    lines.extend(f"{cid} {_display(text)}" for cid, text in listed)
    lines.append("</finish-checklist>")
    return "\n".join(lines)


def refusal_message(
    problems: Sequence[Tuple[str, str, str]],
    hints: Optional[Mapping[str, str]] = None,
) -> str:
    """What the model reads when finish arrives without a complete attestation."""

    lines = [
        "finish was not accepted: a web deliverable is verified requirement by "
        'requirement. Call finish again, alone, with "requirements": one entry '
        "per ID below, each",
        '{"id": "<ID>", "status": "verified", "observed": "<text copied exactly '
        "from a browser_command, checks_run or visual_check result you received "
        'after your last edit>"}',
        "The copy must contain at least four consecutive words of one result (a "
        "shorter result may be quoted whole); words you add around it are not "
        "evidence.",
        'Use "not_applicable" with the reason in "observed" only for a line that '
        'asks nothing of the deliverable (a process note or context), and "unmet" '
        "for a requirement that is not met. Exercise or fix a requirement before "
        "you attest it; one browser call can return several facts at once.",
        "A line that names keys is verified only after each of those keys was "
        "pressed with browser_command action=press on the deliverable page after "
        "your last edit, in the state the line describes (synthetic events on a "
        "probe page do not count); several presses and reads can share one "
        "response.",
        "",
        "Open requirements:",
    ]
    for cid, text, problem in problems:
        lines.append(f"{cid} [{problem}] {_display(text)}")
        hint = (hints or {}).get(cid)
        if hint:
            lines.append(f"    closest fresh result (copy it only if it shows this line holds): {hint}")
    return "\n".join(lines)
