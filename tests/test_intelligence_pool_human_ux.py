from __future__ import annotations

from karox.orchestration_presenter import render_intelligence_list


def _endpoints() -> list[dict[str, object]]:
    return [
        {
            "endpoint_id": "subscription:claude:opaque-internal-id",
            "display_name": "Claude subscription",
            "source_kind": "subscription",
            "already_paid": True,
            "enabled": True,
            "quota": {"remaining_fraction": 0.68},
            "roles": ["reviewer", "orchestrator"],
            "capabilities": ["reasoning", "code"],
        },
        {
            "endpoint_id": "api:openrouter:glm-5.2",
            "display_name": "GLM 5.2",
            "source_kind": "api",
            "already_paid": False,
            "enabled": True,
            "roles": ["scout", "implementer"],
            "capabilities": ["reasoning", "code"],
        },
    ]


def test_default_intelligence_pool_is_human_first_and_quota_aware() -> None:
    text = render_intelligence_list(_endpoints(), "en")
    assert text.startswith("Available AI")
    assert "Claude subscription · subscription · already paid · quota 68% left" in text
    assert "GLM 5.2 · API" in text
    assert "opaque-internal-id" not in text
    assert "api:openrouter:glm-5.2" not in text
    assert "roles:" not in text
    assert "capabilities:" not in text


def test_intelligence_pool_details_preserve_power_user_metadata() -> None:
    text = render_intelligence_list(_endpoints(), "en", details=True)
    assert "id: subscription:claude:opaque-internal-id" in text
    assert "roles: reviewer, orchestrator" in text
    assert "capabilities: reasoning, code" in text


def test_russian_intelligence_pool_keeps_same_progressive_disclosure() -> None:
    text = render_intelligence_list(_endpoints(), "ru")
    assert text.startswith("Доступный AI")
    assert "Claude subscription · подписка · уже оплачено · лимит 68%" in text
    assert "opaque-internal-id" not in text
