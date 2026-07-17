"""Unit tests for KaroX 4.0 pure helpers (no server, no network).

Run: python scripts/test_karox4_units.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import repo_tools  # noqa: F401  (imports karox4 chain at module tail)
import karox4_exec
import karox4_files
import karox4_git

FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


# --------------------------------------------------------------------------
# normalize_bytes
# --------------------------------------------------------------------------

def test_normalize_bytes() -> None:
    utf8 = "Привет, world".encode("utf-8")
    check("normalize_bytes utf-8", karox4_exec.normalize_bytes(utf8) == "Привет, world")
    cp866 = "Привет мир".encode("cp866")
    check("normalize_bytes cp866", karox4_exec.normalize_bytes(cp866) == "Привет мир", repr(karox4_exec.normalize_bytes(cp866)))
    cp1251 = "Каталог файлов".encode("cp1251")
    decoded = karox4_exec.normalize_bytes(cp1251)
    check("normalize_bytes cp1251", decoded == "Каталог файлов", repr(decoded))
    check("normalize_bytes empty", karox4_exec.normalize_bytes(b"") == "")
    check("normalize_bytes none", karox4_exec.normalize_bytes(None) == "")
    check("normalize_bytes ascii", karox4_exec.normalize_bytes(b"hello") == "hello")


# --------------------------------------------------------------------------
# parse_errors
# --------------------------------------------------------------------------

def test_parse_errors() -> None:
    javac = "src/main/java/App.java:42: error: cannot find symbol\n        foo();\n"
    errors = karox4_exec.parse_errors(javac)
    check("parse_errors javac", len(errors) == 1 and errors[0]["file"].endswith("App.java") and errors[0]["line"] == 42)

    tsc = "src/app.ts(10,5): error TS2304: Cannot find name 'foo'.\n"
    errors = karox4_exec.parse_errors(tsc)
    check("parse_errors tsc", len(errors) == 1 and errors[0]["tool"] == "tsc" and errors[0]["line"] == 10)

    kotlin = "e: file:///D:/proj/src/Main.kt:7:13 Unresolved reference: bar\n"
    errors = karox4_exec.parse_errors(kotlin)
    check("parse_errors kotlin", len(errors) == 1 and errors[0]["line"] == 7, repr(errors))

    pytest_out = "tests/test_x.py:15: AssertionError: expected 1 got 2\n"
    errors = karox4_exec.parse_errors(pytest_out)
    check("parse_errors pytest", len(errors) == 1 and errors[0]["line"] == 15, repr(errors))

    eslint = "D:\\proj\\src\\index.js\n  12:3  error  Unexpected console statement  no-console\n"
    errors = karox4_exec.parse_errors(eslint)
    check("parse_errors eslint", any(e["tool"] == "eslint" and e["line"] == 12 for e in errors), repr(errors))

    gradle = "* What went wrong:\nExecution failed for task ':compileJava'.\n> Compilation failed\n"
    errors = karox4_exec.parse_errors(gradle)
    check("parse_errors gradle", any(e["tool"] == "gradle" for e in errors), repr(errors))

    clean = "BUILD SUCCESSFUL in 4s\n12 actionable tasks\n"
    check("parse_errors clean", karox4_exec.parse_errors(clean) == [])


# --------------------------------------------------------------------------
# secret scan (secrets assembled at runtime so this file never matches)
# --------------------------------------------------------------------------

def test_secret_scan() -> None:
    gh_token = "gh" + "p_" + "A" * 24
    text = f"line one\nTOKEN = \"{gh_token}\"\nline three\n"
    findings = karox4_git.scan_text_for_secrets(text)
    check("secret scan github token", len(findings) >= 1 and findings[0]["line"] == 2, repr(findings))

    aws = "AKIA" + "ABCDEFGHIJKLMNOP"
    findings = karox4_git.scan_text_for_secrets(f"key={aws}\n")
    check("secret scan aws", len(findings) == 1 and findings[0]["line"] == 1, repr(findings))

    pem = "-----BEGIN " + "RSA PRIVATE KEY-----"
    findings = karox4_git.scan_text_for_secrets(f"header\n{pem}\n")
    check("secret scan pem", len(findings) == 1 and findings[0]["line"] == 2, repr(findings))

    entropy_val = "aB3" + "xK9mQ2wE7rT5yU1iP0oL" + "sD4fG6hJ8"
    findings = karox4_git.scan_text_for_secrets(f'password = "{entropy_val}"\n')
    check("secret scan entropy", len(findings) >= 1, repr(findings))

    clean = "const port = 8080\nlet name = 'hello'\npassword = ''\n"
    check("secret scan clean", karox4_git.scan_text_for_secrets(clean) == [])

    check("shannon entropy low", karox4_git.shannon_entropy("aaaaaaaa") < 1.0)
    check("shannon entropy high", karox4_git.shannon_entropy("aB3xK9mQ2wE7rT5yU1iP") > 3.5)


# --------------------------------------------------------------------------
# split_hunks
# --------------------------------------------------------------------------

def test_split_hunks() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n"
        "index 111..222 100644\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        "+added1\n"
        " line2\n"
        "@@ -10,2 +11,3 @@\n"
        " line10\n"
        "+added2\n"
    )
    header, hunks = karox4_git.split_hunks(diff)
    check("split_hunks count", len(hunks) == 2, f"got {len(hunks)}")
    check("split_hunks header", header.startswith("diff --git") and "+++ b/x.py" in header)
    check("split_hunks hunk0", hunks[0].startswith("@@ -1,3") and "+added1" in hunks[0])
    check("split_hunks hunk1", hunks[1].startswith("@@ -10,2") and "+added2" in hunks[1])
    header2, hunks2 = karox4_git.split_hunks("")
    check("split_hunks empty", header2 == "" and hunks2 == [])


# --------------------------------------------------------------------------
# patch path parsing
# --------------------------------------------------------------------------

def test_patch_paths() -> None:
    patch = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
        "--- /dev/null\n"
        "+++ b/src/new_file.py\n"
        "@@ -0,0 +1 @@\n"
        "+new\n"
    )
    paths = karox4_files.patch_paths(patch)
    check("patch_paths finds both", "src/app.py" in paths and "src/new_file.py" in paths, repr(paths))
    check("patch_paths skips dev/null", "/dev/null" not in paths)


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

if __name__ == "__main__":
    test_normalize_bytes()
    test_parse_errors()
    test_secret_scan()
    test_split_hunks()
    test_patch_paths()
    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} test(s): {FAILED}")
        raise SystemExit(1)
    print("All karox4 unit tests passed.")
