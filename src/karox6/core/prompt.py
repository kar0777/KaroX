"""System prompt: short (<= 600 words), general, no task- or library-specific rules.

Every rule here has to earn its place in an A/B run (DECISIONS.md D-021). Tool usage
details live in the tool descriptions, as in DSH.
"""

from __future__ import annotations

import configparser
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from karox6.tools.shell import ShellInfo

BASE_PROMPT = """You are KaroX, an autonomous software engineering agent. You work in the user's project through tools: you run shell commands and read, search and edit files. Nobody is available to answer questions during the run, so carry the task through to a verified result on your own. When something is ambiguous, choose the most reasonable interpretation consistent with the codebase and continue.

# Explore and understand
- What the task states is exact: required names, signatures, return shapes, accepted and rejected types, and what must be preserved or excluded. Its examples are samples: other forms of the same input and other paths to the same behaviour belong to the task unless it rules them out.
- Read the relevant code, its callers, neighbouring tests and the build/test configuration to find the root cause, confirm the signatures and libraries you will use, and learn how this project runs its checks.
- When output must match something existing (a schema, another function's output, a format), inspect that source and compare against its real output, not a copy you wrote by hand.

# Make the change
- Fix the cause, not only the reported example, where every affected input passes through, in the structure and style of the surrounding code. Leave behaviour the task does not concern as it is: no unrelated refactors, speculative abstractions, planning files in the repository, or new dependencies unless required.
- Never make a check pass by deleting, skipping or weakening a test, suppressing a type error, loosening configuration, or rewriting a docstring to match faulty code. A failing test usually points at your code.

# Verify
- Start with the narrowest relevant check (a reproduction or the closest test), then run the project's build or type check and the broader related tests. Check the [exit code: N] marker of every command.
- Passing the task's example proves only that example. Then try a few inputs the task did not spell out (another form of the same input, a boundary, an input that must be rejected, another path to the same code) in one throwaway command, and fix what fails for the same cause. Take expected values from the task or the real source of truth, not from your implementation.
- Whether to add tests is the task's and the project's call; if you do, give new test functions distinctive names so they cannot collide with other test suites. Delete temporary scripts before finishing.
- If the same fix fails three times, stop repeating it: read the full error, recheck the task's constraints, and change approach.

# Efficiency and safety
- Every step re-sends the whole conversation, so prefer fewer, fuller steps: issue independent read, grep and glob calls together, combine related shell commands, read whole relevant sections at once, and do not re-read a file just to confirm an edit that succeeded.
- Economy never justifies a partial solution: work through recoverable errors and timeouts, and keep going after the context is summarised.
- Solve the task from the project in front of you; do not hunt for a ready-made fix in package caches, other installed copies of the code, or unreachable git objects.
- Never discard existing work (git reset --hard, checkout of files you did not change, clean) and do not commit unless the task asks for it.

When the task is complete and verified, review your final diff for accidental changes, then stop calling tools and reply with a short summary of what you changed and how you verified it. If something could not be done or still fails, say so with the evidence."""  # noqa: E501

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "KAROX.md")
INSTRUCTIONS_MAX_CHARS = 16_000


def build_system_prompt(
    workspace: Path, shell: ShellInfo, *, base: str | None = None, append: str | None = None
) -> str:
    parts = [base or BASE_PROMPT]
    if append and append.strip():
        parts.append(append.strip())
    parts.append(environment_block(workspace, shell))
    instructions = project_instructions(workspace)
    if instructions:
        parts.append(instructions)
    return "\n\n".join(parts)


def environment_block(workspace: Path, shell: ShellInfo) -> str:
    shell_name = {
        "git-bash": "bash (Git Bash on Windows)",
        "bash": "bash",
        "sh": "sh",
        "pwsh": "PowerShell 7 (pwsh)",
        "powershell": "Windows PowerShell",
    }.get(shell.flavor, shell.flavor)
    lines = [
        "Environment:",
        f"- Working directory: {workspace}",
        f"- Platform: {platform.system()} {platform.release()}; shell tool: {shell_name}",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        f"- {git_summary(workspace)}",
    ]
    hint = python_hint()
    if hint:
        lines.append(f"- {hint}")
    lines.extend(f"- {item}" for item in project_inventory(workspace))
    lines.extend(f"- {item}" for item in environment_preflight(workspace))
    return "\n".join(lines)


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?![:=])", re.M)
_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
)


