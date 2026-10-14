"""Circuit breaker.

CLOSED     normal: calls go through; consecutive failures are counted.
OPEN       too many failures: calls are refused instantly (CircuitOpenError)
           without touching the broken dependency.
HALF_OPEN  after recovery_timeout, exactly ONE probe call is allowed. If it
           succeeds the circuit closes; if it fails the circuit re-opens.

State is per process (thread-safe). With several API workers each one trips on
its own, which is simpler and good enough here; sharing the state through Redis
would make all workers trip together at the cost of an extra round trip per call.
"""

import threading
import time
from typing import Callable


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            if (
                self._state == self.OPEN
                and self._clock() - self._opened_at >= self.recovery_timeout
            ):
                return self.HALF_OPEN
            return self._state

    def call(self, fn, *args, is_failure=lambda exc: True, **kwargs):
        """Run fn through the breaker. is_failure(exc) says whether an exception
        counts against the dependency (a card decline, for example, does not)."""
        self._before_call()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            self._after_call(failed=is_failure(exc))
            raise
        self._after_call(failed=False)
        return result

    def _before_call(self) -> None:
        with self._lock:
            if self._state == self.OPEN:
                if self._clock() - self._opened_at >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
                    self._probe_in_flight = False
                else:
                    raise CircuitOpenError()
            if self._state == self.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError()
                self._probe_in_flight = True

    def _after_call(self, failed: bool) -> None:
        with self._lock:
            if self._state == self.HALF_OPEN:
                self._probe_in_flight = False
                if failed:
                    self._state = self.OPEN
                    self._opened_at = self._clock()
                else:
                    self._state = self.CLOSED
                    self._failures = 0
                return
            if self._state == self.OPEN:
                return  # a call that started before the circuit opened
            if failed:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._state = self.OPEN
                    self._opened_at = self._clock()
            else:
                self._failures = 0
