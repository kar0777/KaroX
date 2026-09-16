#!/usr/bin/env python3
"""Validate documented ``karox`` commands against the real argparse builder.

The checker parses ``src/karox/cli.py`` as Python AST, reconstructs the command
and option tree created through ``add_subparsers``, ``add_parser``, and
``add_argument``, then validates executable command lines in canonical docs.
It imports no KaroX dependency and makes no network or provider call.

The goal is not to execute examples. It prevents documentation from inventing a
subcommand or option such as the removed/nonexistent ``migrate --dry-run``.
"""

from __future__ import annotations

import argparse
import ast
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CANONICAL_DOCS = (
    "README.md",
    "README_RU.md",
    "QUICKSTART.md",
    "SECURITY.md",
    "TROUBLESHOOTING.md",
    "CONTRIBUTING.md",
    "docs/V5_RELEASE_SCOPE.md",
    "docs/RELEASE_CHECKLIST.md",
    "docs/LIVE_TEST_RUNBOOK.md",
    "docs/TAILSCALE_LIVE_RUNBOOK.md",
    "docs/INSTALLER_REHEARSAL.md",
    "docs/MIGRATION_V4_TO_V5.md",
    "docs/CONNECTIVITY.md",
    "docs/IMPLEMENTATION_STATUS.md",
)

SHELL_META = ("|", "&&", ";", "$(", "`", ">", "<")
COMMAND_ALIASES = {"models": "model"}


@dataclass
class ParserContract:
    paths: set[tuple[str, ...]] = field(default_factory=lambda: {()})
    options: dict[tuple[str, ...], set[str]] = field(
        default_factory=lambda: {(): {"-h", "--help"}}
    )


class _BuilderInterpreter:
    """Interpret the small argparse-construction subset used by KaroX."""

    def __init__(self) -> None:
        self.contract = ParserContract()
        self.parsers: dict[str, tuple[str, ...]] = {}
        self.subparsers: dict[str, tuple[str, ...]] = {}
        self.constants: dict[str, str] = {}

    @staticmethod
    def _target_name(node: ast.AST) -> str | None:
        return node.id if isinstance(node, ast.Name) else None

    @staticmethod
    def _attribute_call(node: ast.AST, name: str) -> tuple[str, ast.Call] | None:
        if not isinstance(node, ast.Call):
            return None
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != name:
            return None
        if not isinstance(function.value, ast.Name):
            return None
        return function.value.id, node

    def _string_argument(self, call: ast.Call) -> str | None:
        if not call.args:
            return None
        value = call.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            return self.constants.get(value.id)
        return None

    def _record_parser_assignment(self, target: str, value: ast.AST) -> bool:
        if not isinstance(value, ast.Call):
            return False
        function = value.func
        if not isinstance(function, ast.Attribute):
            return False
        if function.attr == "ArgumentParser":
            self.parsers[target] = ()
            self.contract.paths.add(())
            return True
        if function.attr == "add_subparsers" and isinstance(function.value, ast.Name):
            owner = function.value.id
            if owner in self.parsers:
                self.subparsers[target] = self.parsers[owner]
                return True
        if function.attr == "add_parser" and isinstance(function.value, ast.Name):
            owner = function.value.id
            prefix = self.subparsers.get(owner)
            name = self._string_argument(value)
            if prefix is not None and name:
                path = prefix + (name,)
                self.parsers[target] = path
                self.contract.paths.add(path)
                self.contract.options.setdefault(path, {"-h", "--help"})
                return True
        return False

    def _record_argument_call(self, expression: ast.AST) -> None:
        matched = self._attribute_call(expression, "add_argument")
        if matched is None:
            return
        owner, call = matched
        path = self.parsers.get(owner)
        if path is None:
            return
        bucket = self.contract.options.setdefault(path, {"-h", "--help"})
        for argument in call.args:
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value.startswith("-")
            ):
                bucket.add(argument.value)

    def visit_statements(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = self._target_name(statement.targets[0])
                if target is not None:
                    self._record_parser_assignment(target, statement.value)
            elif isinstance(statement, ast.AnnAssign):
                target = self._target_name(statement.target)
                if target is not None and statement.value is not None:
                    self._record_parser_assignment(target, statement.value)
            elif isinstance(statement, ast.Expr):
                self._record_argument_call(statement.value)

            if isinstance(statement, ast.For) and isinstance(statement.target, ast.Name):
                values: list[str] = []
                if isinstance(statement.iter, (ast.Tuple, ast.List)):
                    for item in statement.iter.elts:
                        if isinstance(item, ast.Constant) and isinstance(item.value, str):
                            values.append(item.value)
                if values:
                    previous = self.constants.get(statement.target.id)
                    for value in values:
                        self.constants[statement.target.id] = value
                        self.visit_statements(statement.body)
                    if previous is None:
                        self.constants.pop(statement.target.id, None)
                    else:
                        self.constants[statement.target.id] = previous
                    self.visit_statements(statement.orelse)
                    continue

            nested: list[list[ast.stmt]] = []
            if isinstance(statement, (ast.If, ast.While)):
                nested.extend((statement.body, statement.orelse))
            elif isinstance(statement, ast.For):
                nested.extend((statement.body, statement.orelse))
            elif isinstance(statement, ast.Try):
                nested.extend((statement.body, statement.orelse, statement.finalbody))
                nested.extend(handler.body for handler in statement.handlers)
            elif isinstance(statement, ast.With):
                nested.append(statement.body)
            for body in nested:
                self.visit_statements(body)


def _build_contract(root: Path) -> ParserContract:
    source = (root / "src" / "karox" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builder = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_parser", "build_parser"}
        ),
        None,
    )
    if builder is None:
        raise ValueError("src/karox/cli.py has no _parser/build_parser function")
    interpreter = _BuilderInterpreter()
    interpreter.visit_statements(builder.body)

    # KaroX keeps the orchestration/intelligence command family in a separate
    # module so cli.py does not become one giant parser function. Interpret that
    # registered builder too instead of treating delegated parser construction as
    # nonexistent documentation. The function receives the root ``commands``
    # subparser collection, so seed that exact binding before walking its body.
    orchestration_path = root / "src" / "karox" / "orchestration_cli.py"
    orchestration_tree = ast.parse(orchestration_path.read_text(encoding="utf-8"))
    register = next(
        (
            node
            for node in orchestration_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "register_orchestration_commands"
        ),
        None,
    )
    if register is None:
        raise ValueError("orchestration_cli.py has no register_orchestration_commands")
    interpreter.subparsers["commands"] = ()
    interpreter.visit_statements(register.body)

    if len(interpreter.contract.paths) < 2:
        raise ValueError("argparse contract contains no subcommands")
    return interpreter.contract


