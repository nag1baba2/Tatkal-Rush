"""Circuit breaker, retry with backoff, and the fake gateway, in isolation."""

import random
import threading

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.gateway import (
    GatewayDeclined,
    GatewayTimeout,
    GatewayUnavailable,
)
from app.retry import retry_with_backoff


# ---- circuit breaker -------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def boom():
    raise RuntimeError("down")


def fine():
    return "ok"


@pytest.fixture
def clock():
    return Clock()


def make_breaker(clock, threshold=3, recovery=10.0):
    return CircuitBreaker(failure_threshold=threshold, recovery_timeout=recovery, clock=clock)


def trip(breaker, n):
    for _ in range(n):
        with pytest.raises(RuntimeError):
            breaker.call(boom)


def test_breaker_stays_closed_below_the_threshold(clock):
    b = make_breaker(clock, threshold=3)
    trip(b, 2)
    assert b.state == "closed"
    assert b.call(fine) == "ok"


def test_a_success_resets_the_failure_count(clock):
    b = make_breaker(clock, threshold=3)
    trip(b, 2)
    b.call(fine)
    trip(b, 2)  # only 2 consecutive again, so still closed
    assert b.state == "closed"


def test_breaker_opens_after_threshold_and_refuses_without_calling(clock):
    b = make_breaker(clock, threshold=3)
    trip(b, 3)
    assert b.state == "open"
    calls = []
    with pytest.raises(CircuitOpenError):
        b.call(lambda: calls.append(1))
    assert calls == []  # the dependency was never touched


def test_breaker_goes_half_open_after_the_recovery_timeout(clock):
    b = make_breaker(clock, threshold=1, recovery=10)
    trip(b, 1)
    clock.t = 9.9
    assert b.state == "open"
    clock.t = 10.0
    assert b.state == "half_open"


def test_successful_probe_closes_the_circuit(clock):
    b = make_breaker(clock, threshold=1, recovery=10)
    trip(b, 1)
    clock.t = 10
    assert b.call(fine) == "ok"
    assert b.state == "closed"
    assert b.call(fine) == "ok"


def test_failed_probe_reopens_and_restarts_the_timer(clock):
    b = make_breaker(clock, threshold=1, recovery=10)
    trip(b, 1)
    clock.t = 10
    trip(b, 1)  # the probe fails
    assert b.state == "open"
    clock.t = 19.9  # 9.9s after the failed probe, not yet recovered
    assert b.state == "open"
    clock.t = 20.0
    assert b.state == "half_open"


def test_only_one_probe_is_allowed_while_half_open(clock):
    b = make_breaker(clock, threshold=1, recovery=10)
    trip(b, 1)
    clock.t = 10

    release = threading.Event()
    started = threading.Event()

    def slow_probe():
        started.set()
        release.wait(5)
        return "ok"

    t = threading.Thread(target=lambda: b.call(slow_probe))
    t.start()
    assert started.wait(5)
    with pytest.raises(CircuitOpenError):  # a second caller is turned away
        b.call(fine)
    release.set()
    t.join(5)
    assert b.state == "closed"


def test_errors_that_are_not_failures_do_not_open_the_circuit(clock):
    b = make_breaker(clock, threshold=2)

    def declined():
        raise GatewayDeclined()

    for _ in range(10):
        with pytest.raises(GatewayDeclined):
            b.call(declined, is_failure=lambda e: not isinstance(e, GatewayDeclined))
    assert b.state == "closed"


# ---- retry with backoff -----------------------------------------------------

class Flaky:
    def __init__(self, failures, exc=GatewayUnavailable):
        self.failures = failures
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc()
        return "done"


def run_retry(fn, **kw):
    delays = []
    kwargs = dict(attempts=4, base_delay=0.1, max_delay=2.0,
                  retry_on=(GatewayUnavailable,), sleep=delays.append, rng=lambda: 1.0)
    kwargs.update(kw)
    result = retry_with_backoff(fn, **kwargs)
    return result, delays


