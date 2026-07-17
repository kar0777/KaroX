# KaroX v3.16.2 — Process-safe updates and stronger agent workflows

## Fixed

- Windows updates now stop verified live KaroX server, tunnel, and runner processes before replacing application files.
- The updater reads recorded session PIDs, validates their current command lines, and refuses to kill stale or unrelated reused PIDs.
- Orphaned KaroX Uvicorn and tunnel processes are discovered safely while the updater's own ancestor process chain is excluded.
- Installation retries up to four times after transient Windows directory-lock failures instead of aborting immediately on `app\server`.
- The public `karox stop [--session ID] [--json]` command can terminate session processes without loading the larger administrative runtime.

## Better AI work

- `karox_list_dir` lists a focused directory without loading the entire repository tree.
- `karox_search` searches source content and returns compact matching snippets.
- `karox_read_file_range` provides bounded, line-numbered context for large files.
- `karox_read_files` batches several context reads into one tool call.
- `karox_apply_edits` performs exact replacements and refuses stale or ambiguous edits before writing.
- `karox_run_checks` runs an ordered build/test/lint checklist and summarizes each result.
- Agent tools live in a separate module so capabilities can evolve without destabilizing the authenticated Streamable HTTP transport.

## Validation

- Product quality passed on Windows, macOS, and Linux with Python 3.10 and 3.12.
- Provider and release contracts passed.
- CodeQL passed.
- Regression coverage includes a real MCP initialize → tools/list → tools/call workflow, safe edits, search, range reads, process shutdown, and Windows PowerShell 5.1 parsing of the update guard.

Existing settings, sessions, repository history, and the persistent Notion URL/token are preserved.