def project_inventory(workspace: Path, max_items: int = 12) -> list[str]:
    """What the project declares at its top level: build files, script names, make
    targets and a local virtualenv. Files are only read, nothing is run, and no command is
    presented as "the" test command - the model still decides what to run."""
    out: list[str] = []

    def text(name: str) -> str | None:
        return _read_project_text(workspace, name)

    def names(items: list[str]) -> str:
        shown = ", ".join(i if len(i) <= 40 else i[:40] + "..." for i in items[:max_items])
        return shown + (f" (+{len(items) - max_items} more)" if len(items) > max_items else "")

    py = [
        n for n in ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini") if text(n)
    ]
    if py:
        pyproject = text("pyproject.toml") or ""
        pytest_cfg = "[tool.pytest" in pyproject or text("pytest.ini") is not None
        note = "; pytest configured" if pytest_cfg else ""
        out.append(f"Python project files: {', '.join(py)}{note}")
    for venv in (".venv", "venv"):
        for exe in ("bin/python", "Scripts/python.exe"):
            if (workspace / venv / exe).is_file():
                out.append(f"Project virtualenv: {venv}/{exe}")
                break
        else:
            continue
        break
    package = text("package.json")
    if package is not None:
        try:
            scripts = list((json.loads(package).get("scripts") or {}).keys())
        except (ValueError, AttributeError):
            scripts = []
        manager = next((m for f, m in _LOCKFILES if (workspace / f).is_file()), None)
        via = f" (lockfile: {manager})" if manager else ""
        listed = f"; scripts: {names(scripts)}" if scripts else ""
        out.append(f"package.json{via}{listed}")
    go_mod = text("go.mod")
    if go_mod is not None:
        module = re.search(r"^module\s+(\S+)", go_mod, re.M)
        out.append(f"Go module: {module.group(1) if module else 'go.mod'}")
    if text("Cargo.toml") is not None:
        out.append("Rust crate: Cargo.toml")
    for name in ("Makefile", "makefile", "justfile"):
        body = text(name)
        if body is not None:
            targets = [t for t in dict.fromkeys(_MAKE_TARGET.findall(body)) if t != "PHONY"]
            out.append(f"{name} targets: {names(targets)}" if targets else name)
            break
    return [line if len(line) <= 400 else line[:400] + "..." for line in out]


PREFLIGHT_BUDGET_S = 3.0
PREFLIGHT_DEPS_TIMEOUT_S = 2.0
PREFLIGHT_MAX_LINE_CHARS = 198  # leaves room for the "- " bullet within 200 chars
PREFLIGHT_MAX_MISSING_DEPS = 8

_PY_TOOL_CHECKERS = (
    "ruff",
    "mypy",
    "black",
    "isort",
    "flake8",
    "pylint",
    "pytest",
    "pyright",
    "tox",
)
_STANDALONE_CHECKER_FILES: tuple[tuple[str, str], ...] = (
    ("ruff.toml", "ruff"),
    (".ruff.toml", "ruff"),
    ("mypy.ini", "mypy"),
    (".mypy.ini", "mypy"),
    (".flake8", "flake8"),
    (".pylintrc", "pylint"),
    ("pylintrc", "pylint"),
    ("pytest.ini", "pytest"),
    (".pytest.ini", "pytest"),
    (".isort.cfg", "isort"),
    ("pyrightconfig.json", "pyright"),
    ("tox.ini", "tox"),
    ("noxfile.py", "nox"),
)
_JS_DEVDEP_CHECKERS: dict[str, str] = {
    "eslint": "eslint",
    "typescript": "tsc",
    "tsc": "tsc",
    "prettier": "prettier",
    "jest": "jest",
    "vitest": "vitest",
}
_JS_SCRIPT_CHECKER_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(eslint|tsc|prettier|jest|vitest)(?:\.(?:cmd|exe|ps1))?(?![A-Za-z0-9_.-])"
)
_TEST_DEP_GROUPS = frozenset({"test", "tests", "testing", "dev"})
_PEP503_SEP = re.compile(r"[-_.]+")
_REQ_NAME_RE = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*(?:@|[=<>!~(]|$)"
)
_SIMPLE_OS_MARKER_RE = re.compile(
    r"^\s*(sys_platform|os_name|platform_system)\s*(==|!=)\s*['\"]([^'\"]+)['\"]\s*$"
)
_PROBE_DIST_SCRIPT = (
    "import sys\n"
    "sys.path = [p for p in sys.path if p not in ('', '.')]\n"
    "import importlib.metadata as m, json, re\n"
    "norm = lambda s: re.sub(r'[-_.]+', '-', s).lower()\n"
    "have = set()\n"
    "for d in m.distributions():\n"
    "    try:\n"
    "        name = d.metadata['Name'] if d.metadata else None\n"
    "    except Exception:\n"
    "        name = None\n"
    "    if name:\n"
    "        have.add(norm(name))\n"
    "want = json.loads(sys.stdin.read())\n"
    "print(json.dumps([n for n in want if norm(n) not in have]))\n"
)