def test_retry_doubles_the_delay_ceiling_each_time():
    fn = Flaky(failures=3)
    result, delays = run_retry(fn)
    assert result == "done"
    assert fn.calls == 4
    assert delays == pytest.approx([0.1, 0.2, 0.4])


def test_retry_caps_the_delay():
    fn = Flaky(failures=4)
    _, delays = run_retry(fn, attempts=5, base_delay=1.0, max_delay=2.0)
    assert delays == pytest.approx([1.0, 2.0, 2.0, 2.0])


def test_retry_applies_jitter():
    fn = Flaky(failures=2)
    _, delays = run_retry(fn, rng=lambda: 0.5)
    assert delays == pytest.approx([0.05, 0.10])


def test_retry_stops_at_the_first_success():
    fn = Flaky(failures=0)
    result, delays = run_retry(fn)
    assert (result, fn.calls, delays) == ("done", 1, [])


def test_retry_raises_the_last_error_when_exhausted():
    fn = Flaky(failures=99)
    delays = []
    with pytest.raises(GatewayUnavailable):
        retry_with_backoff(fn, attempts=3, base_delay=0.1, max_delay=2.0,
                           retry_on=(GatewayUnavailable,), sleep=delays.append,
                           rng=lambda: 1.0)
    assert fn.calls == 3
    assert len(delays) == 2  # no sleep after the final failure


def test_retry_does_not_retry_other_errors():
    fn = Flaky(failures=99, exc=GatewayDeclined)
    with pytest.raises(GatewayDeclined):
        run_retry(fn)
    assert fn.calls == 1


# ---- fake gateway ------------------------------------------------------------

def reliable(make_gateway, **kw):
    return make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0, **kw)


def test_gateway_charges_once_per_idempotency_key(make_gateway):
    gw = reliable(make_gateway)
    refs = {gw.charge("key-1", 500).ref for _ in range(5)}
    assert len(refs) == 1
    assert len(gw.ledger_entries()) == 1
    assert gw.stats()["replays"] == 4


def test_different_keys_are_charged_separately(make_gateway):
    gw = reliable(make_gateway)
    gw.charge("a", 100)
    gw.charge("b", 100)
    assert len(gw.ledger_entries()) == 2


def test_failure_before_processing_does_not_charge(make_gateway):
    gw = make_gateway(fail_rate=1.0, lost_response_rate=0, decline_rate=0)
    with pytest.raises(GatewayUnavailable):
        gw.charge("k", 100)
    assert gw.ledger_entries() == []


def test_decline_does_not_charge(make_gateway):
    gw = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=1.0)
    with pytest.raises(GatewayDeclined):
        gw.charge("k", 100)
    assert gw.ledger_entries() == []


def test_lost_response_charges_but_retry_with_same_key_does_not_charge_again(make_gateway):
    gw = make_gateway(fail_rate=0, lost_response_rate=1.0, decline_rate=0,
                      lost_replay_rate=0.0)
    with pytest.raises(GatewayTimeout):
        gw.charge("k", 100)
    assert len(gw.ledger_entries()) == 1  # the money DID move

    result = gw.charge("k", 100)  # the retry
    assert result.ref == gw.ledger_entries()[0]["ref"]
    assert len(gw.ledger_entries()) == 1  # and was not charged again


def test_a_replay_can_lose_its_response_too(make_gateway):
    gw = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0,
                      lost_replay_rate=1.0)
    gw.charge("k", 100)  # first call: fine
    for _ in range(3):
        with pytest.raises(GatewayTimeout):
            gw.charge("k", 100)  # every replay times out
    assert len(gw.ledger_entries()) == 1  # and still only one charge


def test_failure_rates_roughly_match_the_configuration(make_gateway):
    gw = make_gateway(fail_rate=0.3, lost_response_rate=0.0, decline_rate=0.1,
                      rng=random.Random(1))
    n = 2000
    for i in range(n):
        try:
            gw.charge(f"k{i}", 100)
        except (GatewayUnavailable, GatewayDeclined):
            pass
    stats = gw.stats()
    assert 0.25 < stats["unavailable"] / n < 0.35
    assert 0.06 < stats["declines"] / n < 0.14
