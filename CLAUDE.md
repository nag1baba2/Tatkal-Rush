# Tatkal-Rush — project notes for Claude

IRCTC Tatkal-style booking backend. The value of this project is **correctness
under concurrency** (no double-booked seats, no double charges), not UI polish.
Resume portfolio project for SDE internships (payments/infra roles).

## Scope rule
Only edit files inside this folder (`Tatkal-Rush`). Do not touch `Research_Code`.

## Stack (do not change without asking the user)
Python 3.12 (in Docker), FastAPI, PostgreSQL 16, Redis 7, Celery 5.6, SQLAlchemy 2.x,
Alembic, **React + Vite** (thin UI), Locust, Docker Compose, GitHub Actions.

Decision log:
- **React + Vite instead of Next.js** (user knows Node + basic React; SSR not needed).
- **Money is stored as integer paise**, never floats.
- **Seats belong to a schedule** (one train on one date): 4 coaches x 18 seats = 72
  seats per schedule. Single class; multi-class and partial-route segments are
  out of scope ("future work").
- **Tests run against real Postgres**, not SQLite (partial indexes/locks differ).
- **Backend runs only in Docker**; no local Python needed (host has 3.10, image uses 3.12).
- Postgres is exposed on host port **5433** (avoids clashing with a local Postgres).
- JWT auth is deliberately small: register, login, bearer token, 60 min expiry,
  bcrypt hashes, no refresh tokens/roles. Login returns the same 401 for unknown
  email and wrong password (and does a dummy hash check to equalise timing).
- Hold time is configurable: `HOLD_SECONDS` (default 300) so Phase 4 tests need not wait 5 min.
- Gateway fault rates are configurable (`GATEWAY_FAIL_RATE` 0.3, `GATEWAY_LOST_RESPONSE_RATE`
  0.1, `GATEWAY_DECLINE_RATE` 0.05, `GATEWAY_LATENCY_MS` 0), via env in docker-compose.
- **A failed payment keeps the seat HELD** (user may retry with a new key until the hold
  expires), like real IRCTC.
