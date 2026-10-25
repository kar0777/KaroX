---
name: repo-selfcheck
version: 1.0.0
description: Summarize the shape of a repository with the bundled script before changing code.
when_to_use: first contact with a repo, or when a change's blast radius is unclear.
triggers: []
scripts: ["scripts/repo_summary.py"]
---
# Repository self-check recipe

- Run the bundled script first: `skill.run(name="repo-selfcheck", script="scripts/repo_summary.py")`.
- Read the JSON it prints: file counts, line totals and the largest modules by directory.
- Use the largest modules to decide where to read before editing; do not open every file.
- The script runs in an isolated scratch directory; it only reads, never writes.
- Re-run it after a change to confirm the shape of the tree did not drift unexpectedly.
