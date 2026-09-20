# 🚀 KaroX 5.0.0rc3 — Public Beta / Публичная бета

<div align="center">

**One local runtime for every AI coding agent. Your models can change. KaroX remembers.**

![Channel](https://img.shields.io/badge/channel-public_beta-f59e0b)
![Runtime](https://img.shields.io/badge/runtime-5.0.0rc3-2563eb)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-64748b)

</div>

`5.0.0rc3` is the third public **KaroX 5 beta release candidate**. It carries the
independently validated runtime candidate from the 2026-09-20 validation pass
plus every platform fix that candidate needed to go green on the full
Windows/macOS/Linux CI matrix (Python 3.10–3.14).

`5.0.0rc3` — третий публичный **release candidate KaroX 5**: проверенный кандидат
времени выполнения и все платформенные исправления, потребовавшиеся ему для
зелёного прогона полной CI-матрицы.

## What's inside / Что внутри

- **Runtime candidate (validated)**: affected test selection no longer
  truncates after 200 targets; journal/event store got an SQLite index,
  transactional id allocation and batched log writes; hot reload publishes
  modules under import locks with rollback; project fact hashing reuses one
  bytes snapshot. +131 test cases across 23 files.
- **CI platform fixes**: path-containment checks now resolve the repository
  base first (8.3 short names on Windows runners, symlinked `/var` on macOS
  no longer make every relative path look like a scope violation); the
  literal-arrow identity regression pins `Path.is_file` explicitly because
  Python 3.14 rewrote pathlib.
- **Installability**: same one-command install as rc2
  (`pipx install "karox-runtime==5.0.0rc3"` or
  `uv tool install --prerelease=allow karox-runtime`).

Полный отчёт валидации — `ADAPT_VALIDATION_REPORT.md`; контроль качества —
Ruff, mypy, 12/12 статических проверок, полный набор Windows/macOS/Linux.

Stable `5.0.0` still waits for the remaining live conformance, external-beta
and install/upgrade evidence recorded in `docs/RELEASE_CHECKLIST.md`.