- **Sweep interval 10 s** (`EXPIRY_SWEEP_SECONDS`); the worker **auto re-drives stale PENDING
  payments** (user's choices for Phase 4).
- Redis server holds: db 0 = gateway ledger/state + sweep pause flag, db 1 = Celery broker.
- `redis` is pinned to 6.4.0 because kombu (Celery) requires `redis<6.5`.
- **Rate limits (user's choice: defaults):** per identity (user from JWT, else client IP) 10 burst /
  5 per second refill; plus a per-IP ceiling of 100 burst / 50 per second. **Fail open** if Redis is
  down (user's choice). All configurable via `RATE_LIMIT_*` env vars.
- **UI (user's choices):** React + Vite, plain JavaScript (no TypeScript), no router and no UI library;
  session token in **localStorage**; the UI runs on the **Vite dev server on the host** (port 5173),
  not in Docker. CORS allows `http://localhost:5173` and `http://127.0.0.1:5173` (`CORS_ORIGINS`).
- Circuit breaker state is **per process** (thread-safe), not shared via Redis. Simpler;
  with N workers each trips on its own. Trade-off is documented in `circuit_breaker.py`.
- Git was initialised locally (branch `main`) in Phase 8 with the user's go-ahead. Nothing is pushed; do not push or
  create a remote unless asked.

## Booking design (see README "Design decisions")
Three independent layers stop double booking (`app/services/booking.py`):
1. Row lock: `SELECT ... FOR UPDATE` on the seat serialises competing requests.
2. Re-check under the lock: loser sees the winner's committed row -> `SeatUnavailable` (409).
3. DB backstop: partial unique index `uq_bookings_active_seat` on `seat_id`
   `WHERE status IN ('HELD','CONFIRMED')`.
Relies on Postgres default READ COMMITTED isolation.

Idempotency: fast-path lookup by key, then re-check after taking the lock. Replay -> 200 +
`Idempotent-Replay: true`; same key with a different seat -> 422.
Expired holds are released **lazily inside the booking path** (under the seat lock), so
correctness never depends on the Celery worker. BUT a hold with a PENDING payment is never
released.

## Payment design (`app/services/payment.py`)
Goals: never charge twice, never charge without giving the seat, never lose track of money.
`pay_booking` = 3 steps: (1) DECIDE under the booking row lock: replay a stored result, or move
the payment to PENDING and COMMIT; (2) CHARGE outside any transaction (no DB connection held)
with gateway key `pay-<payment id>-<attempt>` through retry+backoff+circuit breaker;
(3) RECORD under booking lock then payment lock: SUCCESS -> payment SUCCESS + booking
CONFIRMED atomically; FAILED -> seat stays held; UNKNOWN -> stays PENDING.
- **Timeouts are ambiguous** (money may have moved) so they are NEVER recorded as FAILED. A
  PENDING payment older than `PAYMENT_STALE_SECONDS` (30) is re-driven with the SAME gateway
  key; the gateway returns the original charge instead of charging again. The re-drive is
  claimed by refreshing `updated_at` so concurrent requests see "in progress".
- State machine: CREATED -> PENDING -> SUCCESS | FAILED; FAILED -> PENDING (retry). Enforced in
  Python (`payment_state.py`) AND by a Postgres trigger (migration 0002); a payment must be
  inserted as CREATED.
- One payment row per booking (`UNIQUE(booking_id)`). Same key after FAILED = replay of the
  failure; a different key after FAILED = new attempt (attempts+1, key updated). A key used by
  another booking -> 409.
- HTTP: 200 paid, 402 attempt failed, 202 in progress/unresolved, 409 not payable / key used,
  404 not your booking.
- **Lock order everywhere: seat -> booking -> payment.** Never reverse it.
- Every lock-taking query uses `populate_existing=True` so it sees the row's CURRENT state
  (the session loaded these rows earlier in the same request).
- Pay endpoint runs the gateway call (and backoff sleeps) on a request worker thread. With 40
  threads that caps concurrent payments; fine for now, note it for Phase 7 results.

## Frontend design (`frontend/`)
Files: `src/api.js` (client + error classes), `src/format.js` (money/time/messages/keys),
`src/session.js` (localStorage), `src/App.jsx`, `src/components/{AuthBar,Search,SeatMap,BookingPanel,MyBookings}.jsx`.
- **Idempotency keys are chosen by the UI, per attempt.** Reuse the SAME key while the outcome is
  unknown (network drop, 429, 5xx, a 202 in-progress payment); discard it after a definite answer
  (success, or a 4xx other than 429, `isDefiniteOutcome`). A FAILED payment (402) is final, so the next
  press is a NEW attempt with a new key. This is what makes retries unable to double-book or double-charge.
- Payment responses 200/202/402 are RESULTS (returned by `payBooking`), not exceptions.
- A hold with a payment in flight is shown as "in progress", never "expired", even when overdue
  (the server will not release it either). The panel polls `GET /bookings/{id}/payment` until it settles.
- Seat map refreshes every 3 s. A dropped connection during a background refresh shows a small inline
  note that clears itself; it must NOT raise the global error banner (it used to, and the banner stayed
  after the server came back). Real errors (404, 401...) still surface; a 401 logs the user out.
- Session: JWT in `localStorage` (`tatkal.session`), expiry checked client-side, every storage access is
  try/catch'd. XSS trade-off noted in the README; production would use an httpOnly cookie.
- Backend additions for the UI: `GET /trains`, `GET /trains/{id}/schedules`, `GET /bookings` (mine, with
  seat/train/payment details), CORS, and `app/services/availability.py` (the seat map and the date list
  now agree with booking: an overdue hold with a PENDING payment still shows as HELD).
- Money is integer paise in the UI too; only `formatMoney` formats it.

## Load test design (`loadtest/`, `backend/tools/`)
- `loadtest/run_scenario.sh NAME USERS RATE MAX_S on|off WORKERS [SEATS] [ROUNDS]` does everything: sets
  `HOLD_SECONDS=20`, `PAYMENT_STALE_SECONDS=10`, `UVICORN_ARGS="--workers N"`, runs `docker compose up -d`
  then **restarts api/worker/beat** (fresh processes, fresh breaker, picks up code changes), sets or clears the
  rate-limit bypass, provisions, runs Locust in the `locust` compose profile, samples container CPU every 2 s,
  runs the checker (waits up to 90 s for the worker to settle PENDING payments), writes
  `loadtest/data/<NAME>.*` and cleans the DB rows. Needs Git Bash (`MSYS_NO_PATHCONV=1` is set inside).
- `tools.provision` (backend container): isolated train + schedule + N seats + M users with ready-made JWTs
  (shared bcrypt hash, nobody logs in), clears the gateway ledger and `rl:*` buckets, writes `run.json`
  (1,000 JWTs: deleted after runs). `tools.cleanup --run FILE | --all`. `tools.summarize_runs` builds the
  README tables from `*.report.json`.
- `tools.check_invariants`: `load_snapshot()` reads DB + gateway ledger, `evaluate()` is PURE (Snapshot ->
  Checks) so tests feed it corrupted data (21 corruptions, `tests/test_invariants.py`). 19 checks; a
  `[scenario]` check is a property of the test, not a promise of the system. Also compares "users told
  confirmed" (Locust's `*_outcomes.json`) with the DB, and prints WARNINGs if journeys != users x rounds.
- `loadtest/locustfile.py`: each user = one identity, `ROUNDS` journeys; first pick random, later picks from
  the seat map; same-key retries on 0/5xx/429; new key + 1-3 s pause after a declined payment; 10% of
  requests double-clicked (greenlet twin, same key). Expected 409/402/429 are NOT Locust failures.
- Locust 2.46.7. Pitfalls found: Locust keeps the RUNNING user count at the target, so users that `StopUser`
  early get REPLACED (they re-used identities and ran extra journeys); spare users must stop from inside a task
  (StopUser from `on_start` logs an ERROR traceback); `self.stop()` is graceful and lets another task start.
- Reporting pitfall: `UVICORN_ARGS` lives on the host, not in the container, so provision must be passed
  `-e UVICORN_ARGS -e API_WORKERS -e LIMITER_MODE` or the report labels 4-worker runs as 1 worker.

## Rate limiter design (`app/ratelimit.py`)
- Token bucket per key in Redis, updated by ONE Lua script (atomic: refill, take a token, write back).
  The script reads the clock from Redis `TIME` (one shared clock), accepts an optional `now_ms` for
  tests, expires idle buckets after 2x their refill time, and counts allowed/denied/bypassed in a
  stats hash. A naive GET/SET version over-admits badly (measured: 35 and 200 admitted of an allowed
  10 under 200 simultaneous requests); the Lua version admitted exactly 10 every time.
- Global FastAPI dependency (`FastAPI(dependencies=[Depends(rate_limit)])`): checks the per-IP ceiling
  first, then the per-identity bucket. 429 + `Retry-After` + `X-RateLimit-Limit/Remaining`.
  Exempt: `/health`, `/docs`, `/openapi.json`, `/redoc`. Identity comes from verifying the JWT
  signature only (`user_id_from_token`), NEVER the database, so the limiter holds no DB connection
  (avoids the Phase 2 threads-vs-connections deadlock). A forged/expired token falls back to the IP.
- `X-Forwarded-For` is ignored unless `TRUST_FORWARDED_FOR=true` (spoofable; only behind a trusted proxy).
- **Fail open:** any Redis error lets the request through (logs a warning at most every 5 s). A shared
  circuit breaker (3 failures, 2 s recovery) stops each request waiting on a dead Redis. The database
  still guarantees correctness, the limiter only protects capacity. Socket timeouts are 0.25 s.
- **Ops bypass flag** (`set_bypass()` / `clear_bypass()`, Redis key `tatkal:ratelimit:bypass`, always
  with a TTL): while set nothing is limited. The test suite sets it for the whole run (the live API
  would otherwise throttle the HTTP concurrency tests: hundreds of requests from one IP).
  Load runs: `loadtest/run_scenario.sh` sets the bypass for `off` scenarios and clears it for `on`
  (Locust runs from one IP, so with limiting ON the per-IP ceiling refuses ~73% of requests; reported
  separately).
- Limits: bucket state lives in one Redis (not cluster-safe key layout; the stats key is shared);
  anonymous users behind one NAT share a bucket; the ceiling can block legitimate users on a shared IP.

## Background worker design (`app/services/expiry.py`, `app/worker/`)
Logic lives in plain service functions (testable without Celery); `app/worker/tasks.py` wraps them.
- `release_expired_holds`: marks overdue HELD bookings EXPIRED. Housekeeping only: the booking
  path already releases lazily. Uses `FOR UPDATE SKIP LOCKED` (concurrent sweepers/bookings/payments
  never block each other or double-process). NEVER releases a hold with a PENDING payment; checked
  in the candidate query AND again after the row lock (fresh snapshot).
- `redrive_stale_payments`: calls `pay_booking` for PENDING payments older than
  `PAYMENT_STALE_SECONDS` (same code path as a user retry: claims the payment, same gateway key,
  so no double charge). One failing payment does not stop the batch.
- Tasks: `sweep_expired_holds`, `redrive_stale_payments`. Beat schedules both every 10 s with
  `expires` = 3x interval (no backlog behind a dead worker). `acks_late` + `reject_on_worker_lost`
  (sweeps are idempotent so at-least-once is safe). Retry policy: transient DB/Redis errors
  (`OperationalError`, redis `ConnectionError`) retry 3x with exponential backoff + jitter.
- **Pause flag** (`app/worker/control.py`, Redis key `tatkal:sweeps:paused`, always with a TTL): while
  set, both tasks do nothing. Ops kill switch AND how the test suite keeps the live worker off the
  data concurrency tests count exactly (session-wide autouse fixture; resumes at the end).
- Migration 0003 adds partial indexes `ix_bookings_held_expiry` and `ix_payments_pending_updated`.
- A payment unresolved > `PAYMENT_ALERT_SECONDS` (600) logs a warning (stand-in for paging). Seats
  with an unresolved payment stay held until it resolves; there is no escalation/refund flow.
- Compose services: `worker` (celery worker, concurrency 2) and `beat` (scheduler), both wait for the
  API healthcheck (the API runs the migrations). **They do not hot-reload: restart them after code
  changes** (`docker compose restart worker beat`).

## Gotchas learned the hard way (keep these)
- **Threads-vs-connections deadlock:** FastAPI runs sync code on a 40-thread pool. A dependency
  that leaves a DB transaction open keeps its connection while the request waits for a worker
  thread; under 200 simultaneous requests all connections were held by requests that could not
  get a thread -> hang until `pool_timeout`, HTTP 500s. Fix: `get_current_user` calls
  `db.commit()` right after loading the user. Any new dependency touching the DB must do the
  same. Pool exhaustion now returns **503 + Retry-After**.
- A booking INSERT takes `FOR KEY SHARE` on the referenced seat row via the FK. A test holding
  `FOR UPDATE` therefore blocks the insert even if the service forgot its lock. The lock test
  holds `FOR NO KEY UPDATE` (`with_for_update(key_share=True)`).
- **Chaos test was initially vacuous:** the fake gateway only lost the response on the first call,
  so a retry with the same key always succeeded and no payment ever stayed PENDING (that branch
  of the test was dead code). Fixed with `lost_replay_rate` (replays can time out too) and a
  guard assertion that PENDING payments really occur. Always check a test can fail (mutation).
- **NEVER hold a DB connection while a request waits for a thread.** FastAPI runs sync code on a 40-thread
  pool and hops back onto it to build the response. A request whose transaction is still open at that
  point holds a connection while waiting for a thread; under load all threads wait for connections held by
  requests waiting for threads (a stall that only a timeout breaks). Found TWICE, both by load: Phase 2
  (auth dependency) and Phase 7 (every read endpoint). Symptom to recognise: Postgres shows N connections
  "idle in transaction" (N = pool size) and every container's CPU is near zero. **Every endpoint must
  `db.commit()` (or finish via a service that commits) BEFORE it returns**; `get_current_user` and `login`
  do too. `tests/test_no_deadlock.py` enforces it with both pools shrunk to 2.
- The async `get_db` (cleanup on the event loop) is NOT shown necessary by any test (the stall does not
  return with a plain generator); kept as defence in depth for error paths. Don't claim it fixes the stall.
- `rollback()` EXPIRES every loaded object (even with `expire_on_commit=False`): returning an ORM object after
  a rollback makes the response schema silently re-query inside a worker thread. Replay paths `commit()`
  instead. A column the DB fills on UPDATE (`updated_at`) is left unloaded: set it in Python. The test
  `db` fixture uses `expire_on_commit=False` to mirror `SessionLocal` (the default hid these).
- Postgres connections: `api workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW) + celery + other < max_connections`.
  4 workers x 40 = 160 > 100 gave `FATAL: too many clients` -> 472 HTTP 500s. Compose now sets
  `max_connections=200`; pool sizes are env-configurable. `OperationalError` and pool timeouts map to 503.
- Instant retries are not human: a simulated user retrying a declined payment within milliseconds burned all
  3 attempts inside one 10 s circuit-breaker open window (up to ~half of users in a 1x run abandoned). The
  load test now pauses 1-3 s between payment attempts. The breaker still costs real users attempts.
- Compose does not restart an unchanged service, so a code change needs `docker compose restart api worker
  beat` (the load script always does this). `--workers N` runs without `--reload`.
- Dev DB pool: 20 + 20 overflow per API process. Concurrency test engine: 50. Postgres max is 200.
- After raw SQL changes in a test, call `db.expire_all()` (session keeps loaded objects).
- With Redis down: the limiter fails open and booking still works (Postgres only), but `/pay` returns
  500 because the SIMULATED gateway keeps its state in Redis (a real external gateway would not).
  The payment is left PENDING and the worker settles it once Redis is back (observed live).
- Test fixture lesson: cleanup in a fixture must not use a connection a test deliberately broke.
- **Two UI bugs were found only by using the real app in a browser**, not by the unit tests: (1) "No
  upcoming dates" flashed while dates were still loading (loading and empty must be different states);
  (2) an error banner from a background refresh stayed on screen after the server came back. Both now
  have tests that fail without the fix. Always drive the UI for real, not only unit-test it.
- Unhandled server errors (HTTP 500) carry no CORS headers, so a browser shows them as a network error
  ("Can't reach the server"), e.g. `/pay` while Redis is down. Expected 4xx/409/429/503 responses do carry them.
- Browser tooling here: after a page navigation the built-in browser pane was often "hidden" (0x0
  viewport, screenshots time out); `tabs_select` brings it back for clicks. DOM and text reads still work
  when hidden. Visual layout was therefore NOT verified by screenshot.
- `TaskStop` on `npm run dev` kills the npm wrapper but NOT the Vite child process: find and stop the
  PID listening on 5173 (`Get-NetTCPConnection -LocalPort 5173`).
- A background bash command that ends in `grep` exits 1 when nothing matches, and the harness reports it
  as "failed" even though the real command worked.
- A tool call that blocks must finish under 45 s (JS) / the shell timeout; keep samplers short.
- The live worker shares the dev DB with the tests; without the pause flag it could re-drive/expire
  test data. Always run tests through the normal fixtures (they pause it).
- One test run took >5 min wall-clock although pytest reported 10 s; not reproduced in 8 more
  runs, cause unknown (Docker Desktop hiccup suspected, unproven). `pytest.ini` now sets
  `faulthandler_timeout = 120` so a real hang dumps all thread stacks.
- Known test limit: removing ONLY the post-lock re-check in `release_expired_holds` is NOT
  detected by the tests (it guards a few-millisecond window that cannot be forced); removing the
  candidate filter or both layers IS detected.
- A bash one-liner with nested quotes silently failed once and the doc edit did not apply; for
  doc edits use a script file and check every replacement matched.

## Phase status
| Phase | Scope | Status |
|---|---|---|
| 1 | Docker Compose (Postgres, Redis, API) + schema + Alembic | Done, verified |
| 2 | Booking endpoint, row locks, idempotency, JWT, 200-parallel test | Done, verified |
| 3 | Payment state machine, fake gateway, circuit breaker | **Done, verified** |
| 4 | Celery seat-hold expiry + retries | **Done, verified** |
| 5 | Redis token-bucket rate limiter (Lua) | **Done, verified** |
| 6 | Minimal React (Vite) UI | **Done, verified** |
| 7 | Locust load tests + invariant checker + README results | **Done, verified** |
| 8 | GitHub Actions CI + final README + architecture diagram | **Done; CI green on first GitHub run** |

Stop after each phase for user review.

## Verification (real runs, 2026-10-14)
Backend: **246 passed** (schema 11, booking service 8, API 13, booking concurrency 6,
state machine 30, resilience 21, payment service 17, payment API 10, payment concurrency 3,
expiry/re-drive 14, worker tasks 11, worker concurrency 5, rate limiter 30, rate limiter concurrency 3,
catalog/CORS 17, invariant checker + provisioning 38, no-deadlock/no-lazy-load 9).
Frontend: **67 passed** (`cd frontend && npm test`: format 11, api 15, session 5, Search 7, SeatMap 15, BookingPanel 14).
Production build (`npm run build`) succeeds (about 158 kB JS, 51 kB gzipped).
`alembic check`: no drift; migrations 0002 and 0003 downgrade/upgrade round trips work.

Phase 2 (unchanged): 200 parallel bookings for one seat -> exactly 1 success (service level and
over live HTTP: one 201, 199 x 409); 200 users over 20 seats -> each seat once; 50 parallel
retries with one Idempotency-Key -> one booking.

Phase 3:
- 50 parallel pay requests for one booking (same key) -> exactly 1 gateway charge, payment SUCCESS,
  booking CONFIRMED; same with 50 *different* keys -> still 1 charge.
- Chaos test: 60 bookings x 3 duplicate simultaneous pay requests against a gateway with
  20% reject / 60% lost response (also on replays) / 5% decline. Checked against the gateway
  ledger: no payment charged twice; no FAILED payment charged; every SUCCESS charged exactly once
  with its booking CONFIRMED; PENDING payments really occurred and, after re-driving them with
  the same keys, none remain PENDING and every ledger entry maps to a SUCCESS payment.
  Chaos test repeated 10x: passed every time.
- All 12 possible status transitions checked against the DB trigger (4 legal pass, 8 illegal rejected).
- **Mutation checks** (break the code, tests must fail, then restore): M1 no booking lock when
  starting a payment; M2 unstable gateway key; M3 timeout recorded as FAILED (also caught by the
  chaos test's real invariant "a FAILED payment was actually charged"); M4 hold released with a
  payment in flight; M5 declines trip the circuit; M6 success does not confirm the booking.
  All six CAUGHT; suite green after restore.
- One-off live runs through the real API (random, so numbers differ each run, not for the README):
  20 payments with default gateway -> 20 SUCCESS, 31 gateway calls (9 rejected, 2 lost responses
  recovered by replay), 20 ledger charges, 0 double charges. Gateway rejecting everything:
  pay #1 4 calls/410 ms -> FAILED; #2 opened the circuit after 1 call; #3, #4 made 0 calls
  (24-39 ms); 5 calls total instead of 16; `/health` showed `payment_circuit: open`.
Phase 4:
- Sweeps vs live traffic: 40 seats with expired holds, 3 new bookers per seat + 4 sweepers all at
  once -> each seat ends with exactly one new booker and an EXPIRED old hold.
- 4 parallel sweepers over 100 expired holds (batches of 10) -> released exactly 100 in total, once each.
- A sweeper skips a row another transaction holds (finishes in <2 s with a 3 s lock_timeout) and
  releases it on the next run.
- A hold that expires WHILE its payment is in flight (3 s gateway call, 2 s hold) is never released
  by a sweeper that runs the whole time; the payment lands and the booking is CONFIRMED.
- 10 stale PENDING payments re-driven by 4 parallel workers -> exactly 10 gateway charges, 10 SUCCESS.
- **Mutation checks** (7, all CAUGHT, suite green after restore): sweep ignores in-flight payments;
  no SKIP LOCKED; releases unexpired holds; re-drive with wrong user; tasks ignore pause flag; tasks
  lose retry policy; re-drive does not claim first. (Caveat above for the re-check-only mutation.)
- **Live end-to-end with the real worker + beat containers** (one-off, timings vary):
  hold of 5 s, no API calls afterwards -> booking EXPIRED at t=14 s, worker log "released 1 expired
  hold(s)". Gateway that charges but loses every response, stale threshold 5 s: pay -> 202 PENDING,
  1 charge; two worker ticks stayed `still_pending`, 9 gateway calls (1 charge + 8 replays), still 1
  charge; then healthy gateway -> worker set payment SUCCESS + booking CONFIRMED within 10 s, ledger
  still exactly 1 entry, `gateway_ref` matched it, same key `pay-<id>-1`.
- Verified the new partial index CAN serve the sweep query (planner forced with seqscan off);
  NOT verified that the planner chooses it at scale.
Phase 5:
- 500 simultaneous requests on one bucket (capacity 10) -> exactly 10 allowed, 490 refused (stable over
  5 runs); 200 requests over 20 identities (capacity 5) -> exactly 5 each. A naive non-atomic limiter
  over-admitted in every round measured (35, then 200 of 200).
- Bucket maths with a controllable clock: burst then refuse, `Retry-After`, refill, fractional tokens
  accumulate, capped at capacity, cost > 1, clock going backwards mints nothing, idle buckets expire.
- HTTP layer: per-user and per-IP buckets are independent; anonymous flood does not block a logged-in
  user; per-IP ceiling stops many accounts on one address; forged/expired tokens fall back to the IP;
  `/health` never limited; `X-Forwarded-For` ignored unless trusted; refused requests never reach the
  endpoint (no booking created).
- Fail open: unreachable Redis lets requests through; the breaker makes 200 checks take <0.5 s; real
  enforcement resumes when Redis returns (probe through half-open).
- **Mutation checks** (10, all CAUGHT, suite green after restore): bucket grows past capacity; no refill;
  fails closed; bypass ignored; forged token picks its bucket; /health limited; IP ceiling skipped;
  X-Forwarded-For always trusted; no Retry-After; a non-atomic limiter (caught by the 500-request test).
- **Live through the real API** (one-off, counts vary slightly): 40 rapid anonymous requests -> 10 x 200
  + 30 x 429 (first 429 had `Retry-After: 1`); after 2.2 s a request passed; with an anonymous flood in
  progress a logged-in user on the same IP still got 200; 200 simultaneous requests -> 12 allowed (10
  burst + refill during the run), 188 refused; `/health` always 200.
- **Live fail-open with Redis actually stopped:** 40 rapid requests -> 40 x 200 (0.33 s), `POST /bookings`
  -> 201, limiter warning logged. `POST /pay` -> 500 (simulator needs Redis, see gotchas). After
  `docker compose start redis`: breaker `half_open` then limiting enforced again (11 x 200, 9 x 429 of
  20); the payment left PENDING by the outage was settled by the worker (SUCCESS, booking CONFIRMED).
- NOT verified: behaviour with several API workers sharing the limiter under real network latency;
  Redis Cluster / replication; a real proxy in front (XFF handling is only unit tested); limiter
  overhead per request (not measured, Phase 7 will).
Phase 6:
- Backend: train search (case-insensitive, `%`/`_` treated literally), upcoming schedules with live
  availability, "my bookings" with payment status, seat map consistent with booking rules, CORS preflight
  for the dev origin (custom headers allowed, evil origin refused) and CORS headers on error responses.
- **Mutation checks on the UI logic (11, all CAUGHT, suite green after restore):** booking key discarded
  after a network drop; booking key never discarded; seats clickable during a booking; payment key kept
  after FAILED; new payment key on every press; 202 shown as failed; overdue-but-pending shown as expired;
  402 thrown as an error; Idempotency-Key header not sent; expired token trusted; poll blip reported as error.
- **Driven in the real app (built-in browser, API + Vite dev server running):** register, login, login
  survives a full reload (localStorage), train -> date -> seat map (72 seats), hold seat S1-5 (live 4:59
  countdown), pay -> "Payment successful", seat shows "yours, booked". DB check agreed: booking CONFIRMED,
  payment SUCCESS (1 attempt), exactly 1 gateway ledger charge. Then with a gateway declining every card:
  hold S1-6, pay -> "Your card was declined." + "Try again"; gateway healthy again, Try again -> confirmed on
  attempt 2 with a NEW gateway key (`pay-<id>-2`); ledger had exactly 2 charges for 2 seats and the gateway
  saw 3 calls (1 decline, 2 charges). Restarting the API under an open seat map: inline "Connection problem"
  note appeared at ~8 s, seats stayed visible, no error banner, note cleared itself at ~11 s.
- NOT verified: visual appearance (screenshots failed because the pane was hidden; only an early
  narrow-viewport screenshot was seen), real Chrome/Firefox/Safari, mobile layout, keyboard-only use and
  screen readers (only aria labels are tested), the 429 message and the in-progress (202) screen in a real
  browser (unit-tested only), many concurrent users in the UI, and production hosting of the UI.
Phase 7 (all numbers from `loadtest/data/*.report.json`, 2026-10-14, i5-12500H laptop, Docker VM 16 CPUs /
7.6 GB, load generator + API + Postgres + Redis on the same machine; table in README "Results"):
- 11 runs (1x x3, 100x/1 worker x3, 100x/4 workers x3, 100x limiter-on x2): 209 invariant checks, 0 failed;
  1,448 double-click duplicates sent, none created a second booking or charge; 0 HTTP 5xx; users told
  "confirmed" always matched the DB; ledger money == SUCCESS payments every time.
- 100x/1 worker: 40/40 seats, 120-134 req/s, median 3.0-6.6 s, p95 11-12 s, API peak CPU 129-150% (saturated).
  100x/4 workers: 40/40, 261-262 req/s, median 13-14 ms, p95 1.5-2.1 s, API 531-569%, Postgres 66-82%.
  Limiter on: 6,582 and 6,669 requests refused (73% of responses), ~450 users gave up, invariants held.
- Incident story (observed once, run files deleted): FIRST 100x run on one worker, before the fixes: 4,924
  requests, 85% timed out (30 s client timeout), 30 req/s, **8 of 40 seats sold** (a probe run: 0), yet 19/19
  invariants. Postgres: 40 connections idle in transaction, last query = the seat-map SELECT, CPU ~0. After the
  endpoint-commit fix: 40/40 seats, 143 req/s. 4 workers then hit `too many clients` (472 x 500) until
  Postgres max_connections was raised and OperationalError mapped to 503.
- Mutation-style proofs: undoing the endpoint commit in seat map / train search / my bookings makes
  `test_no_deadlock` fail (9-41 s runs); the async get_db was shown NOT necessary.
- NOT verified: multiple machines, long soak, real network latency/TLS, Postgres or Redis tuning, a real
  browser in the loop under load; fairness (who gets seats) and acceptable latency are NOT proven by the
  invariants. The per-process circuit breaker means each of 4 workers trips on its own.
- NOT verified: multiple API workers/containers (breaker state is per process, tests use one
  process); gateway behaviour with real latency (`GATEWAY_LATENCY_MS` was 0); circuit
  half-open recovery over HTTP (covered by unit tests with a fake clock only).

## CI (Phase 8): `.github/workflows/ci.yml`
Jobs: `backend` (compose up --wait, pytest inside the api container, alembic check), `frontend` (Node 20, npm ci/test/build),
`load-smoke` (`bash loadtest/run_scenario.sh ci-smoke 20 10 120 off 1 10 1`, uploads reports). Backend tests run in the compose
stack because `test_concurrency.py` needs a live API (it skips itself otherwise). 1,000-user runs stay out of CI on purpose.
Verified locally only: actionlint clean; from an emptied pgdata volume 246 passed + `alembic check` clean; smoke 19/19 in ~95 s;
frontend clean `npm ci` + 67 tests + build (local Node 24, CI pins 20). Pushed to https://github.com/nag1baba2/Tatkal-Rush (origin/main). First Actions run (commit a4f0371) passed backend, frontend, load-smoke
(user's screenshot of the summary; logs and 3 warnings/3 notices not read). NOT verified: GitHub's Mermaid rendering
(diagram syntax parsed with mermaid 11.4.1 in Node).
Local runs of `docker compose down -v` delete the seed data: re-run `docker compose exec api python -m app.seed`.

## How to run (Windows 11 + Docker Desktop)
Docker Desktop must show "Engine running" first.

```bash
docker compose up -d --build                      # Postgres, Redis, API (runs migrations)
curl http://localhost:8000/health                 # expect status ok
docker compose exec api python -m app.seed        # demo data (idempotent)
docker compose exec api python -m pytest -q -p no:warnings                       # all tests
docker compose exec api python -m pytest -v -p no:warnings tests/test_worker_concurrency.py
docker compose exec api python -m pytest -v -p no:warnings tests/test_ratelimit_concurrency.py
docker compose logs -f worker                     # watch the sweeps (every 10 s)
loadtest/run_scenario.sh 100x 1000 200 180 off 1  # load test + invariant check (Git Bash); see loadtest/run_scenario.sh
docker compose exec api python -m tools.summarize_runs   # results tables from loadtest/data/*.report.json
docker compose restart worker beat                # after changing worker/service code
docker compose exec api alembic check             # models vs migration drift
docker compose down                               # stop (add -v to wipe the database)
```

API docs: http://localhost:8000/docs  (use "Authorize" with a token from POST /auth/login).

UI (needs Node; the API stack must be running):
```bash
cd frontend
npm install                 # first time only
npm run dev                 # http://127.0.0.1:5173  (stop with Ctrl+C)
npm test                    # 67 unit/component tests
npm run build               # production build into frontend/dist
```
Set `VITE_API_URL` if the API is not on http://localhost:8000. For a deterministic payment demo start
the API with e.g. `GATEWAY_FAIL_RATE=0 GATEWAY_LOST_RESPONSE_RATE=0 GATEWAY_DECLINE_RATE=0 docker compose up -d`
(and plain `docker compose up -d` afterwards to restore the random defaults).

Endpoints: `POST /auth/register`, `POST /auth/login`, `GET /trains`, `GET /trains/{id}/schedules`,
`GET /schedules/{id}/seats`, `POST /bookings` (header `Idempotency-Key`), `GET /bookings` (mine),
`GET /bookings/{id}`,
`POST /bookings/{id}/pay` (header `Idempotency-Key`), `GET /bookings/{id}/payment`, `GET /health`.

## Environment notes
- Docker Desktop on this machine needed WSL2 + the Virtual Machine Platform feature
  repaired before it would start (host setup issue, not a project issue).
- Docker data lives on C: by default; consider moving the disk image to D: in
  Docker Desktop -> Settings -> Resources if C: space gets tight.
- Rate limiter ops switches (from `app.ratelimit`): `set_bypass(600)` / `clear_bypass()`, e.g.
  `docker compose exec api python -c "from app.ratelimit import set_bypass; set_bypass(600)"`.
  The test suite sets and clears the bypass itself.
- The worker and beat run as separate containers. Pause the sweeps by hand with
  `pause_sweeps(600)` from `app.worker.control` (and `resume_sweeps()`), via
  `docker compose exec api python -c ...`.
- Concurrency tests commit real rows to the dev DB (and gateway keys to Redis under a
  `gwtest:` prefix) and delete them in `finally`.
- Mutation-check script lives outside the repo (session scratchpad); re-create ad hoc if needed.

## Rules the user set
- Never invent performance numbers; every README number comes from a real run with
  the command to reproduce it.
- Be upfront about what cannot be verified here (user's browser, deployment,
  GitHub Actions runs).
- On failures: get the exact error, find the root cause with evidence, then fix.
- Give complete file contents when creating/changing files.
- Explain new concepts (Redis, Celery, Locust, row locks, idempotency, circuit
  breakers) in 2-3 plain lines with an analogy the first time they appear.
