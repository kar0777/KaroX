from __future__ import annotations

import importlib
import errno
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.registry import (
    ModelPricing,
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
    RegistryError,
)


def provider(provider_id: str = "local") -> ProviderRecord:
    return ProviderRecord(
        provider_id=provider_id,
        adapter_kind="openai_compatible_chat",
        base_url="http://127.0.0.1:8000/v1",
        privacy_class="local",
    )


def model(
    model_id: str = "model-a", *, aliases: tuple[str, ...] = ("default",)
) -> ModelRecord:
    return ModelRecord(
        provider_id="local",
        model_id=model_id,
        aliases=aliases,
        tools="true",
        streaming="true",
        pricing=ModelPricing("2026-07", "USD", 1.5, 4.0, "test fixture"),
    )


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "providers.json"
        self.registry = ProviderRegistry(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_is_deterministic_and_resolves_aliases(self) -> None:
        self.registry.put_provider(provider("z-provider"))
        self.registry.put_provider(provider())
        self.registry.put_model(model("z-model", aliases=("z",)))
        self.registry.put_model(model())

        self.assertEqual(
            [item.provider_id for item in self.registry.providers()],
            ["local", "z-provider"],
        )
        self.assertEqual(
            [item.model_id for item in self.registry.models("local")],
            ["model-a", "z-model"],
        )
        selected = self.registry.model("local", "default")
        self.assertEqual(selected.model_id, "model-a")
        assert selected.pricing is not None
        self.assertEqual(
            selected.pricing.estimate(
                {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
            ),
            3.5,
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["providers"][0]["provider_id"], "local")
        self.assertEqual(payload["models"][0]["model_id"], "model-a")

    def test_requires_provider_and_model_arrays(self) -> None:
        invalid_values = (
            {"schema_version": 1, "providers": {}, "models": []},
            {"schema_version": 1, "providers": [], "models": {}},
            {"schema_version": 1, "providers": []},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(RegistryError, "must be arrays"):
                    self.registry.providers()

    def test_rejects_collisions_across_model_ids_and_aliases(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model("first", aliases=("shared",)))

        for record in (
            model("shared", aliases=()),
            model("second", aliases=("shared",)),
            model("third", aliases=("third",)),
        ):
            with (
                self.subTest(record=record),
                self.assertRaisesRegex(RegistryError, "colliding model IDs or aliases"),
            ):
                self.registry.put_model(record)

    def test_save_validates_orphans_before_writing(self) -> None:
        orphan = ModelRecord(provider_id="missing", model_id="orphan")
        with self.assertRaisesRegex(RegistryError, "orphaned"):
            self.registry._save([provider()], [orphan])
        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_preserves_previous_registry(self) -> None:
        self.registry.put_provider(provider())
        original = self.path.read_bytes()

        with patch("karox.registry.os.replace", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                self.registry.put_provider(provider("another"))

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_a_cached_prompt_is_not_billed_as_a_fresh_one(self) -> None:
        pricing = ModelPricing("2026-07", "USD", 5.0, 25.0, "test fixture")

        fresh = pricing.estimate(
            {"prompt_tokens": 100_000, "completion_tokens": 1_000}
        )
        cached = pricing.estimate(
            {
                "prompt_tokens": 100_000,
                "completion_tokens": 1_000,
                "cache_read_tokens": 95_000,
            }
        )

        # prompt_tokens is the whole prompt; charging all of it at the input
        # rate reported a cost the run did not incur once caching existed.
        self.assertEqual(fresh, 0.525)
        self.assertEqual(cached, 0.0975)
        self.assertEqual(pricing.estimate_uncached({
            "prompt_tokens": 100_000,
            "completion_tokens": 1_000,
            "cache_read_tokens": 95_000,
        }), 0.525)

    def test_a_cache_write_costs_more_than_plain_input(self) -> None:
        pricing = ModelPricing("2026-07", "USD", 5.0, 25.0, "test fixture")

        written = pricing.estimate(
            {"prompt_tokens": 1_000_000, "cache_write_tokens": 1_000_000}
        )

        self.assertEqual(written, 6.25)

    def test_explicit_cache_rates_beat_the_assumed_multipliers(self) -> None:
        pricing = ModelPricing(
            "2026-07",
            "USD",
            5.0,
            25.0,
            "test fixture",
            cache_read_per_million=0.25,
            cache_write_per_million=7.5,
        )

        self.assertEqual(pricing.cache_read_rate, 0.25)
        self.assertEqual(pricing.cache_write_rate, 7.5)
        self.assertEqual(
            pricing.estimate(
                {"prompt_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
            ),
            0.25,
        )

    def test_pricing_rejects_invalid_currency_and_non_finite_values(self) -> None:
        for arguments in (
            ("v1", "usd", 1.0, 2.0, "source"),
            ("v1", "USD", float("nan"), 2.0, "source"),
            ("v1", "USD", 1.0, -1.0, "source"),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ModelPricing(*arguments)

    def test_provider_rejects_non_text_header_and_query_entries(self) -> None:
        cases = (
            {"headers": {1: "value"}},
            {"headers": {"X-Test": 1}},
            {"query": {1: "value"}},
            {"query": {"api-version": 1}},
        )
        for options in cases:
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "must contain only text"),
            ):
                ProviderRecord(
                    provider_id="typed",
                    adapter_kind="openai_compatible_chat",
                    base_url="https://provider.example/v1",
                    **options,  # type: ignore[arg-type]
                )

    def test_selection_is_canonical_and_persistent(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model())

        selected = self.registry.select_model("local", "default")

        self.assertEqual(selected.model_id, "model-a")
        reloaded = ProviderRegistry(self.path).selected_model()
        assert reloaded is not None
        self.assertEqual(
            (reloaded.provider_id, reloaded.model_id), ("local", "model-a")
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["selected_model"],
            {"provider_id": "local", "model_id": "model-a"},
        )

    def test_map_alias_creates_and_moves_provider_scoped_alias(self) -> None:
        self.registry.put_provider(provider())
        first = self.registry.map_alias("local", "sol", "actual-sol-a")
        self.assertEqual(first.aliases, ("sol",))
        second = self.registry.map_alias("local", "sol", "actual-sol-b")
        self.assertIn("sol", second.aliases)
        self.assertNotIn("sol", self.registry.model("local", "actual-sol-a").aliases)
        self.assertEqual(self.registry.model("local", "sol").model_id, "actual-sol-b")

    def test_removal_requires_cascade_and_clears_selection(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model())
        self.registry.select_model("local", "default")

        with self.assertRaisesRegex(RegistryError, "use cascade"):
            self.registry.remove_provider("local")
        self.assertIsNotNone(self.registry.selected_model())

        self.registry.remove_provider("local", cascade=True)
        self.assertEqual(self.registry.providers(), [])
        self.assertEqual(self.registry.models(), [])
        self.assertIsNone(self.registry.selected_model())

    def test_removing_selected_model_clears_selection(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model())
        self.registry.select_model("local", "default")

        removed = self.registry.remove_model("local", "default")

        self.assertEqual(removed.model_id, "model-a")
        self.assertIsNone(self.registry.selected_model())

    def test_rejects_dangling_selected_model_on_load(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "providers": [asdict(provider())],
                    "models": [],
                    "selected_model": {
                        "provider_id": "local",
                        "model_id": "missing",
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RegistryError, "selected model does not exist"):
            self.registry.selected_model()


def transient_error() -> OSError:
    """A read that lands inside the swap window, as the OS reports it.

    Measured against the real file: every read failure carries ``errno.EACCES``
    with ``winerror`` unset, while every ``os.replace`` failure carries
    ``winerror`` 5. The classifier has to accept both spellings.
    """
    return PermissionError(errno.EACCES, "Permission denied")


def with_winerror(error: OSError, code: int) -> OSError:
    try:
        error.winerror = code
    except AttributeError:  # pragma: no cover - a build without the member
        raise unittest.SkipTest("this build does not expose OSError.winerror")
    return error


class RegistrySharingRetryTests(unittest.TestCase):
    """A reader thread and the save worker share one registry file."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "providers.json"
        self.registry = ProviderRegistry(self.path)
        self.registry.put_provider(provider())
        self.registry.put_model(model())
        self.registry_module = importlib.import_module("karox.registry")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _classify(self, error: OSError, *, platform: str = "nt") -> bool:
        with patch.object(self.registry_module, "os", SimpleNamespace(name=platform)):
            return self.registry_module._is_transient_sharing_error(error)

    def test_a_bare_access_denial_on_windows_is_the_swap_window(self) -> None:
        # This is the spelling the reader actually gets, so it must be retried
        # even though the exception carries no Win32 code to inspect.
        self.assertTrue(self._classify(transient_error()))

    def test_a_win32_sharing_code_is_the_swap_window(self) -> None:
        self.assertTrue(self._classify(with_winerror(transient_error(), 5)))
        self.assertTrue(self._classify(with_winerror(transient_error(), 32)))

    def test_errors_that_are_not_the_swap_window_are_not_retried(self) -> None:
        self.assertFalse(
            self._classify(FileNotFoundError(errno.ENOENT, "No such file"))
        )
        self.assertFalse(
            self._classify(IsADirectoryError(errno.EISDIR, "Is a directory"))
        )
        self.assertFalse(self._classify(with_winerror(transient_error(), 2)))

    def test_posix_never_retries_an_access_denial(self) -> None:
        # A retry is only honest where the window exists at all.
        self.assertFalse(self._classify(transient_error(), platform="posix"))
        self.assertFalse(
            self._classify(with_winerror(transient_error(), 5), platform="posix")
        )

    def test_a_read_that_lands_in_the_swap_window_is_retried(self) -> None:
        transient = transient_error()
        real = Path.read_text
        attempts: list[int] = []

        def flaky(path: Path, *args: object, **kwargs: object) -> str:
            attempts.append(1)
            if len(attempts) <= 2:
                raise transient
            return real(path, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(
            self.registry_module, "_is_transient_sharing_error", lambda error: True
        ):
            with patch.object(Path, "read_text", flaky):
                providers = self.registry.providers()

        self.assertEqual([item.provider_id for item in providers], ["local"])
        self.assertEqual(len(attempts), 3)

    def test_a_permanent_read_error_is_not_retried(self) -> None:
        # Retrying is only for the transient window: an error the classifier
        # rejects must surface immediately, once, naming the registry.
        permanent = FileNotFoundError(errno.ENOENT, "No such file")
        attempts: list[int] = []

        def broken(path: Path, *args: object, **kwargs: object) -> str:
            attempts.append(1)
            raise permanent

        with patch.object(
            self.registry_module, "_is_transient_sharing_error", lambda error: False
        ):
            with patch.object(Path, "read_text", broken):
                with self.assertRaises(RegistryError) as raised:
                    self.registry.providers()

        self.assertEqual(len(attempts), 1)
        self.assertIn("cannot read provider registry", str(raised.exception))

    def test_a_swap_that_meets_an_open_reader_is_retried(self) -> None:
        transient = transient_error()
        real_replace = os.replace
        attempts: list[int] = []

        def flaky(source: object, target: object) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise transient
            real_replace(source, target)  # type: ignore[arg-type]

        with patch.object(
            self.registry_module, "_is_transient_sharing_error", lambda error: True
        ):
            with patch("karox.registry.os.replace", flaky):
                self.registry.put_provider(provider("second"))

        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            [item.provider_id for item in self.registry.providers()],
            ["local", "second"],
        )

    def test_a_failed_swap_still_reports_after_the_retry_budget(self) -> None:
        # The retry is bounded: a file that stays unwritable must not spin.
        permanent = FileNotFoundError(errno.ENOENT, "No such file")
        attempts: list[int] = []

        def broken(source: object, target: object) -> None:
            attempts.append(1)
            raise permanent

        with patch.object(
            self.registry_module, "_is_transient_sharing_error", lambda error: False
        ):
            with patch("karox.registry.os.replace", broken):
                with self.assertRaises(FileNotFoundError):
                    self.registry.put_provider(provider("second"))

        self.assertEqual(len(attempts), 1)

    def test_concurrent_reads_and_writes_never_surface_a_sharing_error(self) -> None:
        # The registry is saved from a worker thread while the status path reads
        # it, so both sides must pass through the real window instead of failing.
        #
        # The readers pause between reads on purpose. A reader that re-opens the
        # file in a tight loop keeps it open ~99% of the time (measured: 396 of
        # 400 swaps collided), and no bounded retry can win that race -- so a
        # spinning reader would assert arithmetic instead of behaviour. A real
        # reader is a periodic poll, which is what this models.
        stop = threading.Event()
        failures: list[str] = []

        def writer() -> None:
            try:
                for index in range(30):
                    self.registry.put_provider(provider(f"p{index}"))
            except Exception as error:  # noqa: BLE001 - the point is to record it
                failures.append(f"write: {error!r}")
            finally:
                stop.set()

        def reader() -> None:
            while not stop.is_set():
                try:
                    self.registry.providers()
                except Exception as error:  # noqa: BLE001 - the point is to record it
                    failures.append(f"read: {error!r}")
                time.sleep(0.001)

        threads = [threading.Thread(target=writer)]
        threads.extend(threading.Thread(target=reader) for _ in range(2))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
