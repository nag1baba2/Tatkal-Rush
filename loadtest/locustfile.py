"""The Tatkal rush: many users try to book a handful of seats at the same instant.

Each simulated user wants ONE seat and behaves like the real UI:
  * its first pick is random (everyone rushes the same few seats); if that seat is
    taken it looks at the seat map and picks a free one, waiting and re-checking while
    none is free (holds expire, so seats come back) until it runs out of patience;
  * every booking and payment carries an Idempotency-Key. A request whose outcome is
    unknown (timeout, 5xx, 429) is retried with the SAME key; a declined payment is a
    new attempt with a NEW key;
  * about DUPLICATE_RATE of its requests are sent twice at once with the same key,
    like a double-click.
  * after a failed payment it pauses 1-3 s before pressing Pay again, like a person would.

Locust's own "failures" mean real faults (timeouts, 5xx). Expected business answers
are NOT failures: 409 seat taken, 402 payment declined, 429 rate limited.

What the system did is judged afterwards by tools/check_invariants.py, not by this file.
"""

import itertools
import json
import os
import random
import time
import uuid
from collections import Counter

import gevent
from locust import HttpUser, constant, events, task
from locust.exception import StopUser

RUN = json.load(open(os.environ.get("RUN_FILE", "/loadtest-data/run.json")))
OUT_PREFIX = os.environ.get("OUT_PREFIX", "")

USERS = RUN["users"]
SEAT_IDS = RUN["seat_ids"]
SCHEDULE_ID = RUN["schedule_id"]
ROUNDS = int(RUN.get("rounds", 1))   # bookings each user makes, one after another

TOTAL_USERS = int(os.environ.get("TOTAL_USERS", len(USERS)))
SEAT_PATIENCE_S = float(os.environ.get("SEAT_PATIENCE_S", 40))
MAX_SEAT_ATTEMPTS = int(os.environ.get("MAX_SEAT_ATTEMPTS", 8))
PAY_ATTEMPTS = int(os.environ.get("PAY_ATTEMPTS", 3))
DUPLICATE_RATE = float(os.environ.get("DUPLICATE_RATE", 0.10))
CLIENT_TIMEOUT_S = float(os.environ.get("CLIENT_TIMEOUT_S", 30))
MAX_TRIES = 5            # retries of one logical request (same idempotency key)
PAYMENT_WAIT_S = 30      # how long to wait for an in-progress (202) payment
PAY_RETRY_PAUSE_MIN_S = float(os.environ.get("PAY_RETRY_PAUSE_MIN_S", 1.0))
PAY_RETRY_PAUSE_MAX_S = float(os.environ.get("PAY_RETRY_PAUSE_MAX_S", 3.0))

EXPECTED = {
    "POST /bookings": {200, 201, 409, 429},
    "POST /bookings/{id}/pay": {200, 202, 402, 409, 429},
    "GET /schedules/{id}/seats": {200, 429},
    "GET /bookings/{id}/payment": {200, 429},
}

OUTCOMES: Counter = Counter()
RETRIES: Counter = Counter()
DUPLICATES: Counter = Counter()
STATUS_CODES: Counter = Counter()
CONNECTION_ERRORS: Counter = Counter()   # what 'HTTP 0' actually was (timeout, reset, ...)
CONFIRMED_USER_IDS: list[int] = []
_next_user = itertools.count()
_finished = 0
_quit_scheduled = False


