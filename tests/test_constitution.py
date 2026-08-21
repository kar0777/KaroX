"""The Constitution is compact, complete, deterministic, and cache-stable.

Contract under test (mandate: Universal Constitution, prompt composition,
cache-friendly prefix, provider deltas): the core carries every mandated
section and invariant verbatim, provider adapters never fork the core,
composition is deterministic with a fixed sparse ordering, and the stable
prefix is byte-identical no matter how the dynamic deltas change.
"""

from karox.constitution import (
    CONSTITUTION_CORE,
    CORE_MAX_CHARS,
    DELTA_MAX_CHARS,
    PROVIDER_DELTAS,
    compose_system_prompt,
    provider_delta,
)


class TestCore:
    def test_covers_the_mandated_sections(self):
        for heading in (
            "## Mission",
            "## Engineering discipline",
            "## Autonomy",
            "## Modes",
            "## Effort",
            "## Root-cause discrimination",
            "## Project intelligence",
            "## Memory",
            "## Tool discipline",
            "## Verification",
            "## Communication",
            "## Safety",
        ):
            assert heading in CONSTITUTION_CORE

    def test_engineering_invariants_are_verbatim(self):
        for invariant in (
            "code written != feature complete",
            "module exists != runtime wired",
            "unit test green != product path verified",
            "plan exists != implementation complete",
        ):
            assert invariant in CONSTITUTION_CORE

    def test_maturity_ladder_present(self):
        for stage in ("FOUNDATION", "WIRED", "MEASURED", "LIVE"):
            assert stage in CONSTITUTION_CORE

    def test_root_cause_discipline_is_the_cheapest_discriminator(self):
        assert "cheapest observation or test" in CONSTITUTION_CORE

    def test_no_progress_theater(self):
        normalized = " ".join(CONSTITUTION_CORE.split())
        assert "Never invent progress percentages" in normalized

    def test_stays_compact(self):
        assert len(CONSTITUTION_CORE) <= CORE_MAX_CHARS
        for delta in PROVIDER_DELTAS.values():
            assert len(delta) <= DELTA_MAX_CHARS


class TestProviderDeltas:
    def test_known_families_and_aliases(self):
        assert provider_delta("openai") == PROVIDER_DELTAS["openai"]
        assert provider_delta("sol") == PROVIDER_DELTAS["openai"]
        assert provider_delta("claude") == PROVIDER_DELTAS["anthropic"]
        assert provider_delta("anthropic") == PROVIDER_DELTAS["anthropic"]
        assert provider_delta("luna") == PROVIDER_DELTAS["glm"]
        assert provider_delta("google") == PROVIDER_DELTAS["gemini"]

    def test_unknown_provider_gets_generic(self):
        assert provider_delta("brand-new-lab") == PROVIDER_DELTAS["generic"]
        assert provider_delta(None) == PROVIDER_DELTAS["generic"]
        assert provider_delta("  ") == PROVIDER_DELTAS["generic"]

    def test_deltas_are_adapters_not_forks(self):
        for delta in PROVIDER_DELTAS.values():
            assert "## Mission" not in delta
            assert "## Verification" not in delta


class TestComposition:
    def test_prefix_is_stable_across_dynamic_changes(self):
        one = compose_system_prompt(provider="openai", mode_delta="MODE-77")
        two = compose_system_prompt(
            provider="openai",
            mode_delta="OTHER-MODE-88",
            effort_line="EFFORT-77",
            memory_context="MEMORY-77",
        )
        assert one.prefix_sha256 == two.prefix_sha256
        assert one.stable_prefix == two.stable_prefix

    def test_prefix_depends_on_provider_only(self):
        openai = compose_system_prompt(provider="openai")
        claude = compose_system_prompt(provider="claude")
        assert openai.prefix_sha256 != claude.prefix_sha256

    def test_dynamic_order_is_fixed_and_sparse(self):
        composed = compose_system_prompt(
            provider="generic",
            research_block="RESEARCH-77",
            effort_line="EFFORT-77",
            mode_delta="MODE-77",
        )
        assert composed.sections == (
            "core",
            "provider",
            "mode",
            "effort",
            "research",
        )
        text = composed.text
        assert (
            text.index("MODE-77")
            < text.index("EFFORT-77")
            < text.index("RESEARCH-77")
        )

    def test_empty_sections_are_not_injected(self):
        composed = compose_system_prompt(
            mode_delta="   ", memory_context="", skill_suffix="\n\n"
        )
        assert composed.sections == ("core", "provider")
        assert composed.dynamic_suffix == ""

    def test_composition_is_deterministic(self):
        first = compose_system_prompt(provider="glm", project_suffix="P-77")
        second = compose_system_prompt(provider="glm", project_suffix="P-77")
        assert first.text == second.text
        assert first.text == first.stable_prefix + first.dynamic_suffix
