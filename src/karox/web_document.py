"""Pure document analyzers for web checks: import maps, scripts and pages."""

from __future__ import annotations

from .web_scripts import (  # noqa: F401
    _FRAMES_DRAWN_JS as _FRAMES_DRAWN_JS,
    _FRAME_GATE_JS as _FRAME_GATE_JS,
    _PAGE_PROBE_JS as _PAGE_PROBE_JS,
    _PIXEL_STATS_JS as _PIXEL_STATS_JS,
    _RESIZE_PROBE_JS as _RESIZE_PROBE_JS,
    _SCRATCH_ARTIFACT as _SCRATCH_ARTIFACT,
    _SCRATCH_PAGE as _SCRATCH_PAGE,
    _SVG_FIT_JS as _SVG_FIT_JS,
)
import json
import pathlib
import re
from html.parser import HTMLParser
from typing import Optional



_MODULE_BLOCK = re.compile(
    r"<script[^>]*type\s*=\s*[\"']module[\"'][^>]*>(.*?)</script>", re.S | re.I
)

_IMPORT_MAP = re.compile(
    r"<script[^>]*type\s*=\s*[\"']importmap[\"'][^>]*>(.*?)</script>", re.S | re.I
)

# Matches `import x from "s"`, `export { y } from "s"` and the side-effect form
# `import "s"`. A static `import(` call is not a module specifier for the loader.
_FROM = re.compile(r"\b(?:import|export)\b[^;]*?\bfrom\s*[\"']([^\"']+)[\"']")

_BARE_SIDE_EFFECT = re.compile(r"\bimport\s*[\"']([^\"']+)[\"']")

_MAX_PAGES = 20

def import_map_targets(text: str) -> dict[str, str]:
    """Every specifier prefix the page's import maps define."""

    targets: dict[str, str] = {}
    for block in _IMPORT_MAP.findall(text):
        try:
            payload = json.loads(block)
        except ValueError:
            continue
        imports = payload.get("imports") if isinstance(payload, dict) else None
        if isinstance(imports, dict):
            for key, value in imports.items():
                if isinstance(key, str) and isinstance(value, str) and key:
                    targets.setdefault(key, value)
    return targets

def module_specifiers(text: str) -> list[str]:
    specifiers: list[str] = []
    for block in _MODULE_BLOCK.findall(text):
        for match in _FROM.finditer(block):
            specifiers.append(match.group(1))
        for match in _BARE_SIDE_EFFECT.finditer(block):
            specifiers.append(match.group(1))
    return specifiers

def _resolves_bare(specifier: str, targets: dict[str, str]) -> bool:
    if specifier in targets:
        return True
    # A trailing-slash key maps a whole subtree: "three/addons/" -> ".../jsm/".
    return any(
        key.endswith("/") and specifier.startswith(key) for key in targets
    )

# A module fetched by URL can itself import a bare specifier, and the page cannot
# know that without fetching it. The three.js addons are the case this benchmark
# hit twice: OrbitControls.js imports `three`, so a page that loads it by URL and
# defines no import map fails with "Failed to resolve module specifier "three""
# even though every specifier the page itself wrote is a full URL.
_ADDONS = re.compile(r"https?://[^\"']*three@[^\"']*/(?:examples/jsm|addons)/")

def _addons_without_three_mapping(text: str, targets: dict[str, str]) -> list[str]:
    if "three" in targets:
        return []
    # A "three/" prefix entry is not enough: the addon module itself imports the
    # bare specifier "three", which a trailing-slash mapping cannot resolve.
    problems: list[str] = []

    def addon(specifier: str) -> bool:
        return bool(
            _ADDONS.search(specifier)
            or specifier.startswith(("three/addons/", "three/examples/jsm/"))
        )

    for block in _MODULE_BLOCK.findall(text):
        for match in _FROM.finditer(block):
            specifier = match.group(1)
            if addon(specifier):
                problems.append(specifier)
        for match in _BARE_SIDE_EFFECT.finditer(block):
            specifier = match.group(1)
            if addon(specifier):
                problems.append(specifier)
    return problems

