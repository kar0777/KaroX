"""ClickUp OAuth approval navigation compatibility tests."""

from __future__ import annotations

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.oauth_bridge import _form_action


def test_clickup_approval_form_posts_only_to_karox() -> None:
    policy = _form_action("https://karox.example")

    assert policy == "'self' https://karox.example"
    assert "clickup" not in policy
    assert "*" not in policy


def test_form_action_uses_the_exact_public_origin() -> None:
    policy = _form_action("https://monsterpc.taila81286.ts.net")

    assert policy == "'self' https://monsterpc.taila81286.ts.net"
    assert "http:" not in policy
