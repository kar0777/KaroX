"""Task dossier: the code a task is about, found before the model's first step.

Benchmark 2026-11-13 (32 KaroX trajectories, 1585 steps): 36% of all steps and
37% of the model's reasoning went to searching and reading before the first
edit -- work a deterministic index does in a second. The dossier ranks the
repository's source files by the identifiers the task names (weighted by how
rare each is, with a bonus where the name is defined), then hands the model the
top candidates, the definitions they hold, the tests that exercise them and the
recent history of the leading file. The model still decides where the change
belongs; it no longer has to find the candidates.
"""

from __future__ import annotations

import ast
import math
import time
import re
from collections import defaultdict
from typing import Any, Dict, List, Sequence

_CODE_SUFFIXES = (
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
    ".java", ".kt", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cs", ".swift",
    ".sh", ".cfg", ".toml", ".vue", ".svelte",
)
_NON_SOURCE_DIRS = ("docs/", "doc/", "examples/", "example/", "benchmarks/", "asv_bench/", "website/", "test_data/", "testdata/", "fixtures/")
_GENERATED = re.compile(r"(^|/)(dist|build|vendor|_vendored|node_modules|generated|third_party|static/vendor)/|\.min\.(js|css)$")
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$|(^|/)conftest\.py$")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PATHLIKE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w-][\w.-]*\.[A-Za-z]{1,5})(?![\w/])")
_BACKTICK = re.compile(r"`([^`\n]{2,120})`")
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_STOP = frozenset(
    """the and for with that this from not are was were but have has had you your our its into when what which
    then than there their them they will would should could can may might must also only just like such some
    any all each other more most less very same new old use used using get set default value values true false
    none null self return def class import none str int bool list dict type types error errors issue bug fix
    expected actual behavior behaviour output input example code file files test tests function method
    python version current case cases data name names option options argument arguments param params
    print run call called returns raise raised object instance""".split()
)
MAX_CANDIDATES = 6
MAX_DEFINITIONS = 3
DEFINITION_LINES = 45
MAX_TEXT = 14_000
MAX_FILE_BYTES = 1_000_000
SCAN_SECONDS = 8.0
TITLE_BOOST = 3.0


def _is_source(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.endswith(_CODE_SUFFIXES)
        and not _TEST_PATH.search(lowered)
        and not lowered.startswith(_NON_SOURCE_DIRS)
        and "/docs/" not in lowered
        and "/examples/" not in lowered
        and not _GENERATED.search(lowered)
    )


def _identifiers(task: str) -> Dict[str, float]:
    """Code-like names in the task, weighted by where they appear."""

    weights: Dict[str, float] = defaultdict(float)
    code = " ".join(_FENCE.findall(task)) + " " + " ".join(_BACKTICK.findall(task))
    prose = _FENCE.sub(" ", task)
    for source, base in ((code, 2.0), (prose, 1.0)):
        for word in _WORD.findall(source):
            lowered = word.lower()
            if lowered in _STOP or word.isdigit():
                continue
            shaped = "_" in word.strip("_") or (word[0].isupper() and any(c.islower() for c in word[1:]) and any(c.isupper() for c in word[1:]))
            if base == 1.0 and not shaped:
                continue  # plain English in prose is not a code name
            weight = base + 0.5 if shaped else (1.2 if base > 1.0 else 1.0)
            weights[word] = max(weights[word], weight)
    # The title says what the task is about; a pasted config or log below it
    # can name dozens of keys that are only context.
    title = (task.strip().splitlines() or [""])[0]
    for word in _WORD.findall(title):
        if word in weights:
            weights[word] *= TITLE_BOOST
    return dict(weights)


def _git(core: Any, argv: List[str]) -> str:
    try:
        result = core._git(argv, 30.0)
    except Exception:  # noqa: BLE001 - the dossier is optional context
        return ""
    if not isinstance(result, dict) or result.get("exit_code") not in (0, 1):
        return ""
    return str(result.get("stdout") or "")