def check_page(page: pathlib.Path) -> list[str]:
    """Return every import this page cannot resolve, as user-facing lines."""

    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return [f"{page.name}: cannot be read ({type(error).__name__})"]
    targets = import_map_targets(text)
    problems: list[str] = []
    for specifier in module_specifiers(text):
        if specifier.startswith(("http://", "https://", "data:", "//")):
            continue
        if specifier.startswith(("./", "../", "/")):
            if specifier.startswith("/"):
                # An absolute path is served from a server root this check cannot
                # know; only a relative target can be proved missing here.
                continue
            # A cache-busting query or a fragment is not part of the file name.
            target = (page.parent / specifier.split('?', 1)[0].split('#', 1)[0]).resolve()
            if not target.exists():
                problems.append(
                    f"{page.name}: imports {specifier!r}, which does not exist"
                )
            continue
        if not _resolves_bare(specifier, targets):
            problems.append(
                f"{page.name}: imports the bare specifier {specifier!r} with no "
                "import-map entry; a browser cannot resolve it"
            )
    for specifier in _addons_without_three_mapping(text, targets):
        problems.append(
            f"{page.name}: loads {specifier!r}, whose own module imports 'three', "
            "and the page defines no import map for it; the browser will refuse "
            "with \"Failed to resolve module specifier \\\"three\\\"\""
        )
    for problem in _document_problems(page.name, text):
        problems.append(problem)
    for problem in _duplicate_declarations(page.name, text):
        problems.append(problem)
    return problems

class _DocumentEndParser(HTMLParser):
    """Recognize markup end tags, not raw-text/RCDATA content."""

    _TEXT_ELEMENTS = {
        "script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes",
    }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._TEXT_ELEMENTS:
            self.set_cdata_mode(tag)

    def __init__(self) -> None:
        super().__init__()
        self.closed_html = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "html":
            self.closed_html = True

_JS_DECLARATION = re.compile(
    r"(?:export\s+)?(?:const|let|class)\s+([A-Za-z_$][\w$]*)"
)

def _top_level_declarations(source: str) -> list[str]:
    """Names declared at one script's top level, in order.

    A small scanner rather than a parser: strings and comments are skipped and
    braces track depth, which is enough to tell a module-level ``const FISH_L``
    from a local of the same name inside a function (legal shadowing).
    """

    names: list[str] = []
    depth = 0
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "/" and index + 1 < length:
            if source[index + 1] == "/":
                newline = source.find("\n", index)
                index = length if newline < 0 else newline + 1
                continue
            if source[index + 1] == "*":
                end = source.find("*/", index + 2)
                index = length if end < 0 else end + 2
                continue
            before = source[:index].rstrip()
            if not before or before[-1] in "(,=:[!&|?{};+-*%<>~^" or re.search(
                r"\b(?:return|typeof|case|in|of|void|delete)$", before
            ):
                # A regex literal: "/^https?:\/\//" holds "//" that is not a
                # comment, and skipping its line lost a "{" (false redeclaration).
                index += 1
                in_class = False
                while index < length and source[index] != "\n":
                    if source[index] == "\\":
                        index += 2
                        continue
                    if source[index] == "[":
                        in_class = True
                    elif source[index] == "]":
                        in_class = False
                    elif source[index] == "/" and not in_class:
                        index += 1
                        break
                    index += 1
                continue
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0:
            match = _JS_DECLARATION.match(source, index)
            if match is not None:
                prefix = source[max(0, index - 24) : index].rstrip()
                if prefix.endswith(("for (", "for(", "for await (", "for await(")):
                    # `for (let i ...)` and `for (const sx of ...)` bind inside
                    # the loop statement; several such loops at module level are
                    # legal and must not be reported as redeclarations.
                    index = match.end()
                    continue
                names.append(match.group(1))
                index = match.end()
                continue
        index += 1
    return names

