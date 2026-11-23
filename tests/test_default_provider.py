from karox6.cli import default_provider


def test_claude_on_anthropic_api_uses_native_messages():
    assert default_provider("claude-haiku-5-5", None) == "anthropic"
    assert default_provider("claude-opus-5-5", "https://api.anthropic.com") == "anthropic"


def test_other_models_and_gateways_keep_openai_chat():
    assert default_provider("gpt-6.2-sol", None) == "openai-chat"
    assert default_provider(None, None) == "openai-chat"
    assert default_provider("claude-haiku-5-5", "https://openrouter.ai/api/v1") == "openai-chat"
    assert default_provider("anthropic/claude-haiku-5.5", None) == "openai-chat"


def test_only_the_bare_anthropic_endpoint_counts_as_native():
    assert default_provider("claude-haiku-5-5", "https://api.anthropic.com/") == "anthropic"
    assert default_provider("claude-haiku-5-5", "https://api.anthropic.com/v1") == "openai-chat"
    proxy = "https://gw.test/api.anthropic.com"
    assert default_provider("claude-haiku-5-5", proxy) == "openai-chat"
