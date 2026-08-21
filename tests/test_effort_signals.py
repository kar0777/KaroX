"""AUTO effort derives its signals from real, local evidence.

Contract under test (mandate: AUTO real task signals): a tiny isolated task
gets a lower recommendation, a cross-module risky task a higher one, prompt
length alone changes nothing, and every derived signal leaves an evidence
line. Map-dependent signals are injected as stored-map state so the tests
stay hermetic and deterministic.
"""

from karox.effort import recommend_effort
from karox.effort_signals import derive_task_signals

_MAP_STATE = {
    "implementation": [
        "src/karox/auth.py",
        "src/karox/session.py",
        "src/karox/bridge.py",
        "src/karox/tokens.py",
    ],
    "second_pass": {"additional_change_points": ["src/karox/proxy.py"]},
    "git_evidence": {
        "enabled": True,
        "hot_files": [
            {"path": "src/karox/auth.py", "commits": 7},
        ],
        "co_change_pairs": [
            {"paths": ["src/karox/auth.py", "src/karox/session.py"], "count": 5},
            {"paths": ["src/karox/auth.py", "src/karox/bridge.py"], "count": 4},
            {"paths": ["src/karox/auth.py", "src/karox/tokens.py"], "count": 4},
            {"paths": ["src/karox/auth.py", "src/karox/proxy.py"], "count": 3},
            {"paths": ["src/karox/auth.py", "tests/test_auth.py"], "count": 3},
        ],
    },
}


class TestScopeDiscrimination:
    def test_tiny_isolated_task_recommends_low(self):
        derived = derive_task_signals(
            "fix the typo in README.md", map_state={"implementation": []}
        )
        assert not derived.signals.ambiguity
        assert derived.signals.files_likely_affected == 1
        assert recommend_effort(derived.signals).level == "low"

    def test_risky_cross_module_task_recommends_high_or_more(self):
        derived = derive_task_signals(
            "fix the token refresh in src/karox/auth.py",
            map_state=_MAP_STATE,
        )
        signals = derived.signals
        assert signals.risk_area
        assert signals.dependency_breadth >= 4
        assert signals.regression_history
        level = recommend_effort(signals).level
        assert level in ("high", "extra-high", "ultra")

    def test_prompt_length_is_not_a_signal(self):
        short = derive_task_signals(
            "fix the typo in README.md", map_state=_MAP_STATE
        )
        padding = (
            "please could you, when you have a spare moment, kindly and "
            "with the greatest of care, " * 20
        )
        long = derive_task_signals(
            padding + "fix the typo in README.md", map_state=_MAP_STATE
        )
        assert (
            recommend_effort(short.signals).level
            == recommend_effort(long.signals).level
        )

    def test_cross_cutting_vocabulary_uses_map_breadth(self):
        derived = derive_task_signals(
            "refactor error handling across the codebase",
            map_state=_MAP_STATE,
        )
        assert derived.signals.files_likely_affected >= 5
        assert any("cross-cutting" in line for line in derived.evidence)


class TestHonestUnknowns:
    def test_no_map_is_recorded_not_invented(self):
        derived = derive_task_signals("почему иногда не работает?")
        assert derived.signals.ambiguity
        assert derived.signals.dependency_breadth == 0
        assert any("ambiguous" in line for line in derived.evidence)
        assert any(
            "no stored project map" in line for line in derived.evidence
        )

    def test_unknown_mode_is_dropped(self):
        derived = derive_task_signals("task", mode="warp")
        assert derived.signals.mode is None


class TestModeAndFreshness:
    def test_stale_map_escalates_one_step(self):
        fresh = derive_task_signals(
            "fix src/karox/session.py handling",
            map_state=_MAP_STATE,
            map_fresh=True,
        )
        stale = derive_task_signals(
            "fix src/karox/session.py handling",
            map_state=_MAP_STATE,
            map_fresh=False,
        )
        assert not fresh.signals.map_stale
        assert stale.signals.map_stale
        assert (
            recommend_effort(fresh.signals).level
            != recommend_effort(stale.signals).level
        )

    def test_ideate_mode_caps_the_recommendation(self):
        derived = derive_task_signals(
            "rework auth token migration across all modules in "
            "src/karox/auth.py",
            mode="ideate",
            map_state=_MAP_STATE,
        )
        recommendation = recommend_effort(derived.signals)
        assert recommendation.level == "high"
        assert any("capped at high" in r for r in recommendation.reasons)

    def test_plan_mode_on_ambiguity_explains_itself(self):
        vague = "почему всё иногда не работает?"
        build = derive_task_signals(vague, mode="build")
        plan = derive_task_signals(vague, mode="plan")
        build_reasons = recommend_effort(build.signals).reasons
        plan_reasons = recommend_effort(plan.signals).reasons
        assert not any("plan mode" in r for r in build_reasons)
        assert any("plan mode" in r for r in plan_reasons)

    def test_russian_risk_vocabulary_is_understood(self):
        derived = derive_task_signals("поправь авторизацию в проекте")
        assert derived.signals.risk_area
