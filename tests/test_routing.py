from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.provider_factory import ProviderFactory
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
)
from karox.registry import ModelPricing, ModelRecord, ProviderRecord, ProviderRegistry
from karox.routing import RouteTarget, RoutedProvider, RoutingPolicy


def request(*, tools: bool = False) -> ModelRequest:
    provider_tools = ()
    if tools:
        provider_tools = (ProviderTool("git_status", "status", {"type": "object"}),)
    return ModelRequest(
        model="route-alias",
        messages=(ModelMessage("user", "work"),),
        tools=provider_tools,
        deadline_seconds=2,
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


class FakeFactory:
    def __init__(self, providers: dict[str, FakeProvider]) -> None:
        self.providers = providers
        self.created: list[str] = []

    def create(self, record: ProviderRecord) -> FakeProvider:
        self.created.append(record.provider_id)
        return self.providers[record.provider_id]


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = ProviderRegistry(
            Path(self.temporary.name) / "providers.json"
        )

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
            )
        )
        return RouteTarget(provider_id, "route-alias")

    def routed(
        self,
        routes: tuple[RouteTarget, ...],
        providers: dict[str, FakeProvider],
        **policy: object,
    ) -> tuple[RoutedProvider, FakeFactory]:
        factory = FakeFactory(providers)
        return (
            RoutedProvider(
                self.registry,
                factory,
                RoutingPolicy(routes, **policy),  # type: ignore[arg-type]
            ),
            factory,
        )

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
            {"prompt_tokens": 12, "completion_tokens": 8},
        )
        self.assertEqual(second.pricing_version, "2026-07")

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


if __name__ == "__main__":
    unittest.main()