def _read_project_text(workspace: Path, name: str) -> str | None:
    path = workspace / name
    try:
        if path.is_file() and path.stat().st_size < 512 * 1024:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return None


def _read_pyproject(workspace: Path) -> dict[str, object] | None:
    raw = _read_project_text(workspace, "pyproject.toml")
    if raw is None:
        return None
    try:
        data = tomllib.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _normalize_dist_name(name: str) -> str:
    return _PEP503_SEP.sub("-", name).lower()


def _ini_section_checker(section: str) -> str | None:
    sec = section.strip().lower()
    if sec in ("tool:pytest", "pytest"):
        return "pytest"
    if sec == "flake8" or sec.startswith("flake8:"):
        return "flake8"
    if sec == "mypy" or sec.startswith("mypy-"):
        return "mypy"
    if sec == "isort":
        return "isort"
    if sec == "pylint" or sec.startswith("pylint."):
        return "pylint"
    if sec == "ruff" or sec.startswith("ruff."):
        return "ruff"
    if sec == "black":
        return "black"
    if sec in ("tox", "testenv") or sec.startswith(("tox:", "testenv:")):
        return "tox"
    if sec == "pyright":
        return "pyright"
    return None


def _configured_checkers(workspace: Path, pyproject: dict[str, object] | None = None) -> list[str]:
    found: list[str] = []

    if pyproject is None:
        pyproject = _read_pyproject(workspace)
    if pyproject is not None:
        tool = pyproject.get("tool")
        if isinstance(tool, dict):
            for key in tool:
                if isinstance(key, str) and key in _PY_TOOL_CHECKERS:
                    found.append(key)

    for ini_name in ("setup.cfg", "tox.ini", ".flake8"):
        body = _read_project_text(workspace, ini_name)
        if body is None:
            continue
        parser = configparser.RawConfigParser(strict=False)
        try:
            parser.read_string(body)
        except configparser.Error:
            continue
        for sec in parser.sections():
            checker = _ini_section_checker(sec)
            if checker is not None:
                found.append(checker)

    for filename, checker in _STANDALONE_CHECKER_FILES:
        try:
            if (workspace / filename).is_file():
                found.append(checker)
        except OSError:
            continue

    package_raw = _read_project_text(workspace, "package.json")
    if package_raw is not None:
        try:
            pkg = json.loads(package_raw)
        except ValueError:
            pkg = None
        if isinstance(pkg, dict):
            dev_deps = pkg.get("devDependencies")
            if isinstance(dev_deps, dict):
                for dep_name in dev_deps:
                    if isinstance(dep_name, str) and dep_name in _JS_DEVDEP_CHECKERS:
                        found.append(_JS_DEVDEP_CHECKERS[dep_name])
            scripts = pkg.get("scripts")
            if isinstance(scripts, dict):
                for cmd in scripts.values():
                    if isinstance(cmd, str):
                        for match in _JS_SCRIPT_CHECKER_RE.finditer(cmd):
                            found.append(match.group(1))

    return list(dict.fromkeys(found))


def _is_checker_available(workspace: Path, name: str) -> bool:
    for venv in (".venv", "venv"):
        for subdir in ("bin", "Scripts"):
            base = workspace / venv / subdir
            for candidate in (name, f"{name}.exe", f"{name}.cmd", f"{name}.bat"):
                try:
                    if (base / candidate).is_file():
                        return True
                except OSError:
                    continue
    node_bin = workspace / "node_modules" / ".bin"
    for candidate in (name, f"{name}.cmd", f"{name}.exe", f"{name}.ps1", f"{name}.bat"):
        try:
            if (node_bin / candidate).is_file():
                return True
        except OSError:
            continue
    try:
        return shutil.which(name) is not None
    except OSError:
        return False


