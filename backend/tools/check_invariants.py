"""Prove that a load run left the system correct.

The load tool only says "no errors". This reads the database and the payment
gateway's own ledger (its "bank statement") afterwards and checks the promises the
system makes, plus that what each simulated user was TOLD matches what is stored.

  python -m tools.check_invariants --run /loadtest-data/run.json --name 100x \\
         --locust-prefix /loadtest-data/100x --wait-settle 60

Exit code 0 only if every check passes.

Structure: load_snapshot() reads the data, evaluate() is a pure function from a
Snapshot to a list of Check results. The tests feed evaluate() deliberately
corrupted snapshots to prove each check really detects its violation.
"""

import argparse
import csv
import json
import pathlib
import re
import time
from collections import Counter
from dataclasses import dataclass

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import ACTIVE_BOOKING_STATUSES, Booking, Payment
from app.services.payment import default_gateway

KEY_RE = re.compile(r"^pay-(\d+)-(\d+)$")
BOOKING_STATUSES = {"HELD", "CONFIRMED", "EXPIRED", "CANCELLED"}
PAYMENT_STATUSES = {"CREATED", "PENDING", "SUCCESS", "FAILED"}


@dataclass
class Snapshot:
    seats_total: int
    fare_paise: int
    bookings: list[dict]   # id, user_id, seat_id, status, key
    payments: list[dict]   # id, booking_id, status, amount_paise, gateway_ref, attempts
    ledger: list[dict]     # key, ref, amount   (one entry per REAL charge)
    users_told_confirmed: list[int] | None = None   # from the load tool, if available
    global_active_seat_dupes: int = 0               # across the whole database
    rounds: int = 1                                 # bookings each simulated user makes
    rounds: int = 1                                 # bookings each simulated user makes


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    scope: str = "system"   # "system" = a promise of the system; "scenario" = a property of the test


# ---------------------------------------------------------------------------
# evaluation (pure)
# ---------------------------------------------------------------------------

def _ledger_by_payment(ledger: list[dict]) -> tuple[Counter, list[dict]]:
    """Charges per payment id, and ledger entries whose key is not a payment key."""
    per_payment: Counter = Counter()
    unparseable = []
    for entry in ledger:
        match = KEY_RE.match(entry["key"])
        if match:
            per_payment[int(match.group(1))] += 1
        else:
            unparseable.append(entry)
    return per_payment, unparseable


