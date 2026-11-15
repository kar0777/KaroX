import asyncio

import pytest

from karox6.tools.todo import TodoTool


def run(tool, ctx, todos):
    return asyncio.run(tool.run({"todos": todos}, ctx))


def test_statuses(ctx):
    result = run(
        TodoTool(),
        ctx,
        [
            {"content": " a ", "status": "pending"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "completed"},
        ],
    )
    assert "[ ] a" in result.content
    assert "[~] b" in result.content
    assert "[x] c" in result.content
    assert "1/3" in result.content


def test_empty(ctx):
    assert "0/0" in run(TodoTool(), ctx, []).content


def test_default_status(ctx):
    assert "[ ] a" in run(TodoTool(), ctx, [{"content": "a"}]).content


@pytest.mark.parametrize("item", ["x", {"content": " "}, {"content": "x", "status": "bad"}])
def test_invalid_preserves_previous(ctx, item):
    tool = TodoTool()
    run(tool, ctx, [{"content": "old", "status": "completed"}])
    assert run(tool, ctx, [item]).is_error
    assert tool.items == [{"content": "old", "status": "completed"}]


def test_replace(ctx):
    tool = TodoTool()
    run(tool, ctx, [{"content": "old"}])
    run(tool, ctx, [{"content": "new"}])
    assert tool.items == [{"content": "new", "status": "pending"}]
