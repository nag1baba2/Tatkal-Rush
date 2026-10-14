"""A simulated payment gateway that fails randomly.

It behaves like a real one in the ways that matter for correctness:
- It honours an idempotency key: the same key never charges twice.
- A call can fail BEFORE processing (no money moved)  -> GatewayUnavailable.
- A call can be charged but the response lost         -> GatewayTimeout.
  The client cannot tell this apart from "nothing happened", which is exactly
  why payments must be retried with the same key and never blindly marked failed.
  The response can be lost on a retry too (lost_replay_rate), so a payment can
  stay unresolved after several attempts.
- A card can be declined, which is final and not retryable -> GatewayDeclined.

State lives in Redis so it survives API restarts and is shared by all workers:
  <prefix>charge:<key>  the recorded charge for an idempotency key (SET NX = charge once)
  <prefix>ledger        append-only list of every real charge (the "bank statement")
  <prefix>stats         counters
"""

import json
import random
import time
from dataclasses import dataclass
from uuid import uuid4

import redis


class GatewayError(Exception):
    pass


class GatewayUnavailable(GatewayError):
    """Rejected before processing (like a 503). Definitely no charge. Retryable."""


class GatewayTimeout(GatewayError):
    """No usable response. The charge MAY have happened. Retryable, outcome unknown."""


class GatewayDeclined(GatewayError):
    """Definitive refusal. No charge. Not retryable."""


@dataclass
class ChargeResult:
    ref: str


class FakeGateway:
    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        fail_rate: float = 0.3,
        lost_response_rate: float = 0.1,
        decline_rate: float = 0.05,
        latency_ms: int = 0,
        rng: random.Random | None = None,
        prefix: str = "gw:",
        lost_replay_rate: float | None = None,
    ):
        self.redis = redis_client
        self.fail_rate = fail_rate
        self.lost_response_rate = lost_response_rate
        # Chance that a replay of an already-charged key also loses its response.
        self.lost_replay_rate = (
            lost_response_rate if lost_replay_rate is None else lost_replay_rate
        )
        self.decline_rate = decline_rate
        self.latency_ms = latency_ms
        self.rng = rng or random.Random()
        self.prefix = prefix

    def _k(self, *parts: str) -> str:
        return self.prefix + ":".join(parts)

    def _incr(self, stat: str) -> None:
        self.redis.hincrby(self._k("stats"), stat, 1)

    def charge(self, idempotency_key: str, amount_paise: int) -> ChargeResult:
        self._incr("calls")
        charge_key = self._k("charge", idempotency_key)

        # Idempotency: a key that already produced a charge returns the original.
        existing = self.redis.get(charge_key)
        if existing is not None:
            self._incr("replays")
            if self.rng.random() < self.lost_replay_rate:
                self._incr("timeouts")
                raise GatewayTimeout()
            return ChargeResult(ref=json.loads(existing)["ref"])

        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)

        roll = self.rng.random()
        if roll < self.decline_rate:
            self._incr("declines")
            raise GatewayDeclined()
        if roll < self.decline_rate + self.fail_rate:
            self._incr("unavailable")
            raise GatewayUnavailable()

        ref = f"gw_{uuid4().hex[:16]}"
        record = json.dumps({"ref": ref, "key": idempotency_key, "amount": amount_paise})
        # SET NX makes "charge once per key" atomic even for identical concurrent calls.
        if self.redis.set(charge_key, record, nx=True):
            self.redis.rpush(self._k("ledger"), record)
            self._incr("charges")
        else:
            ref = json.loads(self.redis.get(charge_key))["ref"]

        if self.rng.random() < self.lost_response_rate:
            self._incr("timeouts")
            raise GatewayTimeout()
        return ChargeResult(ref=ref)

    # ---- inspection helpers (tests and the Phase 7 invariant checker) -------

    def ledger_entries(self) -> list[dict]:
        return [json.loads(x) for x in self.redis.lrange(self._k("ledger"), 0, -1)]

    def stats(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.redis.hgetall(self._k("stats")).items()}

    def reset(self) -> None:
        for key in self.redis.scan_iter(match=self.prefix + "*"):
            self.redis.delete(key)
