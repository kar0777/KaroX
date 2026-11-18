import asyncio
import dataclasses

from karox6.tools.clones import copies_note, find_copies, replaced_windows
from karox6.tools.fs import EditTool

KAFKA = """class KafkaAckableMessage(KafkaMessage):
    async def nack(self) -> None:
        if not self.committed:
            raw_message = (
                self.raw_message[0]
                if isinstance(self.raw_message, tuple)
                else self.raw_message
            )
            topic_partition = AIOKafkaTopicPartition(
                raw_message.topic,
                raw_message.partition,
            )
            self.consumer.seek(partition=topic_partition, offset=raw_message.offset)
        await super().nack()
"""

CONFLUENT = """class KafkaMessage(StreamMessage):
    async def nack(self) -> None:
        \"\"\"Reject the Kafka message.\"\"\"
        if self.is_manual and not self.committed:
            raw_message: Message = (
                self.raw_message[0]
                if isinstance(self.raw_message, tuple)
                else self.raw_message
            )
            topic = raw_message.topic()
            partition = raw_message.partition()
        await super().nack()
"""

OLD = """            raw_message = (
                self.raw_message[0]
                if isinstance(self.raw_message, tuple)
                else self.raw_message
            )"""

NEW = """            raw_messages = (
                self.raw_message
                if isinstance(self.raw_message, tuple)
                else (self.raw_message,)
            )"""


def _repo(tmp_path):
    (tmp_path / "kafka").mkdir()
    (tmp_path / "confluent").mkdir()
    (tmp_path / "kafka" / "message.py").write_text(KAFKA.replace(OLD, NEW))
    (tmp_path / "confluent" / "message.py").write_text(CONFLUENT)
    return tmp_path


def test_sibling_copy_found(tmp_path):
    root = _repo(tmp_path)
    found = find_copies(root, root / "kafka" / "message.py", OLD, NEW)
    assert found == [("confluent/message.py", 6)]


def test_windows_need_a_changed_line():
    assert replaced_windows(OLD, OLD + "\n# note") == []
    assert replaced_windows(OLD, NEW)


def test_short_lines_do_not_make_a_copy(tmp_path):
    old = "    )\n    return x\nelse:\n    pass\n}"
    assert replaced_windows(old, "") == []
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text(old)
    assert find_copies(tmp_path, tmp_path / "a.py", old, "") == []


def test_import_blocks_are_not_copies():
    old = "import asyncio\nimport os\nimport shutil\nfrom pathlib import Path\n"
    assert replaced_windows(old, "import os\n") == []


def test_other_file_types_ignored(tmp_path):
    root = _repo(tmp_path)
    (root / "confluent" / "message.py").rename(root / "confluent" / "message.txt")
    assert find_copies(root, root / "kafka" / "message.py", OLD, NEW) == []


def test_second_copy_in_same_file(tmp_path):
    text = KAFKA.replace(OLD, NEW) + "\n\ndef other(self):\n" + OLD + "\n"
    (tmp_path / "m.py").write_text(text)
    found = find_copies(tmp_path, tmp_path / "m.py", OLD, NEW)
    assert [rel for rel, _ in found] == ["m.py"]


def test_one_location_per_copy(tmp_path):
    (tmp_path / "a.py").write_text("pass\n")
    long_old = "\n".join(f"value_{i} = compute_something({i}, flag)" for i in range(8))
    (tmp_path / "b.py").write_text(long_old + "\n")
    found = find_copies(tmp_path, tmp_path / "a.py", long_old, "")
    assert found == [("b.py", 1)]


def test_git_index_lists_tracked_files_nearest_first(tmp_path):
    import shutil
    import subprocess

    import pytest

    from karox6.tools.clones import _candidates

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    for rel in ("a/b/x.py", "a/b/y.py", "a/z.py", "c/d/e/w.py", "a/b/n.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "a", "c/d/e/w.py"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.py").write_text("x = 1\n")
    import time

    got = _candidates(tmp_path, tmp_path / "a/b/x.py", ".py", time.monotonic() + 5)
    assert got == ["a/b/x.py", "a/b/y.py", "a/z.py", "c/d/e/w.py"]


def test_budget_exhausted_reports_nothing(tmp_path):
    root = _repo(tmp_path)
    assert find_copies(root, root / "kafka" / "message.py", OLD, NEW, budget_s=-1) == []


def test_note_lists_each_place_once():
    seen: set[str] = set()
    first = copies_note([("a.py", 3), ("b.py", 9)], seen)
    assert "a.py:3, b.py:9" in first
    assert copies_note([("a.py", 3)], seen) == ""
    many = copies_note([(f"f{i}.py", 1) for i in range(8)], set())
    assert "and 3 more" in many


def _edit(ctx, **args):
    return asyncio.run(EditTool().run(args, ctx))


def test_edit_reports_copies_only_when_enabled(ctx):
    _repo(ctx.workspace)
    (ctx.workspace / "kafka" / "message.py").write_text(KAFKA)
    off = _edit(ctx, file_path="kafka/message.py", old_string=OLD, new_string=NEW)
    assert not off.is_error
    assert "also appears" not in off.content

    (ctx.workspace / "kafka" / "message.py").write_text(KAFKA)
    on_ctx = dataclasses.replace(ctx, copy_notes=True)
    on = _edit(on_ctx, file_path="kafka/message.py", old_string=OLD, new_string=NEW)
    assert "also appears at confluent/message.py:6" in on.content
    (ctx.workspace / "kafka" / "message.py").write_text(KAFKA)
    again = _edit(on_ctx, file_path="kafka/message.py", old_string=OLD, new_string=NEW)
    assert "also appears" not in again.content  # already reported in this run
