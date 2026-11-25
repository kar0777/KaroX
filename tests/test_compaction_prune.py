from karox6.context.compaction import PRUNE_MARKER, PRUNE_THRESHOLD, prune_tool_results
from karox6.core.messages import Message, ToolCall

BIG = "x" * (PRUNE_THRESHOLD + 5000)


def _batch(n, prefix):
    calls = [ToolCall(id=f"{prefix}{i}", name="read", arguments="{}") for i in range(n)]
    reply = Message("assistant", "", tool_calls=calls)
    return [reply, *[Message("tool", BIG, tool_call_id=c.id) for c in calls]]


def test_results_the_model_has_not_read_yet_are_never_pruned():
    messages = [Message("user", "task"), *_batch(2, "old"), *_batch(7, "new")]
    prune_tool_results(messages, keep_last=6)
    old, new = messages[2:4], messages[5:]
    assert all(PRUNE_MARKER in m.content for m in old)
    assert len(new) == 7
    assert all(m.content == BIG for m in new)


def test_the_emergency_cut_still_prunes_everything():
    messages = [Message("user", "task"), *_batch(3, "new")]
    assert prune_tool_results(messages, keep_last=0) > 0
    assert all(PRUNE_MARKER in m.content for m in messages[2:])