def evaluate(s: Snapshot) -> list[Check]:
    checks: list[Check] = []

    def add(name, violations, detail_bad="", scope="system", detail_ok=""):
        """A check passes when there are no violations (empty list/dict, 0 or False)."""
        failed = bool(violations)
        checks.append(Check(name, not failed, detail_bad if failed else detail_ok, scope))

    bookings = {b["id"]: b for b in s.bookings}
    payments = s.payments
    payment_by_booking: dict[int, list[dict]] = {}
    for p in payments:
        payment_by_booking.setdefault(p["booking_id"], []).append(p)
    charges, unparseable = _ledger_by_payment(s.ledger)
    payment_ids = {p["id"] for p in payments}

    # ---- seats and bookings -------------------------------------------------------
    active_per_seat = Counter(b["seat_id"] for b in s.bookings if b["status"] in ACTIVE_BOOKING_STATUSES)
    double_booked = {seat: n for seat, n in active_per_seat.items() if n > 1}
    add("no seat has more than one active booking", double_booked,
        f"{len(double_booked)} seat(s) double-booked: {dict(list(double_booked.items())[:5])}")

    add("no seat is double-booked anywhere in the database", s.global_active_seat_dupes,
        f"{s.global_active_seat_dupes} seat(s) with >1 active booking across the whole DB")

    confirmed = [b for b in s.bookings if b["status"] == "CONFIRMED"]
    add("no more seats confirmed than exist", len(confirmed) > s.seats_total,
        f"{len(confirmed)} confirmed for {s.seats_total} seats",
        detail_ok=f"{len(confirmed)} confirmed of {s.seats_total} seats")

    pair_counts = Counter((b["user_id"], b["seat_id"]) for b in s.bookings)
    add("duplicate requests collapsed: no user has two booking rows for one seat",
        {k: v for k, v in pair_counts.items() if v > 1},
        "a retried or double-clicked booking created extra rows")

    user_active = Counter(b["user_id"] for b in s.bookings if b["status"] in ACTIVE_BOOKING_STATUSES)
    add(f"no user holds more than {s.rounds} active booking(s) (what each simulated user is trying to buy)",
        {u: n for u, n in user_active.items() if n > s.rounds},
        "a user ended up with more active bookings than they attempted", scope="scenario")

    bad_status = [b["id"] for b in s.bookings if b["status"] not in BOOKING_STATUSES]
    bad_status += [p["id"] for p in payments if p["status"] not in PAYMENT_STATUSES]
    add("all statuses are valid", bad_status, f"invalid status on {bad_status[:5]}")

    # ---- bookings vs payments ---------------------------------------------------------
    sold_without_payment = []
    for b in confirmed:
        ps = payment_by_booking.get(b["id"], [])
        if len(ps) != 1 or ps[0]["status"] != "SUCCESS":
            sold_without_payment.append(b["id"])
    add("every CONFIRMED booking has exactly one SUCCESS payment (no seat sold unpaid)",
        sold_without_payment, f"{len(sold_without_payment)} confirmed booking(s) without a paid payment: {sold_without_payment[:5]}")

    orphans = [p["id"] for p in payments
               if p["status"] == "SUCCESS"
               and (p["booking_id"] not in bookings or bookings[p["booking_id"]]["status"] != "CONFIRMED")]
    add("every SUCCESS payment belongs to a CONFIRMED booking (zero orphan payments)",
        orphans, f"{len(orphans)} paid payment(s) whose booking is not confirmed: {orphans[:5]}")

    missing_ref = [p["id"] for p in payments if p["status"] == "SUCCESS" and not p["gateway_ref"]]
    add("every SUCCESS payment records the gateway reference", missing_ref, f"missing on {missing_ref[:5]}")

    multi_payment = [bid for bid, ps in payment_by_booking.items() if len(ps) > 1]
    add("at most one payment row per booking", multi_payment, f"bookings with several payments: {multi_payment[:5]}")

    # ---- the money: payments vs the gateway's ledger ---------------------------------------
    double_charged = {pid: n for pid, n in charges.items() if n > 1}
    add("no payment was charged more than once (zero double charges)", double_charged,
        f"{len(double_charged)} payment(s) charged several times: {dict(list(double_charged.items())[:5])}")

    keys = Counter(e["key"] for e in s.ledger)
    add("no gateway idempotency key was charged twice", {k: n for k, n in keys.items() if n > 1},
        "the gateway charged one key more than once")

    not_charged = [p["id"] for p in payments if p["status"] == "SUCCESS" and charges.get(p["id"], 0) != 1]
    add("every SUCCESS payment was charged exactly once", not_charged,
        f"{len(not_charged)} paid payment(s) without exactly one charge: {not_charged[:5]}")

    failed_but_charged = [p["id"] for p in payments if p["status"] == "FAILED" and charges.get(p["id"], 0) > 0]
    add("no FAILED payment was ever charged (no money taken for nothing)", failed_but_charged,
        f"{len(failed_but_charged)} failed payment(s) were charged: {failed_but_charged[:5]}")

    pending = [p for p in payments if p["status"] == "PENDING"]
    add("no payment is left unresolved (PENDING)", [p["id"] for p in pending],
        f"{len(pending)} payment(s) still PENDING ({sum(1 for p in pending if charges.get(p['id'], 0))} of them already charged)")

    stray = [pid for pid in charges if pid not in payment_ids] + [e["key"] for e in unparseable]
    add("no stray charges: every ledger entry belongs to a payment of this run", stray,
        f"{len(stray)} charge(s) with no matching payment: {stray[:5]}")

    ledger_total = sum(e["amount"] for e in s.ledger)
    paid_total = sum(p["amount_paise"] for p in payments if p["status"] == "SUCCESS")
    add("money conserved: amount charged == amount of SUCCESS payments", ledger_total != paid_total,
        f"gateway took {ledger_total} paise but SUCCESS payments total {paid_total}",
        detail_ok=f"{ledger_total} paise ({ledger_total / 100:.2f} rupees) both ways")

    wrong_amount = [e["key"] for e in s.ledger if e["amount"] != s.fare_paise]
    add("every charge equals the fare", wrong_amount, f"{len(wrong_amount)} charge(s) with the wrong amount")

    # ---- what users were told vs what is stored ------------------------------------------------
    if s.users_told_confirmed is not None:
        told = Counter(s.users_told_confirmed)
        have = Counter(b["user_id"] for b in confirmed)
        lied_to = [u for u, n in told.items() if have[u] < n]
        add("every 'confirmed' a user was told is a CONFIRMED booking in the database", lied_to,
            f"{len(lied_to)} user(s) were told of more confirmed seats than they hold: {lied_to[:5]}",
            detail_ok=f"{len(s.users_told_confirmed)} confirmations told to users, all present in the database")
    return checks


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_snapshot(db: Session, run: dict, gateway=None, users_told_confirmed=None) -> Snapshot:
    gateway = gateway or default_gateway()
    schedule_id = run["schedule_id"]
    bookings = [
        {"id": b.id, "user_id": b.user_id, "seat_id": b.seat_id, "status": b.status, "key": b.idempotency_key}
        for b in db.scalars(select(Booking).where(Booking.schedule_id == schedule_id)).all()
    ]
    booking_ids = [b["id"] for b in bookings]
    payments = [
        {"id": p.id, "booking_id": p.booking_id, "status": p.status, "amount_paise": p.amount_paise,
         "gateway_ref": p.gateway_ref, "attempts": p.attempts, "last_error": p.last_error}
        for p in db.scalars(select(Payment).where(Payment.booking_id.in_(booking_ids))).all()
    ] if booking_ids else []

    dupes = db.scalar(
        select(func.count()).select_from(
            select(Booking.seat_id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
            .group_by(Booking.seat_id)
            .having(func.count() > 1)
            .subquery()
        )
    )
    return Snapshot(
        seats_total=len(run["seat_ids"]),
        fare_paise=run["fare_paise"],
        bookings=bookings,
        payments=payments,
        ledger=gateway.ledger_entries(),
        users_told_confirmed=users_told_confirmed,
        global_active_seat_dupes=dupes or 0,
        rounds=run.get("rounds", 1),
    )


def wait_for_settle(db: Session, run: dict, timeout_s: float, log=print) -> float:
    """The background worker resolves payments left PENDING by timeouts. Wait for it."""
    start = time.monotonic()
    booking_ids = select(Booking.id).where(Booking.schedule_id == run["schedule_id"])
    last = None
    while True:
        db.rollback()  # fresh snapshot each poll
        pending = db.scalar(
            select(func.count()).select_from(Payment).where(
                Payment.status == "PENDING", Payment.booking_id.in_(booking_ids)
            )
        )
        if pending != last:
            log(f"  waiting for the worker: {pending} payment(s) still PENDING")
            last = pending
        if not pending or time.monotonic() - start > timeout_s:
            return time.monotonic() - start
        time.sleep(2)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def read_locust_csv(prefix: str) -> dict | None:
    path = pathlib.Path(f"{prefix}_stats.csv")
    if not path.exists():
        return None
    rows = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            rows[row["Name"]] = {
                "requests": int(row["Request Count"]),
                "failures": int(row["Failure Count"]),
                "median_ms": float(row["50%"]) if row["50%"] not in ("", "N/A") else None,
                "p95_ms": float(row["95%"]) if row["95%"] not in ("", "N/A") else None,
                "p99_ms": float(row["99%"]) if row["99%"] not in ("", "N/A") else None,
                "max_ms": float(row["Max Response Time"]),
                "rps": float(row["Requests/s"]),
            }
    return rows


def summarize(db: Session, run: dict, snapshot: Snapshot) -> dict:
    booking_status = Counter(b["status"] for b in snapshot.bookings)
    payment_status = Counter(p["status"] for p in snapshot.payments)
    limiter = {k.decode(): int(v) for k, v in redis.Redis.from_url(settings.redis_url).hgetall("rl:stats").items()}
    return {
        "users": len(run["users"]),
        "seats": snapshot.seats_total,
        "bookings_by_status": dict(booking_status),
        "payments_by_status": dict(payment_status),
        "failed_payment_reasons": dict(Counter(p.get("last_error") or "none" for p in snapshot.payments if p["status"] == "FAILED")),
        "payment_attempts": dict(sorted(Counter(p["attempts"] for p in snapshot.payments).items())),
        "real_charges_on_gateway_ledger": len(snapshot.ledger),
        "rupees_charged": sum(e["amount"] for e in snapshot.ledger) / 100,
        "seats_sold": booking_status.get("CONFIRMED", 0),
        "gateway_stats": default_gateway().stats(),
        "rate_limiter_stats": limiter,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="/loadtest-data/run.json")
    parser.add_argument("--name", default=None)
    parser.add_argument("--locust-prefix", default=None, help="path prefix of Locust's --csv output and *_outcomes.json")
    parser.add_argument("--wait-settle", type=float, default=0, help="seconds to wait for PENDING payments to resolve")
    parser.add_argument("--report", default=None, help="write the full report as JSON here")
    parser.add_argument("--cleanup", action="store_true", help="delete the run's data afterwards")
    args = parser.parse_args()

    run = json.loads(pathlib.Path(args.run).read_text())
    name = args.name or run["name"]
    outcomes = None
    if args.locust_prefix and pathlib.Path(f"{args.locust_prefix}_outcomes.json").exists():
        outcomes = json.loads(pathlib.Path(f"{args.locust_prefix}_outcomes.json").read_text())

    print(f"Invariant check for '{name}' ({run['tag']}): {len(run['users'])} users, {len(run['seat_ids'])} seats")
    with SessionLocal() as db:
        if args.wait_settle:
            waited = wait_for_settle(db, run, args.wait_settle)
            print(f"  settled after {waited:.0f}s")
        snapshot = load_snapshot(
            db, run, users_told_confirmed=(outcomes or {}).get("confirmed_user_ids")
        )
        checks = evaluate(snapshot)
        summary = summarize(db, run, snapshot)

    for c in checks:
        mark = "PASS" if c.ok else "FAIL"
        extra = f"  [{c.scope}]" if c.scope == "scenario" else ""
        print(f"  {mark}  {c.name}{extra}" + (f"  -> {c.detail}" if c.detail else ""))
    failed = [c for c in checks if not c.ok]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")

    print("\nState after the run")
    print(f"  bookings: {summary['bookings_by_status']}")
    print(f"  payments: {summary['payments_by_status']}")
    print(f"  why payments FAILED: {summary['failed_payment_reasons']}   attempts per payment: {summary['payment_attempts']}")
    print(f"  seats sold: {summary['seats_sold']} of {summary['seats']}   real charges on the gateway: "
          f"{summary['real_charges_on_gateway_ledger']} (Rs {summary['rupees_charged']:.2f})")
    print(f"  gateway: {summary['gateway_stats']}")
    print(f"  rate limiter: {summary['rate_limiter_stats']}")

    locust = read_locust_csv(args.locust_prefix) if args.locust_prefix else None
    if outcomes:
        print(f"\nWhat the simulated users experienced ({outcomes['users']} users)")
        print(f"  outcomes: {outcomes['outcomes']}")
        if outcomes.get("journeys") != outcomes.get("journeys_expected"):
            print(f"  WARNING: {outcomes.get('journeys')} journeys were recorded but {outcomes.get('journeys_expected')} "
                  f"were expected (users x rounds): the load generator misbehaved, treat the numbers with care")
        if outcomes.get("unfinished_users"):
            print(f"  WARNING: {outcomes['unfinished_users']} user(s) had not finished when the run ended")
        if outcomes.get("connection_errors"):
            print(f"  requests with no HTTP response, by cause: {outcomes['connection_errors']}")
        print(f"  retries (same idempotency key): {outcomes['retries']}")
        print(f"  double-click duplicates sent: {outcomes['duplicates_sent']}")
    if locust:
        agg = locust.get("Aggregated")
        print("\nLocust")
        for label, row in locust.items():
            if label == "Aggregated":
                continue
            print(f"  {label:34} n={row['requests']:<6} fail={row['failures']:<4} median={row['median_ms']} ms  "
                  f"p95={row['p95_ms']} ms  p99={row['p99_ms']} ms")
        if agg:
            print(f"  {'TOTAL':34} n={agg['requests']:<6} fail={agg['failures']:<4} "
                  f"{agg['rps']:.1f} req/s  median={agg['median_ms']} ms  p95={agg['p95_ms']} ms  p99={agg['p99_ms']} ms")

    if args.report:
        pathlib.Path(args.report).write_text(json.dumps({
            "name": name,
            "environment": run["environment"],
            "passed": not failed,
            "checks": [c.__dict__ for c in checks],
            "summary": summary,
            "outcomes": outcomes,
            "locust": locust,
        }, indent=2, default=str))
        print(f"\nreport written to {args.report}")

    if args.cleanup:
        from tools.provision import delete_run

        print("cleanup:", delete_run(run))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
