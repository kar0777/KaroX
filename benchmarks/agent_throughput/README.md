# KaroX Agent Throughput Benchmark

This benchmark is intentionally isolated from the live ChatGPT bridge. It uses
disposable temporary Git repositories populated from the same RelayDesk fixture
used by `benchmarks/gpt_web_autonomy/real_chatgpt_benchmark.py`.

It measures context-acquisition throughput for five representative tasks:

- task 01: find implementation + direct tests;
- task 02: trace a TUI/service/runtime call flow;
- task 03: diagnose a focused boundary bug;
- task 05: orient for a multi-file model/service/runtime change;
- task 06: orient for a duplicated-helper refactor.

For each task it compares:

1. `manual_low_level`: one `repo.search` plus multiple `repo.read_file` calls;
2. `execute_plan`: the same search/read work behind one conceptual MCP round-trip;
3. `repo_inspect_cold`: one focused repository inspection with a cold cache;
4. `repo_inspect_warm`: the same inspection immediately replayed from cache.

The report separates conceptual MCP round-trips from internal server-side tool
calls. It also records wall time, result JSON bytes, artifact bytes, delegate
tool time, and time spent in strict/fast repository identity checks.

Two repository shapes are supported:

- `clean`: only the committed fixture;
- `dirty`: the same fixture plus a wholly untracked generated directory, which
  stresses the metadata fingerprint without changing production fixture files.

The benchmark itself never starts/restarts a bridge, mutates saved profiles,
uses OAuth, publishes, pushes Git, or edits the live KaroX repository as a
fixture.

Example standalone run:

```text
python benchmarks/agent_throughput/throughput_benchmark.py --repeats 3 --scenario both --compact
```

The explicit pytest runner is `throughput_smoke.py`. Its filename intentionally
does not begin with `test_`, so normal test discovery does not execute the
performance workload.