class Buyer(HttpUser):
    host = os.environ.get("TARGET", "http://api:8000")
    wait_time = constant(0)

    def on_start(self):
        index = next(_next_user)
        # Locust keeps the number of RUNNING users at the target, so when users finish and
        # stop it spawns replacements. Those must do nothing: each of the TOTAL_USERS
        # identities makes its journey(s) exactly once. (They stop from inside the task:
        # raising StopUser from on_start makes Locust log an ERROR traceback.)
        self.spare = index >= TOTAL_USERS
        if not self.spare:
            identity = USERS[index]
            self.user_id = identity["id"]
            self.token = identity["token"]

    # ---- one HTTP call, classified ---------------------------------------------------

    def call(self, method, path, name, key=None, body=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        with self.client.request(
            method, path, name=name, headers=headers, json=body,
            catch_response=True, timeout=CLIENT_TIMEOUT_S,
        ) as resp:
            code = resp.status_code
            STATUS_CODES[f"{name} {code}"] += 1
            if code in EXPECTED[name]:
                resp.success()
            else:
                reason = ""
                if code == 0:  # no HTTP response at all: record what really happened
                    error = getattr(resp, "error", None)
                    reason = type(error).__name__ if error else "unknown"
                    CONNECTION_ERRORS[reason] += 1
                resp.failure(f"HTTP {code} {reason}".strip())  # 0 = no response, 5xx = server fault
            try:
                data = resp.json()
            except Exception:
                data = None
            return code, data, resp.headers.get("Retry-After")

    def with_retries(self, kind, method, path, name, key, body=None):
        """Retry outcomes that are UNKNOWN (timeout, 5xx, 429) with the SAME key."""
        code, data = 0, None
        for attempt in range(MAX_TRIES):
            code, data, retry_after = self.call(method, path, name, key, body)
            if code == 429:
                wait = float(retry_after or 1)
            elif code == 0 or code >= 500:
                wait = min(3.0, 0.2 * 2 ** attempt)
            else:
                return code, data
            RETRIES[kind] += 1
            gevent.sleep(wait + random.random() * 0.3)
        return code, data

    def maybe_double_click(self, kind, method, path, name, key, body=None):
        """Sometimes send the identical request twice at once, as a double-click does."""
        if random.random() >= DUPLICATE_RATE:
            return self.with_retries(kind, method, path, name, key, body)
        DUPLICATES[kind] += 1
        twin = gevent.spawn(self.with_retries, kind, method, path, name, key, body)
        result = self.with_retries(kind, method, path, name, key, body)
        twin.join()
        return result

    # ---- the user's journey ------------------------------------------------------------

    def book(self, seat_id):
        body = {"schedule_id": SCHEDULE_ID, "seat_id": seat_id}
        code, data = self.maybe_double_click(
            "booking", "POST", "/bookings", "POST /bookings", uuid.uuid4().hex, body
        )
        if code in (200, 201):
            return "held", data
        return ("taken", None) if code == 409 else ("error", None)

    def wait_for_payment(self, booking_id):
        deadline = time.time() + PAYMENT_WAIT_S
        while time.time() < deadline:
            gevent.sleep(1.5 + random.random())
            code, data, _ = self.call("GET", f"/bookings/{booking_id}/payment", "GET /bookings/{id}/payment")
            if code == 200 and data and data["status"] != "PENDING":
                return data["status"]
        return None

    def pay(self, booking_id):
        for attempt in range(PAY_ATTEMPTS):
            if attempt:
                # A person reads "payment failed" and presses Pay again a moment later; they do
                # not retry within milliseconds. (Instant retries made every user burn all their
                # attempts inside a single 10 s circuit-breaker window.)
                gevent.sleep(random.uniform(PAY_RETRY_PAUSE_MIN_S, PAY_RETRY_PAUSE_MAX_S))
            code, data = self.maybe_double_click(
                "payment", "POST", f"/bookings/{booking_id}/pay",
                "POST /bookings/{id}/pay", uuid.uuid4().hex,   # a new key per attempt
            )
            if code == 200:
                return "confirmed"
            if code == 409:
                return "hold_expired"
            if code == 402:
                continue                      # this attempt failed: try again, new key
            if code == 202:
                settled = self.wait_for_payment(booking_id)
                if settled == "SUCCESS":
                    return "confirmed"
                if settled == "FAILED":
                    continue
                return "payment_unresolved"   # the background worker will settle it
            return "payment_error"
        return "payment_abandoned"

    def pick_available_seat(self, started):
        while True:
            code, data, retry_after = self.call("GET", f"/schedules/{SCHEDULE_ID}/seats", "GET /schedules/{id}/seats")
            if code == 200:
                free = [s["id"] for s in data if s["status"] == "AVAILABLE"]
                if free:
                    return random.choice(free)
            if time.time() - started > SEAT_PATIENCE_S:
                return None
            gevent.sleep(float(retry_after or 0) + 2 + random.random() * 3)

    def one_journey(self):
        """Try to buy one seat. Returns how it ended."""
        started = time.time()
        seat = random.choice(SEAT_IDS)
        for _ in range(MAX_SEAT_ATTEMPTS):
            state, booking = self.book(seat)
            if state == "held":
                return self.pay(booking["id"])
            if state == "error":
                return "booking_error"
            seat = self.pick_available_seat(started)
            if seat is None:
                return "sold_out"
        return "gave_up"

    @task
    def rush(self):
        if self.spare:
            raise StopUser()
        for _ in range(ROUNDS):
            outcome = self.one_journey()
            OUTCOMES[outcome] += 1
            if outcome == "confirmed":
                CONFIRMED_USER_IDS.append(self.user_id)
        self.finish()

    def finish(self):
        global _finished, _quit_scheduled
        _finished += 1
        if _finished >= TOTAL_USERS and not _quit_scheduled and self.environment.runner:
            _quit_scheduled = True
            gevent.spawn_later(1, self.environment.runner.quit)  # everyone is done, once
        # StopUser ends THIS user right now. (self.stop() is graceful: Locust may start another
        # task first, which made some users run their journey twice.)
        raise StopUser()


@events.quitting.add_listener
def write_outcomes(environment, **_):
    if not OUT_PREFIX:
        return
    with open(f"{OUT_PREFIX}_outcomes.json", "w") as f:
        json.dump({
            "users": TOTAL_USERS,
            "rounds": ROUNDS,
            "unfinished_users": TOTAL_USERS - _finished,
            "outcomes": dict(OUTCOMES),
            "retries": dict(RETRIES),
            "duplicates_sent": dict(DUPLICATES),
            "status_codes": dict(STATUS_CODES),
            "connection_errors": dict(CONNECTION_ERRORS),
            "journeys": sum(OUTCOMES.values()),
            "journeys_expected": TOTAL_USERS * ROUNDS,
            "confirmed_user_ids": CONFIRMED_USER_IDS,
        }, f)
