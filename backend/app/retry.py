"""Retry with exponential backoff and full jitter."""

import random
import time
from typing import Callable


def retry_with_backoff(
    fn: Callable,
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    retry_on: tuple[type[BaseException], ...],
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
):
    """Call fn up to `attempts` times.

    Only exceptions in `retry_on` are retried; anything else propagates at once.
    Before retry n (0-based) it sleeps rng() * min(max_delay, base_delay * 2**n):
    the ceiling doubles each time, and the random factor ("full jitter") stops a
    crowd of clients from retrying in lockstep.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on:
            if attempt == attempts - 1:
                raise
            sleep(rng() * min(max_delay, base_delay * (2 ** attempt)))