def _join_within(items: list[str], max_chars: int, *, force_ellipsis: bool = False) -> str:
    if not items or max_chars <= 0:
        return ""
    if not force_ellipsis:
        full = ", ".join(items)
        if len(full) <= max_chars:
            return full
    kept: list[str] = []
    for item in items:
        candidate = ", ".join([*kept, item]) + ", ..."
        if len(candidate) <= max_chars:
            kept.append(item)
        else:
            break
    if kept:
        return ", ".join(kept) + ", ..."
    if max_chars <= 3:
        return "." * max_chars
    return items[0][: max_chars - 3] + "..."


def _format_checkers_line(
    available: list[str],
    not_installed: list[str],
    *,
    max_chars: int = PREFLIGHT_MAX_LINE_CHARS,
) -> str | None:
    if not available and not not_installed:
        return None
    suffix = " (do not install; skip or note it)"
    if available and not not_installed:
        prefix = "Checkers configured by the project: available: "
        return prefix + _join_within(available, max_chars - len(prefix))
    if not_installed and not available:
        prefix = "Checkers configured by the project: not installed: "
        return prefix + _join_within(not_installed, max_chars - len(prefix) - len(suffix)) + suffix

    prefix = "Checkers configured by the project: available: "
    mid = "; not installed: "
    budget = max_chars - len(prefix) - len(mid) - len(suffix)
    avail_full = ", ".join(available)
    miss_full = ", ".join(not_installed)
    if len(avail_full) + len(miss_full) <= budget:
        return f"{prefix}{avail_full}{mid}{miss_full}{suffix}"
    half = budget // 2
    if len(avail_full) <= half:
        avail_budget = len(avail_full)
        miss_budget = budget - avail_budget
    elif len(miss_full) <= half:
        miss_budget = len(miss_full)
        avail_budget = budget - miss_budget
    else:
        avail_budget = half
        miss_budget = budget - half
    avail_str = _join_within(available, avail_budget)
    miss_str = _join_within(not_installed, miss_budget)
    return f"{prefix}{avail_str}{mid}{miss_str}{suffix}"


def _os_marker_applies(marker: str) -> bool:
    match = _SIMPLE_OS_MARKER_RE.match(marker)
    if not match:
        return True
    var, op, expected = match.groups()
    actual = {
        "sys_platform": sys.platform,
        "os_name": os.name,
        "platform_system": platform.system(),
    }[var]
    return actual == expected if op == "==" else actual != expected


def _parse_requirement_name(raw: str, *, self_name: str | None = None) -> str | None:
    line = re.sub(r"(?:^|\s)#.*$", "", raw).strip()
    if not line or line.startswith(("-", ".", "/", "\\")):
        return None
    req_part, _, marker_part = line.partition(";")
    req_part = req_part.strip()
    if not req_part or req_part.lower().endswith((".whl", ".tar.gz", ".zip")):
        return None
    if marker_part and not _os_marker_applies(marker_part):
        return None
    match = _REQ_NAME_RE.match(req_part)
    if not match:
        return None
    norm = _normalize_dist_name(match.group(1))
    if self_name and norm == self_name and "[" in req_part:
        return None
    return norm


def _iter_req_file_lines(body: str) -> list[str]:
    merged: list[str] = []
    buf = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            merged.append(buf + stripped)
            buf = ""
    if buf:
        merged.append(buf)
    return merged


