from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.provider_factory import ProviderFactory
from karox.providers import (
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
    ToolCallDelta,
    accumulate_response,
)
from karox.registry import ModelPricing, ModelRecord, ProviderRecord, ProviderRegistry
from karox.routing import RetryPolicy, RouteTarget, RoutedProvider, RoutingPolicy


# Routing-decision cases assert which route ran, not how often it was asked;
# retry has its own cases below.
NO_RETRY = RetryPolicy(max_attempts=1)


def request(
    *, tools: bool = False, deadline_seconds: float = 2
) -> ModelRequest:
    provider_tools = ()
    if tools:
        provider_tools = (ProviderTool("git_status", "status", {"type": "object"}),)
    return ModelRequest(
        model="route-alias",
        messages=(ModelMessage("user", "work"),),
        tools=provider_tools,
        deadline_seconds=deadline_seconds,
    )


def response(
    *, prompt_tokens: int = 4, completion_tokens: int = 2
) -> ModelResponse:
    return ModelResponse(
        content="done",
        tool_calls=(),
        finish_reason="stop",
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        response_id="response-1",
    )


class FakeProvider:
    provider_name = "fake"

    def __init__(self, outcomes: list[ModelResponse | ProviderError]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[ModelRequest] = []

    def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.requests.append(model_request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


class StreamingFakeProvider:
    """Yields scripted events, raising a scripted error where it was placed."""

    provider_name = "streaming-fake"

    def __init__(self, script: list[ModelEvent | ProviderError]) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []

    def stream(self, model_request: ModelRequest):
        self.requests.append(model_request)
        for item in self.script:
            if isinstance(item, ProviderError):
                raise item
            yield item

    def complete(self, model_request: ModelRequest) -> ModelResponse:
        return accumulate_response(self.stream(model_request))


class FakeFactory:
    def __init__(self, providers: dict[str, FakeProvider]) -> None:
        self.providers = providers
        self.created: list[str] = []

    def create(self, record: ProviderRecord) -> FakeProvider:
        self.created.append(record.provider_id)
        return self.providers[record.provider_id]


class FakeClock:
    """A monotonic clock that only advances when the router sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _RoutingFixture(unittest.TestCase):
    """A registry, a fake clock and helpers for declaring routes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = ProviderRegistry(
            Path(self.temporary.name) / "providers.json"
        )
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_route(
        self,
        provider_id: str,
        *,
        privacy: str = "local",
        tools: str = "true",
        streaming: str = "true",
        pricing: ModelPricing | None = None,
        model_id: str | None = None,
        max_output_tokens: int | None = None,
    ) -> RouteTarget:
        self.registry.put_provider(
            ProviderRecord(
                provider_id=provider_id,
                adapter_kind="openai_compatible_chat",
                base_url=f"http://{provider_id}.example/v1",
                privacy_class=privacy,
            )
        )
        model_name = model_id or f"{provider_id}-model"
        self.registry.put_model(
            ModelRecord(
                provider_id=provider_id,
                model_id=model_name,
                aliases=("route-alias",),
                tools=tools,
                streaming=streaming,
                pricing=pricing,
                max_output_tokens=max_output_tokens,
            )
        )
        return RouteTarget(provider_id, "route-alias")

    def routed(
        self,
        routes: tuple[RouteTarget, ...],
        providers: dict[str, FakeProvider],
        *,
        jitter: Callable[[], float] = lambda: 0.0,
        **policy: object,
    ) -> tuple[RoutedProvider, FakeFactory]:
        factory = FakeFactory(providers)
        policy.setdefault("retry", NO_RETRY)
        return (
            RoutedProvider(
                self.registry,
                factory,
                RoutingPolicy(routes, **policy),  # type: ignore[arg-type]
                sleep=self.clock.sleep,
                monotonic=self.clock.monotonic,
                jitter=jitter,
            ),
            factory,
        )


class RoutingTests(_RoutingFixture):
    def test_factory_maps_adapter_and_resolves_credentials_lazily(self) -> None:
        events: list[str] = []

        class Credentials:
            def accessor(self, reference: str):
                events.append(f"accessor:{reference}")

                def resolve() -> str:
                    events.append("resolve")
                    return "secret"

                return resolve

        captured: dict[str, object] = {}

        def constructor(base_url: str, **kwargs: object) -> FakeProvider:
            captured.update({"base_url": base_url, **kwargs})
            return FakeProvider([response()])

        record = ProviderRecord(
            provider_id="mapped",
            adapter_kind="openai_responses",
            base_url="https://provider.example/v1",
            credential_ref="os-keyring:provider/mapped",
            privacy_class="private",
        )
        factory = ProviderFactory(Credentials())  # type: ignore[arg-type]

        with patch.dict(ProviderFactory._ADAPTERS, {"openai_responses": constructor}):
            factory.create(record)

        self.assertEqual(events, ["accessor:os-keyring:provider/mapped"])
        credential = captured["credential"]
        self.assertTrue(callable(credential))
        self.assertEqual(credential(), "secret")  # type: ignore[operator]
        self.assertEqual(events[-1], "resolve")
        self.assertEqual(captured["base_url"], "https://provider.example/v1")

    def test_registered_output_ceiling_reaches_the_provider(self) -> None:
        target = self.add_route("primary", max_output_tokens=64_000)
        provider = FakeProvider([response()])
        routed, _ = self.routed((target,), {"primary": provider})

        routed.complete(request())

        # Routing used to rewrite only the model id, so the registered ceiling
        # never left the process and the Anthropic adapter capped output at its
        # own 4096-token default.
        self.assertEqual(provider.requests[0].max_output_tokens, 64_000)

    def test_an_explicit_request_ceiling_outranks_the_registry(self) -> None:
        target = self.add_route("primary", max_output_tokens=64_000)
        provider = FakeProvider([response()])
        routed, _ = self.routed((target,), {"primary": provider})

        routed.complete(replace(request(), max_output_tokens=1_024))

        self.assertEqual(provider.requests[0].max_output_tokens, 1_024)

    def test_fallback_is_limited_to_transient_errors(self) -> None:
        first = self.add_route("first")
        second = self.add_route("second")
        for kind in (
            ProviderErrorKind.RATE_LIMIT,
            ProviderErrorKind.MODEL_UNAVAILABLE,
            ProviderErrorKind.TRANSPORT,
            ProviderErrorKind.PROVIDER_INTERNAL,
        ):
            with self.subTest(kind=kind):
                providers = {
                    "first": FakeProvider([ProviderError(kind, "temporary")]),
                    "second": FakeProvider([response()]),
                }
                routed, factory = self.routed((first, second), providers)

                result = routed.complete(request())

                self.assertEqual(factory.created, ["first", "second"])
                self.assertEqual(result.selected_provider, "second")
                self.assertEqual(
                    [item["status"] for item in result.route_attempts],
                    ["fallback", "completed"],
                )

    def test_non_transient_errors_never_fallback(self) -> None:
        first = self.add_route("first")
        second = self.add_route("second")
        non_fallback = set(ProviderErrorKind).difference(
            {
                ProviderErrorKind.RATE_LIMIT,
                ProviderErrorKind.MODEL_UNAVAILABLE,
                ProviderErrorKind.TRANSPORT,
                ProviderErrorKind.PROVIDER_INTERNAL,
                # A rejected payload is deterministic for this endpoint but not
                # for the next one, so it falls back; see the case below.
                ProviderErrorKind.INVALID_REQUEST,
            }
        )
        for kind in sorted(non_fallback, key=lambda item: item.value):
            with self.subTest(kind=kind):
                providers = {
                    "first": FakeProvider([ProviderError(kind, "terminal")]),
                    "second": FakeProvider([response()]),
                }
                routed, factory = self.routed((first, second), providers)

                with self.assertRaises(ProviderError) as raised:
                    routed.complete(request())

                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(factory.created, ["first"])
                self.assertEqual(raised.exception.route_attempts[0]["status"], "failed")

    def test_a_payload_one_gateway_rejects_moves_to_the_next_route(self) -> None:
        # Two endpoints serving the same model disagree about parameter names
        # and limits often enough that ending the run on route 0 defeats the
        # point of configuring a fallback.
        first = self.add_route("first")
        second = self.add_route("second")
        providers = {
            "first": FakeProvider(
                [ProviderError(ProviderErrorKind.INVALID_REQUEST, "unsupported field")]
            ),
            "second": FakeProvider([response()]),
        }
        routed, factory = self.routed((first, second), providers)

        result = routed.complete(request())

        self.assertEqual(factory.created, ["first", "second"])
        self.assertEqual(result.selected_provider, "second")
        self.assertEqual(
            [item["status"] for item in result.route_attempts],
            ["fallback", "completed"],
        )

    def test_a_rejected_payload_is_never_re_sent_to_the_same_endpoint(self) -> None:
        # Falling back is a different question from retrying: the identical
        # request would be rejected identically, so a retry only spends the
        # caller's deadline.
        target = self.add_route("only")
        provider = FakeProvider(
            [
                ProviderError(ProviderErrorKind.INVALID_REQUEST, "unsupported field"),
                response(),
            ]
        )
        routed, _ = self.routed(
            (target,), {"only": provider}, retry=RetryPolicy(max_attempts=3)
        )

        with self.assertRaises(ProviderError) as raised:
            routed.complete(request())

        self.assertEqual(raised.exception.kind, ProviderErrorKind.INVALID_REQUEST)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(raised.exception.route_attempts[0].get("retries"), None)

    def test_a_rate_limited_route_is_retried_instead_of_ending_the_run(self) -> None:
        target = self.add_route("only")
        limited = ProviderError(
            ProviderErrorKind.RATE_LIMIT, "slow down", retry_after=0.01
        )
        provider = FakeProvider([limited, limited, response()])
        routed, factory = self.routed(
            (target,), {"only": provider}, retry=RetryPolicy()
        )

        result = routed.complete(request())

        # A single-route configuration has no next route, so before this the
        # first transient rejection ended a whole task.
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(self.clock.sleeps, [0.01, 0.01])
        self.assertEqual(factory.created, ["only"])
        self.assertEqual(result.selected_provider, "only")
        self.assertEqual(
            [item["status"] for item in result.route_attempts], ["completed"]
        )
        self.assertEqual(result.route_attempts[0]["retries"], 2)

    def test_an_hour_long_retry_after_does_not_park_the_run(self) -> None:
        target = self.add_route("only")
        # A 429 may carry Retry-After: 3500, and the sleep is uninterruptible,
        # so honouring it literally holds a run for an hour with nothing able to
        # stop it. Past the cap the caller is better served by moving on.
        patient = ProviderError(
            ProviderErrorKind.RATE_LIMIT, "slow down", retry_after=3_500.0
        )
        provider = FakeProvider([patient, response()])
        routed, _ = self.routed((target,), {"only": provider}, retry=RetryPolicy())

        routed.complete(request(deadline_seconds=3_600.0))

        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertLessEqual(self.clock.sleeps[0], RetryPolicy().max_delay_seconds)

    def test_a_skewed_clock_does_not_turn_a_retry_into_an_instant_resend(
        self,
    ) -> None:
        target = self.add_route("only")
        # The HTTP-date form of Retry-After parses against the local clock, so a
        # host running fast reads the deadline as already past and yields zero.
        skewed = ProviderError(
            ProviderErrorKind.RATE_LIMIT, "slow down", retry_after=0.0
        )
        provider = FakeProvider([skewed, response()])
        routed, _ = self.routed((target,), {"only": provider}, retry=RetryPolicy())

        routed.complete(request())

        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertGreaterEqual(self.clock.sleeps[0], RetryPolicy().base_delay_seconds)

    def test_an_error_a_retry_cannot_help_is_never_retried(self) -> None:
        target = self.add_route("only")
        hopeless = set(ProviderErrorKind).difference(
            {
                ProviderErrorKind.RATE_LIMIT,
                ProviderErrorKind.MODEL_UNAVAILABLE,
                ProviderErrorKind.TRANSPORT,
                ProviderErrorKind.PROVIDER_INTERNAL,
            }
        )
        for kind in sorted(hopeless, key=lambda item: item.value):
            with self.subTest(kind=kind):
                provider = FakeProvider(
                    # Even an explicit Retry-After must not buy an attempt that
                    # cannot change the answer.
                    [ProviderError(kind, "terminal", retry_after=0.01)]
                )
                routed, _ = self.routed(
                    (target,), {"only": provider}, retry=RetryPolicy()
                )

                with self.assertRaises(ProviderError) as raised:
                    routed.complete(request())

                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(self.clock.sleeps, [])
                self.assertNotIn("retries", raised.exception.route_attempts[0])

    def test_backoff_grows_and_is_jittered_within_its_ceiling(self) -> None:
        target = self.add_route("only")
        for jitter, expected in ((0.0, [0.5, 1.0, 1.5]), (1.0, [1.0, 2.0, 3.0])):
            with self.subTest(jitter=jitter):
                self.clock = FakeClock()
                internal = ProviderError(ProviderErrorKind.PROVIDER_INTERNAL, "boom")
                provider = FakeProvider([internal, internal, internal, response()])
                routed, _ = self.routed(
                    (target,),
                    {"only": provider},
                    jitter=lambda value=jitter: value,
                    retry=RetryPolicy(
                        max_attempts=4,
                        base_delay_seconds=1.0,
                        max_delay_seconds=3.0,
                    ),
                )

                routed.complete(replace(request(), deadline_seconds=60))

                self.assertEqual(self.clock.sleeps, expected)

    def test_a_retry_never_sleeps_past_the_request_deadline(self) -> None:
        target = self.add_route("only")
        provider = FakeProvider(
            [ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down", retry_after=90.0)]
        )
        routed, _ = self.routed((target,), {"only": provider}, retry=RetryPolicy())

        with self.assertRaises(ProviderError) as raised:
            routed.complete(request())

        self.assertEqual(raised.exception.kind, ProviderErrorKind.RATE_LIMIT)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(provider.requests), 1)

    def test_retries_are_spent_before_the_next_route_is_tried(self) -> None:
        first = self.add_route("first")
        second = self.add_route("second")
        busy = ProviderError(ProviderErrorKind.RATE_LIMIT, "busy", retry_after=0.01)
        providers = {
            "first": FakeProvider([busy, busy]),
            "second": FakeProvider([response()]),
        }
        routed, factory = self.routed(
            (first, second), providers, retry=RetryPolicy(max_attempts=2)
        )

        result = routed.complete(request())

        self.assertEqual(len(providers["first"].requests), 2)
        self.assertEqual(self.clock.sleeps, [0.01])
        self.assertEqual(factory.created, ["first", "second"])
        self.assertEqual(
            [item["status"] for item in result.route_attempts],
            ["fallback", "completed"],
        )
        self.assertEqual(result.route_attempts[0]["retries"], 1)

    def test_cheapest_strategy_reorders_only_comparable_priced_routes(self) -> None:
        expensive = self.add_route(
            "expensive",
            pricing=ModelPricing("v1", "USD", 10.0, 20.0, "fixture"),
            max_output_tokens=1_000,
        )
        cheap = self.add_route(
            "cheap",
            pricing=ModelPricing("v1", "USD", 1.0, 2.0, "fixture"),
            max_output_tokens=1_000,
        )
        providers = {
            "expensive": FakeProvider([response()]),
            "cheap": FakeProvider([response()]),
        }
        routed, factory = self.routed(
            (expensive, cheap), providers, route_strategy="cheapest"
        )

        result = routed.complete(request())

        self.assertEqual(factory.created, ["cheap"])
        self.assertEqual(result.selected_provider, "cheap")

    def test_cheapest_strategy_keeps_user_order_when_pricing_is_incomplete(self) -> None:
        first = self.add_route("first", max_output_tokens=1_000)
        second = self.add_route(
            "second",
            pricing=ModelPricing("v1", "USD", 1.0, 1.0, "fixture"),
            max_output_tokens=1_000,
        )
        providers = {
            "first": FakeProvider([response()]),
            "second": FakeProvider([response()]),
        }
        routed, factory = self.routed(
            (first, second), providers, route_strategy="cheapest"
        )

        result = routed.complete(request())

        self.assertEqual(factory.created, ["first"])
        self.assertEqual(result.selected_provider, "first")

    def test_retry_policy_rejects_unusable_configuration(self) -> None:
        invalid = (
            {"max_attempts": 0},
            {"max_attempts": True},
            {"max_attempts": 2.0},
            {"max_attempts": 11},
            {"base_delay_seconds": -1.0},
            {"base_delay_seconds": float("inf")},
            {"max_delay_seconds": 0.1},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                RetryPolicy(**options)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            RoutingPolicy((RouteTarget("only", "model"),), retry="fast")  # type: ignore[arg-type]

    def test_a_route_authenticates_from_the_environment_without_a_keyring(self) -> None:
        captured: dict[str, object] = {}

        def constructor(base_url: str, **kwargs: object) -> FakeProvider:
            captured.update({"base_url": base_url, **kwargs})
            return FakeProvider([response()])

        record = ProviderRecord(
            provider_id="ci",
            adapter_kind="openai_responses",
            base_url="https://provider.example/v1",
            credential_ref="env:KAROX_PROVIDER_CI_API_KEY",
        )

        with (
            # A headless runner has no Secret Service and often no keyring
            # module either; the routed layer still has to authenticate.
            patch.dict(sys.modules, {"keyring": None}),
            patch.dict(os.environ, {"KAROX_PROVIDER_CI_API_KEY": "harmless-ci-key"}),
            patch.dict(ProviderFactory._ADAPTERS, {"openai_responses": constructor}),
        ):
            ProviderFactory().create(record)

            credential = captured["credential"]
            self.assertTrue(callable(credential))
            self.assertEqual(credential(), "harmless-ci-key")  # type: ignore[operator]

    def test_preflight_rejects_tools_streaming_and_privacy_fail_closed(self) -> None:
        cases = (
            ("tools", {"tools": "unknown"}, {"tools": True}, "unsupported_capability"),
            ("streaming", {"streaming": "false"}, {}, "unsupported_capability"),
            ("privacy", {"privacy": "public"}, {}, "permission"),
        )
        for name, route_options, request_options, expected in cases:
            with self.subTest(name=name):
                provider_id = f"route-{name}"
                route = self.add_route(provider_id, **route_options)  # type: ignore[arg-type]
                provider = FakeProvider([response()])
                routed, factory = self.routed(
                    (route,),
                    {provider_id: provider},
                    privacy_limit="private" if name == "privacy" else "public",
                )

                with self.assertRaises(ProviderError) as raised:
                    routed.complete(request(**request_options))  # type: ignore[arg-type]

                self.assertEqual(raised.exception.kind.value, expected)
                self.assertEqual(factory.created, [])
                self.assertEqual(
                    raised.exception.route_attempts[0]["status"], "rejected"
                )

    def test_preflight_rejections_continue_to_a_compatible_route(self) -> None:
        cases = (
            ("privacy", {"privacy": "public"}, {}, {"privacy_limit": "private"}),
            ("tools", {"tools": "false"}, {"tools": True}, {}),
            ("streaming", {"streaming": "false"}, {}, {}),
            (
                "missing-pricing",
                {},
                {},
                {"max_cost": 1.0, "currency": "USD"},
            ),
            (
                "currency-mismatch",
                {"pricing": ModelPricing("v1", "EUR", 1.0, 1.0, "fixture")},
                {},
                {"max_cost": 1.0, "currency": "USD"},
            ),
        )
        for name, rejected_options, request_options, policy in cases:
            with self.subTest(name=name):
                rejected_id = f"rejected-{name}"
                accepted_id = f"accepted-{name}"
                rejected = self.add_route(rejected_id, **rejected_options)  # type: ignore[arg-type]
                accepted = self.add_route(
                    accepted_id,
                    pricing=(
                        ModelPricing("v1", "USD", 1.0, 1.0, "fixture")
                        if policy.get("max_cost") is not None
                        else None
                    ),
                )
                providers = {
                    rejected_id: FakeProvider([response()]),
                    accepted_id: FakeProvider([response()]),
                }
                routed, factory = self.routed(
                    (rejected, accepted), providers, **policy
                )

                result = routed.complete(request(**request_options))  # type: ignore[arg-type]

                self.assertEqual(factory.created, [accepted_id])
                self.assertEqual(result.selected_provider, accepted_id)
                self.assertEqual(
                    [item["status"] for item in result.route_attempts],
                    ["rejected", "completed"],
                )

    def test_cost_budget_requires_matching_explicit_pricing(self) -> None:
        missing = self.add_route("missing-pricing")
        eur = self.add_route(
            "eur-pricing",
            pricing=ModelPricing("v1", "EUR", 1.0, 1.0, "fixture"),
        )
        for route, provider_id, message in (
            (missing, "missing-pricing", "requires model pricing"),
            (eur, "eur-pricing", "currency does not match"),
        ):
            with self.subTest(provider=provider_id):
                routed, factory = self.routed(
                    (route,),
                    {provider_id: FakeProvider([response()])},
                    max_cost=1.0,
                    currency="USD",
                )

                with self.assertRaisesRegex(ProviderError, message) as raised:
                    routed.complete(request())

                self.assertEqual(
                    raised.exception.kind, ProviderErrorKind.BUDGET_EXCEEDED
                )
                self.assertEqual(factory.created, [])

    def test_usage_and_cost_accumulate_across_requests(self) -> None:
        pricing = ModelPricing("2026-07", "USD", 2_000.0, 4_000.0, "fixture")
        route = self.add_route("priced", pricing=pricing)
        provider = FakeProvider(
            [
                response(prompt_tokens=10, completion_tokens=5),
                response(prompt_tokens=2, completion_tokens=3),
            ]
        )
        routed, _ = self.routed((route,), {"priced": provider})

        first = routed.complete(request())
        second = routed.complete(request())

        self.assertEqual(first.cost, 0.04)
        self.assertEqual(first.cumulative_cost, 0.04)
        self.assertEqual(second.cost, 0.016)
        self.assertEqual(second.cumulative_cost, 0.056)
        self.assertEqual(
            second.cumulative_usage,
            {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        )
        self.assertEqual(second.pricing_version, "2026-07")

    def test_mixed_provider_usage_schemas_preserve_canonical_total(self) -> None:
        route = self.add_route("mixed")
        provider = FakeProvider(
            [
                ModelResponse(
                    content="first",
                    tool_calls=(),
                    usage={"total_tokens": 10},
                    finish_reason="stop",
                ),
                ModelResponse(
                    content="second",
                    tool_calls=(),
                    usage={"input_tokens": 11, "output_tokens": 9},
                    finish_reason="stop",
                ),
            ]
        )
        routed, _ = self.routed((route,), {"mixed": provider})
        routed.complete(request())
        second = routed.complete(request())
        self.assertEqual(second.cumulative_usage["total_tokens"], 30)

    def test_response_that_crosses_budget_is_charged_and_marked(self) -> None:
        pricing = ModelPricing("v1", "USD", 1_000.0, 1_000.0, "fixture")
        route = self.add_route("overrun", pricing=pricing)
        provider = FakeProvider([response(prompt_tokens=7, completion_tokens=5)])
        factory = FakeFactory({"overrun": provider})
        routed = RoutedProvider(
            self.registry,
            factory,
            RoutingPolicy(
                (route,),
                max_total_tokens=10,
                max_cost=0.01,
                currency="USD",
            ),
        )

        result = routed.complete(request())

        self.assertTrue(result.budget_exceeded)
        self.assertEqual(result.budget_reason, "token_budget,cost_budget")
        self.assertEqual(result.cumulative_cost, 0.012)
        self.assertEqual(result.cumulative_usage["prompt_tokens"], 7)

    def test_exhausted_initial_budgets_do_not_create_or_call_a_provider(self) -> None:
        pricing = ModelPricing("v1", "USD", 1.0, 1.0, "fixture")
        route = self.add_route("budgeted", pricing=pricing)
        cases = (
            (
                {"max_total_tokens": 10},
                {"total_tokens": 10},
                {},
                "token budget",
            ),
            (
                {"max_cost": 1.0, "currency": "USD"},
                {},
                {"USD": 1.0},
                "cost budget",
            ),
        )
        for policy, usage, costs, message in cases:
            with self.subTest(message=message):
                provider = FakeProvider([response()])
                factory = FakeFactory({"budgeted": provider})
                routed = RoutedProvider(
                    self.registry,
                    factory,
                    RoutingPolicy((route,), **policy),  # type: ignore[arg-type]
                    initial_usage=usage,
                    initial_costs=costs,
                )

                with self.assertRaisesRegex(ProviderError, message):
                    routed.complete(request())

                self.assertEqual(factory.created, [])
                self.assertEqual(provider.requests, [])

    def test_all_preflight_rejections_preserve_every_attempt(self) -> None:
        privacy = self.add_route("privacy-rejected", privacy="public")
        tools = self.add_route("tools-rejected", tools="false")
        pricing = self.add_route("pricing-rejected")
        routed, factory = self.routed(
            (privacy, tools, pricing),
            {
                "privacy-rejected": FakeProvider([response()]),
                "tools-rejected": FakeProvider([response()]),
                "pricing-rejected": FakeProvider([response()]),
            },
            privacy_limit="private",
            max_cost=1.0,
            currency="USD",
        )

        with self.assertRaises(ProviderError) as raised:
            routed.complete(request(tools=True))

        self.assertEqual(factory.created, [])
        self.assertEqual(
            [item["provider_id"] for item in raised.exception.route_attempts],
            ["privacy-rejected", "tools-rejected", "pricing-rejected"],
        )
        self.assertEqual(
            [item["status"] for item in raised.exception.route_attempts],
            ["rejected", "rejected", "rejected"],
        )

    def test_complete_failure_retains_every_route_attempt(self) -> None:
        first = self.add_route("first")
        second = self.add_route("second")
        providers = {
            "first": FakeProvider(
                [ProviderError(ProviderErrorKind.TRANSPORT, "offline")]
            ),
            "second": FakeProvider(
                [ProviderError(ProviderErrorKind.RATE_LIMIT, "busy")]
            ),
        }
        routed, _ = self.routed((first, second), providers)

        with self.assertRaises(ProviderError) as raised:
            routed.complete(request())

        self.assertEqual(
            [item["provider_id"] for item in raised.exception.route_attempts],
            ["first", "second"],
        )
        self.assertEqual(
            [item["status"] for item in raised.exception.route_attempts],
            ["fallback", "failed"],
        )


class StreamingRouteTests(_RoutingFixture):
    """The router forwards a route's own events instead of replaying them."""

    def test_stream_preserves_the_order_the_provider_produced(self) -> None:
        # A buffered replay emitted every text delta before every tool call, so
        # a model that narrates between two tool calls came out reordered.
        emitted = (
            ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="first "),
            ModelEvent(
                ModelEventKind.TOOL_CALL_DELTA,
                tool_call_delta=ToolCallDelta(0, "call-1", "git_status", "{}"),
            ),
            ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="second"),
            ModelEvent(ModelEventKind.USAGE, usage={"prompt_tokens": 4}),
            ModelEvent(ModelEventKind.COMPLETION, finish_reason="tool_calls"),
        )
        target = self.add_route("only")
        routed, _ = self.routed(
            (target,), {"only": StreamingFakeProvider(list(emitted))}
        )

        events = list(routed.stream(request()))

        # Every upstream event in its original order, then exactly one terminal
        # event -- the router's own, not the transport's bare completion too.
        self.assertEqual(
            [item.kind for item in events],
            [item.kind for item in emitted[:-1]] + [ModelEventKind.COMPLETION],
        )
        self.assertEqual(
            [item.text_delta for item in events if item.text_delta],
            ["first ", "second"],
        )

    def test_the_terminal_event_carries_the_routed_and_priced_response(self) -> None:
        target = self.add_route(
            "priced",
            pricing=ModelPricing("v1", "USD", 1000.0, 2000.0, "fixture"),
        )
        routed, _ = self.routed(
            (target,),
            {
                "priced": StreamingFakeProvider(
                    [
                        ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="done"),
                        ModelEvent(
                            ModelEventKind.USAGE,
                            usage={"prompt_tokens": 4, "completion_tokens": 2},
                        ),
                        ModelEvent(ModelEventKind.COMPLETION, finish_reason="stop"),
                    ]
                )
            },
        )

        final = list(routed.stream(request()))[-1]

        self.assertEqual(final.kind, ModelEventKind.COMPLETION)
        assert final.response is not None
        # A streaming caller learns the same route, cost and budget verdict a
        # blocking one does, instead of having to ask again.
        self.assertEqual(final.response.selected_provider, "priced")
        self.assertEqual(final.response.selected_model, "priced-model")
        self.assertEqual(final.response.content, "done")
        self.assertEqual(final.response.currency, "USD")
        self.assertAlmostEqual(final.response.cost or 0.0, 0.008)
        self.assertAlmostEqual(final.response.uncached_cost or 0.0, 0.008)
        self.assertEqual(final.response.cache_savings, 0.0)
        self.assertEqual(final.response.pricing_version, "v1")

    def test_a_route_that_dies_before_emitting_anything_still_falls_back(self) -> None:
        first = self.add_route("first")
        second = self.add_route("second")
        routed, _ = self.routed(
            (first, second),
            {
                "first": StreamingFakeProvider(
                    [ProviderError(ProviderErrorKind.TRANSPORT, "offline")]
                ),
                "second": StreamingFakeProvider(
                    [
                        ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="ok"),
                        ModelEvent(ModelEventKind.COMPLETION, finish_reason="stop"),
                    ]
                ),
            },
        )

        events = list(routed.stream(request()))

        final = events[-1]
        assert final.response is not None
        self.assertEqual(final.response.selected_provider, "second")
        self.assertEqual(
            [item["status"] for item in final.response.route_attempts],
            ["fallback", "completed"],
        )

    def test_a_route_that_dies_mid_answer_is_surfaced_not_replayed(self) -> None:
        # Falling back here would show the caller the first half of an answer
        # twice, so the failure is reported instead of being papered over.
        first = self.add_route("first")
        second = self.add_route("second")
        routed, _ = self.routed(
            (first, second),
            {
                "first": StreamingFakeProvider(
                    [
                        ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="half "),
                        ProviderError(ProviderErrorKind.TRANSPORT, "cut off"),
                    ]
                ),
                "second": StreamingFakeProvider(
                    [
                        ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="ok"),
                        ModelEvent(ModelEventKind.COMPLETION, finish_reason="stop"),
                    ]
                ),
            },
        )

        seen: list[ModelEvent] = []
        with self.assertRaises(ProviderError) as raised:
            for event in routed.stream(request()):
                seen.append(event)

        self.assertEqual([item.text_delta for item in seen], ["half "])
        self.assertEqual(
            [item["status"] for item in raised.exception.route_attempts], ["failed"]
        )

    def test_a_partly_delivered_turn_is_not_retried(self) -> None:
        target = self.add_route("only")
        provider = StreamingFakeProvider(
            [
                ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="half "),
                ProviderError(ProviderErrorKind.RATE_LIMIT, "busy"),
            ]
        )
        routed, _ = self.routed(
            (target,), {"only": provider}, retry=RetryPolicy(max_attempts=3)
        )

        with self.assertRaises(ProviderError):
            list(routed.stream(request()))

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
