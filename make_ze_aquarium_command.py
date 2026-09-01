from pathlib import Path

root = Path(__file__).resolve().parent
prompt = (root / "aquarium_benchmark_prompt_current.txt").read_text(encoding="utf-8")
command_dir = root / ".opencode" / "commands"
command_dir.mkdir(parents=True, exist_ok=True)
command = """---
description: Strict ZE Opus 5 aquarium implementation benchmark
agent: build
model: ze/claude-opus-5
---

IMPORTANT HARNESS RULE: You must implement the requested artifact in the current repository. Use Write/Edit tools to create or replace `aquarium-test.html`. A code block in chat alone is NOT completion. Verify the file exists before your final response.

""" + prompt + """

HARNESS OVERRIDE FOR DELIVERY: regardless of any “return only HTML” sentence above, the required delivery in this OpenCode benchmark is the repository file `aquarium-test.html`. Use file tools and finish only after it exists.
"""
out = command_dir / "ze-aquarium.md"
out.write_text(command, encoding="utf-8", newline="\n")
print(out)
print(out.stat().st_size)