def _declared_test_deps(workspace: Path, pyproject: dict[str, object] | None = None) -> list[str]:
    deps: list[str] = []
    if pyproject is None:
        pyproject = _read_pyproject(workspace)

    self_name: str | None = None
    if pyproject is not None:
        project = pyproject.get("project")
        if isinstance(project, dict):
            raw_name = project.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                self_name = _normalize_dist_name(raw_name.strip())
            opt = project.get("optional-dependencies")
            if isinstance(opt, dict):
                for key, val in opt.items():
                    if (
                        isinstance(key, str)
                        and key.lower() in _TEST_DEP_GROUPS
                        and isinstance(val, list)
                    ):
                        for item in val:
                            if isinstance(item, str):
                                parsed = _parse_requirement_name(item, self_name=self_name)
                                if parsed:
                                    deps.append(parsed)

        dep_groups = pyproject.get("dependency-groups")
        if isinstance(dep_groups, dict):
            norm_groups: dict[str, list[object]] = {
                _normalize_dist_name(k): v
                for k, v in dep_groups.items()
                if isinstance(k, str) and isinstance(v, list)
            }

            def collect_group(group_key: str, seen: set[str]) -> None:
                if group_key in seen or group_key not in norm_groups:
                    return
                seen.add(group_key)
                for entry in norm_groups[group_key]:
                    if isinstance(entry, str):
                        parsed = _parse_requirement_name(entry, self_name=self_name)
                        if parsed:
                            deps.append(parsed)
                    elif isinstance(entry, dict):
                        inc = entry.get("include-group")
                        if isinstance(inc, str):
                            collect_group(_normalize_dist_name(inc), seen)

            for key in dep_groups:
                if isinstance(key, str) and key.lower() in _TEST_DEP_GROUPS:
                    collect_group(_normalize_dist_name(key), set())

    setup_cfg = _read_project_text(workspace, "setup.cfg")
    if setup_cfg is not None:
        parser = configparser.RawConfigParser(strict=False)
        try:
            parser.read_string(setup_cfg)
        except configparser.Error:
            pass
        else:
            if parser.has_section("options.extras_require"):
                for key, val in parser.items("options.extras_require"):
                    if key.lower() in _TEST_DEP_GROUPS:
                        for line in _iter_req_file_lines(val):
                            parsed = _parse_requirement_name(line, self_name=self_name)
                            if parsed:
                                deps.append(parsed)

    req_files: list[Path] = []
    for pattern in (
        "requirements-test*.txt",
        "requirements-dev*.txt",
        "test-requirements.txt",
        "dev-requirements.txt",
    ):
        try:
            req_files.extend(sorted(workspace.glob(pattern), key=lambda p: p.name))
        except OSError:
            continue
    for req_path in dict.fromkeys(req_files):
        body = _read_project_text(workspace, req_path.name)
        if body is None:
            continue
        for line in _iter_req_file_lines(body):
            parsed = _parse_requirement_name(line, self_name=self_name)
            if parsed:
                deps.append(parsed)

    return list(dict.fromkeys(deps))


def _project_python(workspace: Path) -> str | None:
    is_win = platform.system() == "Windows"
    rel_exes = (
        (
            "Scripts/python.exe",
            "Scripts/python.cmd",
            "Scripts/python.bat",
            "bin/python",
            "bin/python3",
        )
        if is_win
        else ("bin/python", "bin/python3", "Scripts/python.exe")
    )
    for venv in (".venv", "venv"):
        for rel in rel_exes:
            candidate = workspace / venv / rel
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue

    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        env_root = os.environ.get(env_var)
        if not env_root:
            continue
        root = Path(env_root)
        env_exes = (
            ("Scripts/python.exe", "python.exe", "bin/python")
            if is_win
            else ("bin/python", "bin/python3")
        )
        for rel in env_exes:
            candidate = root / rel
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue

    names = ("python", "python3") if is_win else ("python3", "python")
    fallback: str | None = None
    for name in names:
        try:
            found = shutil.which(name)
        except OSError:
            found = None
        if not found:
            continue
        if is_win and "\\windowsapps\\" in found.lower().replace("/", "\\"):
            fallback = fallback or found
            continue
        return found
    return fallback


