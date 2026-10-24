---
name: repo-orientation
version: 1.0.0
description: Get oriented in an unfamiliar or large repository quickly and cheaply.
when_to_use: first contact with a big repo, "where is X handled", unclear entry points.
triggers: []
---
# Orientation recipe
- Start with repo_inspect (focused) for the goal. Read its result before opening files.
- Read the README and the build/test config (pyproject, package.json, Makefile) in one batch.
- Find entry points by search (main, cli, app, route names), not by listing every directory.
- Read line ranges around the hits, not whole files. Stop exploring when you can name the files you will change.
- Skip generated and vendored code (.tmp, dist, build, node_modules, lock files).