def _tracked(core: Any, suffix: str = "") -> List[str]:
    """The repository's files: git's list, or a pruned walk outside a checkout.

    A benchmark's /app is often not a git checkout; ls-files then lists nothing
    and the whole dossier (candidate files, their tests) silently disappeared.
    """
    listed = [line for line in _git(core, ["ls-files"]).splitlines() if line]
    if not listed:
        walk = getattr(core, "_walk_files", None)
        if callable(walk):
            try:
                root = core.repository
                listed = []
                for path in walk():
                    listed.append(path.relative_to(root).as_posix())
                    if len(listed) >= 20_000:
                        break
            except Exception:  # noqa: BLE001 - the dossier is optional context
                listed = []
    return [path for path in listed if path.endswith(suffix)] if suffix else listed


def build(core: Any, task: str) -> Dict[str, Any]:
    """Rank candidate files for ``task`` and render the dossier text."""

    names = _identifiers(task or "")
    paths_named = {m.strip("./") for m in _PATHLIKE.findall(task or "")}
    tracked = _tracked(core)
    sources = [path for path in tracked if _is_source(path)]
    if not sources:
        return {"files": [], "text": ""}
    source_set = set(sources)
    hits: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tests_for: Dict[str, set] = defaultdict(set)
    if names:
        wanted = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)[:120]) + r")(?![A-Za-z0-9_])")
        tests = [path for path in tracked if _TEST_PATH.search(path.lower()) and path.lower().endswith(_CODE_SUFFIXES)]
        deadline = time.monotonic() + SCAN_SECONDS
        for path in [*sources, *tests[:4000]]:
            if time.monotonic() > deadline:
                break
            try:
                file = core.repository / path
                if file.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for token in wanted.findall(text):
                if path in source_set:
                    hits[path][token] += 1
                else:
                    tests_for[token].add(path)
    df: Dict[str, int] = defaultdict(int)
    for counts in hits.values():
        for token in counts:
            df[token] += 1
    total = max(len(sources), 1)
    idf = {token: math.log(1 + total / n) for token, n in df.items()}
    defined: Dict[str, List[str]] = defaultdict(list)
    rare = [name for name in names if 0 < df.get(name, 0) <= max(3, total // 20)]
    if rare:
        definition = re.compile(
            r"^[ 	]*(?:export[ 	]+(?:default[ 	]+)?)?(?:async[ 	]+)?(?:def|class|function|func|fn|const|let|var|type|interface|struct)[ 	]+("
            + "|".join(re.escape(n) for n in rare[:80])
            + r")(?![A-Za-z0-9_])",
            re.M,
        )
        for path in hits:
            try:
                text = (core.repository / path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in definition.finditer(text):
                defined[path].append(f"{match.group(1)}:{text.count(chr(10), 0, match.start()) + 1}")
    scores: Dict[str, float] = {}
    reasons: Dict[str, List[str]] = {}
    for path, counts in hits.items():
        score = 0.0
        for token, count in counts.items():
            score += names.get(token, 1.0) * idf.get(token, 0.0) * (1 + math.log(count))
        definitions = defined.get(path, [])
        score += sum(4.0 * idf.get(item.split(":")[0], 1.0) for item in definitions)
        scores[path] = score
        reasons[path] = sorted(counts, key=lambda t: -idf.get(t, 0))[:5]
    # Plain words of the task ("python templater", "grdmask") name modules too.
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", task or "")} - _STOP
    title_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", (task or "").strip().split("\n")[0])} - _STOP
    if words:
        parts = {path: set(re.split(r"[/_.\-]+", path.lower())) for path in sources}
        # "templater" names src/.../templaters/: a segment matches a task word
        # or that word plus a short suffix.
        stems = {seg for segs in parts.values() for seg in segs if len(seg) >= 4 and any(seg == w or (seg.startswith(w) and len(seg) - len(w) <= 2) for w in words)}
        path_df: Dict[str, int] = defaultdict(int)
        for segs in parts.values():
            for seg in segs & stems:
                path_df[seg] += 1
        for path, segs in parts.items():
            matched = [seg for seg in segs if seg in stems and path_df[seg] <= max(2, total // 10)]
            if matched:
                scores[path] = scores.get(path, 0.0) + sum(
                    1.5 * (TITLE_BOOST if any(m.startswith(w) for w in title_words) else 1.0) * math.log(1 + total / path_df[m])
                    for m in matched
                )
                reasons.setdefault(path, []).extend(m for m in matched if m not in reasons.get(path, []))
    for path in paths_named:
        for candidate in sources:
            if candidate == path or candidate.endswith("/" + path):
                scores[candidate] = scores.get(candidate, 0.0) + 50.0
                reasons.setdefault(candidate, []).insert(0, "named in the task")
    ranked = sorted(scores, key=lambda p: (-scores[p], p))[:MAX_CANDIDATES]
    if not ranked:
        return {"files": [], "text": ""}
    files = [
        {"path": p, "score": round(scores[p], 2), "names": reasons.get(p, []), "defines": defined.get(p, [])}
        for p in ranked
    ]
    return {"files": files, "text": _render(core, files, tests_for, names)}


def _definition_text(core: Any, path: str, line: int) -> str:
    try:
        raw = (core.repository / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = line - 1
    if start < 0 or start >= len(raw):
        return ""
    indent = len(raw[start]) - len(raw[start].lstrip())
    end = start + 1
    while end < len(raw) and end - start < DEFINITION_LINES:
        text = raw[end]
        if text.strip() and len(text) - len(text.lstrip()) <= indent and not text.lstrip().startswith((")", "]", "}", "@")):
            break
        end += 1
    body = "\n".join(f"{n + 1:5} {raw[n]}" for n in range(start, end))
    more = " (continues)" if end < len(raw) and end - start >= DEFINITION_LINES else ""
    return f"{path} lines {start + 1}-{end}{more}:\n{body}"


def _functions_about(core: Any, path: str, names: Dict[str, float]) -> List[tuple]:
    """Python functions in ``path`` whose bodies use the most task names."""

    if not path.endswith(".py") or not names:
        return []
    try:
        source = (core.repository / path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return []
    lines = source.splitlines()
    found: List[tuple] = []

    def visit(node: Any, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    end = getattr(child, "end_lineno", child.lineno) or child.lineno
                    body = "\n".join(lines[child.lineno - 1 : end])
                    used = {n for n in names if names[n] >= 1.5 and re.search(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])", body)}
                    if used:
                        score = sum(names[n] for n in used) / (1 + math.log(1 + (end - child.lineno) / 40))
                        if child.name == "__init__":
                            score *= 0.5  # a parameter list names everything
                        found.append((score, qual, child.lineno, end))
                visit(child, qual + ".")

    visit(tree, "")
    found.sort(key=lambda item: -item[0])
    return [(qual, start, end) for _, qual, start, end in found]


def _render(core: Any, files: Sequence[Dict[str, Any]], tests_for: Dict[str, set], names: Dict[str, float]) -> str:
    lines = [
        "KaroX located these files from the names in the task (ranked; the change "
        "usually belongs in one of the first; verify before editing):"
    ]
    for item in files:
        why = ", ".join([n for n in item["names"] if names.get(n, 0) >= 1.5 or n == "named in the task" or n not in names][:5])
        defines = ", ".join(d.split(":")[0] + " (line " + d.split(":")[1] + ")" for d in item["defines"][:4])
        lines.append(f"- {item['path']}" + (f" -- defines {defines}" if defines else "") + (f"; mentions {why}" if why else ""))
    shown = 0
    blocks: List[str] = []
    for item in files[:2]:
        for qualname, start, end in _functions_about(core, item["path"], names)[:2]:
            if shown >= MAX_DEFINITIONS:
                break
            text = _definition_text(core, item["path"], start)
            if text:
                blocks.append(f"[{qualname} uses the task's names]\n" + text)
                shown += 1
    for item in files[:3]:
        for entry in item["defines"]:
            if shown >= MAX_DEFINITIONS:
                break
            name, _, lineno = entry.partition(":")
            text = _definition_text(core, item["path"], int(lineno or 0))
            if text:
                blocks.append(text)
                shown += 1
    if blocks:
        lines.append("\nDefinitions the task names:")
        lines.extend(blocks)
    top_names = [n for n in sorted(names, key=lambda n: -names[n]) if n in tests_for][:6]
    tests = sorted({t for n in top_names for t in tests_for[n]}, key=lambda t: -sum(t in tests_for[n] for n in top_names))[:5]
    if tests:
        lines.append("\nTests that use those names: " + ", ".join(tests))
    history = _git(core, ["log", "-4", "--format=%h %ad %s", "--date=short", "--", files[0]["path"]]).strip()
    if history:
        lines.append(f"\nRecent commits to {files[0]['path']}:\n{history}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 20] + "\n[dossier clipped]"


def related_tests(core: Any, sources: Sequence[str], hint: str = "", limit: int = 2) -> List[str]:
    """Test files for changed Python sources.

    Benchmark 2026-11-13 (click-3239 v46): picking tests by import alone ran
    two files that never touch the edited function, reported "passed", and the
    model reran the real ones itself. Ranking is now: a test named after the
    module, then tests that use the names in the edited code (rarest first),
    then tests importing the module.
    """

    sources = [s for s in sources if s.endswith(".py") and not _TEST_PATH.search(s.lower())]
    if not sources:
        return []
    tracked = _tracked(core, ".py")
    tests = [path for path in tracked if _TEST_PATH.search(path.lower()) and not path.endswith("conftest.py")]
    found: List[str] = []
    from .check_targets import _closeness

    for source in sources:
        stem = source.rsplit("/", 1)[-1][:-3]
        if stem == "__init__" and "/" in source:
            stem = source.rsplit("/", 2)[-2]  # a package's tests carry its name
        named = [t for t in tests if t.rsplit("/", 1)[-1] in (f"test_{stem}.py", f"{stem}_test.py", f"test_{stem.lstrip('_')}.py") and t not in found]
        # sympy/core/basic.py: core/tests/test_basic.py, not another package's.
        named.sort(key=lambda t: -_closeness(t, [tuple(source.split("/")[:-1])]))
        found += named
    texts: Dict[str, str] = {}
    for path in tests[:3000]:
        try:
            texts[path] = (core.repository / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    names = {w for w in _WORD.findall(hint or "") if ("_" in w.strip("_") or any(c.isupper() for c in w[1:])) and w.lower() not in _STOP}
    if names and len(found) < limit:
        pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)[:60]) + r")(?![A-Za-z0-9_])")
        counts: Dict[str, Dict[str, int]] = {}
        for path, text in texts.items():
            hits: Dict[str, int] = defaultdict(int)
            for token in pattern.findall(text):
                hits[token] += 1
            if hits:
                counts[path] = hits
        df: Dict[str, int] = defaultdict(int)
        for hits in counts.values():
            for token in hits:
                df[token] += 1
        total = max(len(texts), 1)
        scored = sorted(
            counts,
            key=lambda p: -sum(math.log(1 + total / df[t]) * (1 + math.log(n)) for t, n in counts[p].items()),
        )
        found += [p for p in scored if p not in found][: limit - len(found)]
    for source in sources:
        if len(found) >= limit:
            break
        module = source[:-3]
        for prefix in ("src/", "lib/"):
            if module.startswith(prefix):
                module = module[len(prefix):]
        dotted = module.replace("/", ".").removesuffix(".__init__")
        imports = re.compile(r"(?:from|import)\s+" + re.escape(dotted) + r"(?![\w])")
        found += [p for p, text in texts.items() if p not in found and imports.search(text)][: limit - len(found)]
    return found[:limit]