def _probe_missing_deps(python_exe: str, deps: list[str], *, timeout: float) -> list[str] | None:
    if not deps or timeout <= 0:
        return None
    try:
        proc = subprocess.run(
            [python_exe, "-c", _PROBE_DIST_SCRIPT],
            input=json.dumps(deps).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        try:
            parsed = json.loads(lines[-1])
        except ValueError:
            return None
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        return None
    allowed = set(deps)
    return list(
        dict.fromkeys(norm for item in parsed if (norm := _normalize_dist_name(item)) in allowed)
    )


def _format_missing_deps_line(
    missing: list[str],
    *,
    max_items: int = PREFLIGHT_MAX_MISSING_DEPS,
    max_chars: int = PREFLIGHT_MAX_LINE_CHARS,
) -> str | None:
    if not missing:
        return None
    prefix = "Declared test dependencies not installed: "
    suffix = " (tests needing them will fail or skip; do not install unless the task asks)"
    budget = max_chars - len(prefix) - len(suffix)
    capped = missing[:max_items]
    body = _join_within(capped, budget, force_ellipsis=len(missing) > max_items)
    if not body:
        return None
    return f"{prefix}{body}{suffix}"


def environment_preflight(workspace: Path, *, budget_s: float = PREFLIGHT_BUDGET_S) -> list[str]:
    """One-shot environment preflight: configured checkers and missing declared test deps.

    Bounded by ``budget_s`` with hard subprocess timeouts; any failure is silently omitted.
    """
    deadline = time.monotonic() + max(0.0, budget_s)
    out: list[str] = []
    pyproject = _read_pyproject(workspace)

    try:
        checkers = _configured_checkers(workspace, pyproject)
        if checkers:
            avail: list[str] = []
            missing_checkers: list[str] = []
            for checker in checkers:
                if _is_checker_available(workspace, checker):
                    avail.append(checker)
                else:
                    missing_checkers.append(checker)
            line = _format_checkers_line(avail, missing_checkers)
            if line:
                out.append(line)
    except Exception:
        pass

    try:
        if time.monotonic() < deadline:
            deps = _declared_test_deps(workspace, pyproject)
            if deps:
                python_exe = _project_python(workspace)
                remaining = deadline - time.monotonic()
                if python_exe and remaining > 0.05:
                    missing_deps = _probe_missing_deps(
                        python_exe,
                        deps,
                        timeout=min(PREFLIGHT_DEPS_TIMEOUT_S, remaining),
                    )
                    if missing_deps:
                        line = _format_missing_deps_line(missing_deps)
                        if line:
                            out.append(line)
    except Exception:
        pass

    return out


def python_hint() -> str | None:
    """On Windows `python3` is often the Microsoft Store placeholder: it prints only
    "Python" and exits with code 49, which agents misread and retry."""
    if platform.system() != "Windows":
        return None

    def kind(name: str) -> str:
        path = shutil.which(name)
        if not path:
            return "missing"
        if "\\windowsapps\\" not in path.lower().replace("/", "\\"):
            return "real"
        # WindowsApps holds both the placeholder and a real Store install: ask it.
        return "real" if _answers_version(path) else "stub"

    if kind("python3") != "stub":
        return None
    placeholder = "`python3` here is only the Microsoft Store placeholder (exit code 49)"
    for name in ("python", "py"):
        if kind(name) == "real":
            return f"Python: run it as `{name}`; {placeholder}"
    return f"Python: not installed; {placeholder}"


def _answers_version(path: str) -> bool:
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and b"Python 3" in proc.stdout + proc.stderr


def git_summary(workspace: Path) -> str:
    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip()

    if git("rev-parse", "--is-inside-work-tree") != "true":
        return "Git: not a git repository"
    branch = git("branch", "--show-current") or "(detached HEAD)"
    status = git("status", "--porcelain=v1") or ""
    dirty = len([ln for ln in status.splitlines() if ln.strip()])
    state = f"{dirty} changed/untracked paths" if dirty else "clean working tree"
    return f"Git: repository on branch {branch}, {state}"


def project_instructions(workspace: Path) -> str:
    """AGENTS.md / CLAUDE.md / KAROX.md from the workspace (and its git root), bounded."""
    roots = [workspace]
    git_root = _git_root(workspace)
    if git_root is not None and git_root != workspace:
        roots.insert(0, git_root)
    seen: set[str] = set()
    chunks: list[str] = []
    budget = INSTRUCTIONS_MAX_CHARS
    for root in roots:
        for name in INSTRUCTION_FILES:
            path = root / name
            try:
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            if len(text) > budget:
                text = text[:budget] + "\n[... truncated ...]"
            budget -= len(text)
            chunks.append(f'<instructions file="{path}">\n{text}\n</instructions>')
            if budget <= 0:
                break
    if not chunks:
        return ""
    return (
        "Project instructions from the repository (follow them unless they conflict with the "
        "task):\n" + "\n".join(chunks)
    )


def _git_root(workspace: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.decode("utf-8", "replace").strip())