class _ScriptBlockParser(HTMLParser):
    """Collect inline script bodies; external ``src`` scripts are not read."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._buffer: list[str] | None = None
        self._external = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        self._external = bool(attributes.get("src"))
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._buffer is None:
            return
        if not self._external:
            self.blocks.append("".join(self._buffer))
        self._buffer = None
        self._external = False

def _duplicate_declarations(name: str, text: str) -> list[str]:
    """Top-level names a browser would reject as already declared.

    Two aquarium runs added a second ``const FISH_L`` / ``const
    waterVolumeMaterial`` while appending a section; the page threw before the
    scene ran and only a visual check could see it. This is the static guard,
    measured on the file the mutation just wrote.
    """

    parser = _ScriptBlockParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page is reported elsewhere
        return []
    problems: list[str] = []
    for source in parser.blocks:
        seen: set[str] = set()
        reported: set[str] = set()
        for declared in _top_level_declarations(source):
            if declared in seen and declared not in reported:
                reported.add(declared)
                problems.append(
                    f"{name}: declares '{declared}' more than once at top level; "
                    f"the browser throws \"Identifier '{declared}' has already "
                    "been declared\" before the page runs"
                )
            seen.add(declared)
        if "BufferGeometry" in source and len(re.findall(r"\b(?:const|let|var)\s+ind(?:ex|ices)\w*\s*=\s*\[\s*\]", source)) > source.count("setIndex("):
            problems.append(f"{name}: fills an index list for a BufferGeometry but never calls setIndex(); every three positions then draw as one triangle and the mesh renders as scattered shards")
        torn = _jittered_polyhedron(source)
        if torn:
            problems.append(f"{name}: moves each vertex of {torn} by its own random amount, but that geometry keeps a separate copy of every corner for each face, so the faces split apart and the solid renders cracked; merge first (BufferGeometryUtils.mergeVertices from three/addons/utils/BufferGeometryUtils.js) or displace by a function of the vertex position")
    return problems


_POLYHEDRON = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*new\s+(?:THREE\.)?((?:Icosahedron|Dodecahedron|Octahedron|Tetrahedron)Geometry)\(")
_ZERO_ARG_RANDOM = re.compile(r"\b\w*(?:random|rand|rnd|seed|jitter)\w*\s*\(\s*\)", re.IGNORECASE)


def _jittered_polyhedron(source: str) -> Optional[str]:
    """The built-in polyhedron a loop displaces vertex by vertex at random.

    Since three.js r125 these geometries are not indexed: a corner shared by
    five faces is five vertices, and an independent random offset per vertex
    tears the faces apart (the 2026-11-12 aquarium shipped cracked stones this
    way; 3 of 888 earlier pages did it, all broken). An offset computed from the
    position moves every copy alike and is not reported.
    """

    if "mergeVertices" in source:
        return None
    for made in _POLYHEDRON.finditer(source):
        tail = source[made.end() : made.end() + 1500]
        attribute = re.search(
            r"(?:const|let|var)\s+(\w+)\s*=\s*" + re.escape(made.group(1))
            + r"\.(?:attributes\.position|getAttribute\(\s*['\"]position['\"]\s*\))",
            tail,
        )
        if not attribute:
            continue
        name = re.escape(attribute.group(1))
        rest = tail[attribute.end() :]
        loop = re.search(r"for\s*\([^)]*" + name + r"\.count[^)]*\)\s*\{", rest)
        if not loop:
            continue
        body, depth = rest[loop.end() :], 1
        for index, char in enumerate(body):
            depth += (char == "{") - (char == "}")
            if not depth:
                body = body[:index]
                break
        if _ZERO_ARG_RANDOM.search(body) and re.search(name + r"\.set(?:XYZ|X|Y|Z)\(", body):
            return made.group(2)
    return None

def _document_problems(name: str, text: str) -> list[str]:
    """Whether this is one complete document rather than the middle of one.

    A browser renders a page that lost its ending, which is how a run's edit
    spree shipped a file with no closing ``</html>`` and a check that passed it.
    Leaving the work half-typed is the failure mode, so it is named here rather
    than left for a reader of the file to notice.
    """

    problems: list[str] = []
    stripped = text.strip()
    if not stripped:
        return [f"{name}: the file is empty"]
    if not stripped.lower().startswith("<!doctype html"):
        problems.append(
            f"{name}: does not start with <!doctype html>, so this is not one "
            "complete HTML document"
        )
    document = _DocumentEndParser()
    document.feed(stripped)
    document.close()
    if not document.closed_html:
        problems.append(
            f"{name}: has no closing </html>, so the document was cut short; the "
            "browser will still render what is there, which is why it has to be "
            "said out loud"
        )
    return problems

_DYNAMIC_MARKUP = re.compile(
    r"<\s*(?:script|canvas|video|audio|iframe|object|embed|link|img|picture|source|image|feimage|foreignobject|animate(?:Transform|Motion)?|set)\b"
    r"|<\s*meta\b[^>]*\bhttp-equiv\s*=\s*[\"']?\s*refresh\b"
    r"|\bon[a-z]+\s*="
    r"|@(?:keyframes|import|font-face)\b"
    r"|\b(?:animation|transition)(?:-[a-z]+)?\s*:"
    r"|\b(?:offset-path|will-change)\s*:"
    r"|\brequestAnimationFrame\b"
    r"|\b(?:src|srcset)\s*="
    r"|\b(?:xlink:)?href\s*=\s*(?:[\"']\s*[^#\"'\s]|[^\"'\s#])"
    r"|\burl\s*\(\s*(?:[\"']\s*[^#\"'\s]|[^\"'\s#)])",
    re.I,
)

def is_static_document(page: pathlib.Path) -> bool:
    """True only when markup is self-contained and proven free of scripts, animations or external/media assets."""

    suffix = page.suffix.lower()
    if suffix not in {".svg", ".html", ".htm"}:
        return False
    try:
        if page.is_symlink() or page.stat().st_size > _MAX_PICTURE_BYTES:
            return False
        text = page.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip() or _DYNAMIC_MARKUP.search(text):
        return False
    if suffix == ".svg":
        return not check_picture(page)
    return not check_page(page)

_MAX_PICTURE_BYTES = 8_000_000

def check_picture(picture: pathlib.Path) -> list[str]:
    """Problems a browser would show instead of the SVG picture.

    A standalone SVG is XML: one unescaped ``&`` or an unclosed tag and the
    browser renders a parse error where the drawing should be. Only the bytes
    are read; nothing is rendered or fetched.
    """

    import xml.etree.ElementTree as ElementTree

    try:
        if picture.stat().st_size > _MAX_PICTURE_BYTES:
            return []
        data = picture.read_bytes()
    except OSError as error:
        return [f"{picture.name}: could not be read ({type(error).__name__})"]
    if not data.strip():
        return [f"{picture.name}: is empty"]
    try:
        element = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        return [
            f"{picture.name}: is not well-formed XML ({error}); a browser shows "
            "a parse error instead of the picture"
        ]
    if element.tag != f"{_SVG_NAMESPACE}svg":
        if element.tag == "svg":
            return [
                f"{picture.name}: the <svg> root has no "
                'xmlns="http://www.w3.org/2000/svg"; opened as a file, a browser '
                "shows the markup as text instead of the picture"
            ]
        return [f"{picture.name}: the root element is <{element.tag}>, not <svg>"]
    return []


def is_scratch_page(page: str | pathlib.Path) -> bool:
    """Whether a page is a diagnostic surface, not part of the deliverable.

    A 3D run wrote ``_probe.html`` to inspect pixels and left it in the tree.
    ``default_pages`` sorted every root HTML, so ``_probe.html`` came before
    ``index.html`` and the final visual check judged the scratch page. Its
    failure then stayed owed against a correct game. A leading underscore or a
    probe/scratch/tmp/debug token in the stem marks a page as scratch; the
    caller still refuses to let such a page prove or block the deliverable.
    """

    name = pathlib.Path(page).name
    if not name:
        return False
    if name.startswith(("_", ".")):
        return True
    return bool(_SCRATCH_PAGE.search(pathlib.Path(name).stem))

def is_scratch_artifact(path: str | pathlib.Path) -> bool:
    """Whether a path is diagnostic work, not a task input.

    ``identity`` binds every proof to the bytes it judged. A run that deletes
    its own ``_probe.html`` or ``_scratch_*.py`` after a passing check must not
    invalidate that proof: the deliverable did not change. HTML keeps the wider
    page rule; other files need a probe/scratch/tmp token, so ``__init__.py``
    and ``debug.py`` are not swept away as scratch.
    """

    name = pathlib.Path(path).name
    if not name:
        return False
    if pathlib.Path(name).suffix.lower() in {".html", ".htm"}:
        return is_scratch_page(path)
    return bool(_SCRATCH_ARTIFACT.search(pathlib.Path(name).stem))

def default_pages(root: pathlib.Path) -> list[pathlib.Path]:
    """Root-level deliverable pages, with the conventional entry page first.

    ``glob("*.html")`` is case-sensitive on POSIX and case-insensitive on Windows,
    so the same repository would be checked on one platform and silently skipped
    on the other -- and a skipped deliverable is the exact failure this check
    exists to catch. Scratch pages are excluded: checking them is useful on
    request, but treating them as the deliverable makes a temporary diagnostic
    page outrank ``index.html`` and block the run.
    """

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    pages = [
        path
        for path in entries
        if path.is_file()
        and path.suffix.lower() == ".html"
        and not is_scratch_page(path)
    ]
    pages.sort(
        key=lambda path: (
            0 if path.name.lower() in {"index.html", "index.htm"} else 1,
            path.name.lower(),
        )
    )
    return pages[:_MAX_PAGES]

_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"

def default_pictures(root: pathlib.Path) -> list[pathlib.Path]:
    """Root-level SVG files that are not scratch work, by name."""

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [
        path
        for path in entries
        if path.is_file()
        and path.suffix.lower() == ".svg"
        and not is_scratch_page(path)
    ][:_MAX_PAGES]

def shadowed_by_repository() -> Optional[str]:
    """Report the path that shadows this module, or ``None`` when it is genuine.

    ``python -m karox.web_check`` puts the working directory first on ``sys.path``,
    and a check runs in the repository it checks. A measured run wrote
    ``karox/web_check.py`` into the repository, so *that* file answered the check,
    passed it, and KaroX reported the page verified on the strength of a checker
    the deliverable had written. A check the artifact can rewrite is not a check,
    so this one refuses to be that: it looks up ``karox.web_check`` with the
    working directory removed from the path and compares the origin with itself.

    ``None`` also covers the case where nothing else provides the module -- a
    checkout of KaroX itself, where the repository *is* the source of truth.
    """

    import importlib.util

    import sys

    # This function lives in web_document.py, but the module being checked is
    # karox.web_check. Comparing web_document.py with web_check.py falsely
    # rejects every genuine installed checker as a repository shadow.
    here = pathlib.Path(__file__).with_name("web_check.py").resolve()
    cwd = pathlib.Path.cwd().resolve()
    # Only code *executed from the checked repository* can be a repository
    # shadow. In an isolated CLI run the system Python may import KaroX from
    # one installed location while a second legitimate venv supplies the
    # find_spec origin. Those different installations are NOT a malicious
    # checker written by the task. Do not turn this mismatch into a false
    # verification failure; the separate random checker challenge stays intact.
    if not here.is_relative_to(cwd):
        return None
    stripped = [
        entry
        for entry in sys.path
        if entry and not _same_place(pathlib.Path(entry), cwd)
    ]
    saved = list(sys.path)
    try:
        sys.path = stripped
        spec = importlib.util.find_spec("karox.web_check")
    except Exception:
        return None
    finally:
        sys.path = saved
    origin = getattr(spec, "origin", None) if spec is not None else None
    if not isinstance(origin, str) or not origin:
        return None
    try:
        other = pathlib.Path(origin).resolve()
    except OSError:
        return None
    if other == here:
        return None
    return str(other)

def _same_place(candidate: pathlib.Path, other: pathlib.Path) -> bool:
    try:
        return candidate.resolve() == other
    except OSError:
        return False
