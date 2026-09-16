from karox.route_health import RouteHealthTracker


def test_route_health_uses_fast_confirmation_after_first_failure() -> None:
    tracker = RouteHealthTracker(
        interval_seconds=5.0,
        failure_probe_interval_seconds=1.0,
        failure_threshold=2,
    )

    assert tracker.observe(now=10.0, healthy=False) is False
    assert tracker.consecutive_failures == 1
    assert tracker.next_probe_at == 11.0
    assert tracker.due(10.9) is False
    assert tracker.due(11.0) is True

    assert tracker.observe(now=11.0, healthy=False) is True
    assert tracker.consecutive_failures == 2
    assert tracker.next_probe_at == 12.0


def test_route_health_success_restores_steady_cadence() -> None:
    tracker = RouteHealthTracker(
        interval_seconds=5.0,
        failure_probe_interval_seconds=1.0,
        failure_threshold=2,
    )

    tracker.observe(now=20.0, healthy=False)
    assert tracker.observe(now=21.0, healthy=True) is False
    assert tracker.consecutive_failures == 0
    assert tracker.next_probe_at == 26.0


def test_route_health_failure_cadence_is_never_slower_than_steady_cadence() -> None:
    # The runtime contract is the invariant the old rejection test guarded,
    # expressed differently: a misconfigured slower failure cadence must never
    # make confirmation slower than the steady public probe cadence. The
    # tracker clamps such a policy to the healthy cadence instead of refusing a
    # configuration deterministic tests and aggressive local policies rely on.
    tracker = RouteHealthTracker(interval_seconds=1.0, failure_probe_interval_seconds=2.0)
    assert tracker.failure_probe_interval_seconds == 1.0

    try:
        RouteHealthTracker(interval_seconds=0.0)
    except ValueError as exc:
        assert "probe interval" in str(exc)
    else:
        raise AssertionError("non-positive health cadence was accepted")

    try:
        RouteHealthTracker(interval_seconds=1.0, failure_probe_interval_seconds=0.0)
    except ValueError as exc:
        assert "failure probe interval" in str(exc)
    else:
        raise AssertionError("non-positive failure cadence was accepted")
