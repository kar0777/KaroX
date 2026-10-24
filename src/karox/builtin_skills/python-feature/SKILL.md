---
name: python-feature
version: 1.0.0
description: Add a feature to a Python project together with tests.
when_to_use: new function, CLI option, endpoint or behavior in a Python repo.
triggers: ["*.py", pyproject, pytest]
---
# Python feature recipe
- Find where similar features live (repo_search for a neighbor) and copy its structure, naming and error style.
- Write the smallest working path first, then edge cases. Keep public signatures stable unless the task changes them.
- Type hints and docstrings only where the surrounding code has them.
- Tests next to the existing tests of that module, in the same style (pytest or unittest). Cover the normal case, one edge case and one error case.
- Run the new tests, then the module's test file. If the repo has ruff/mypy in its checks, keep them clean.
- Do not add dependencies unless the task allows it.