def _documented_commands(text: str) -> list[tuple[int, str]]:
    commands: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not (
            line.startswith("karox ")
            or line == "karox"
            or line.startswith("karox-vnext ")
        ):
            continue
        if line.endswith("\\"):
            continue
        if any(marker in line for marker in SHELL_META):
            continue
        if "[--" in line or "..." in line or "SUBCOMMAND" in line:
            continue
        commands.append((number, line))
    return commands


def _matching_path(
    tokens: list[str], contract: ParserContract
) -> tuple[tuple[str, ...], int]:
    path: tuple[str, ...] = ()
    index = 0
    while index < len(tokens):
        token = COMMAND_ALIASES.get(tokens[index], tokens[index]) if index == 0 else tokens[index]
        candidate = path + (token,)
        if token.startswith("-") or candidate not in contract.paths:
            break
        path = candidate
        index += 1
    return path, index


def _allowed_options(path: tuple[str, ...], contract: ParserContract) -> set[str]:
    allowed: set[str] = set()
    for length in range(len(path) + 1):
        allowed.update(contract.options.get(path[:length], set()))
    return allowed


def _validate_command(command: str, contract: ParserContract) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        return [f"cannot parse command: {exc}"]
    if not tokens:
        return []
    executable = tokens.pop(0)
    if executable not in {"karox", "karox-vnext"}:
        return [f"unexpected executable {executable!r}"]

    path, consumed = _matching_path(tokens, contract)
    remaining = tokens[consumed:]
    first = COMMAND_ALIASES.get(tokens[0], tokens[0]) if tokens else ""
    if tokens and consumed == 0 and not first.startswith("-"):
        return [f"unknown top-level command {tokens[0]!r}"]

    allowed = _allowed_options(path, contract)
    problems: list[str] = []
    for token in remaining:
        option = token.split("=", 1)[0] if token.startswith("--") else token
        if option.startswith("-") and option not in allowed:
            display = " ".join(path) or "root"
            problems.append(f"unknown option {option!r} for {display}")
    return problems


def collect_problems(root: Path = ROOT) -> list[str]:
    try:
        contract = _build_contract(root)
    except (OSError, SyntaxError, ValueError) as exc:
        return [f"cannot build CLI contract: {type(exc).__name__}: {exc}"]

    problems: list[str] = []
    for relative in CANONICAL_DOCS:
        path = root / relative
        if not path.is_file():
            problems.append(f"canonical command document is missing: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for line, command in _documented_commands(text):
            for problem in _validate_command(command, contract):
                problems.append(f"{relative}:{line}: {problem}: {command}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    problems = collect_problems(ROOT)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    elif problems:
        print("documented KaroX command contract failed:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("canonical documented karox commands match the argparse contract")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
