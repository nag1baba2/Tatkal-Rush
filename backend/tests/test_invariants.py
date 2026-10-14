"""The invariant checker and the load-test provisioning.

evaluate() is pure, so these tests hand it deliberately corrupted snapshots and
require that the intended check fails each time (some corruptions legitimately trip
several checks). A checker that cannot fail
would make the load test's "all invariants hold" meaningless.
"""

import copy
import csv
import json

import pytest
import redis
from sqlalchemy import func, select

from app.auth import user_id_from_token
from app.circuit_breaker import CircuitBreaker
from app.config import settings
from app.db import SessionLocal
from app.gateway import GatewayTimeout
from app.models import Booking, Payment, Schedule, Seat, Train, User
from app.services.booking import create_booking
from app.services.payment import pay_booking
from tests.helpers import ScriptedGateway, committed_world, no_sleep
from tools.check_invariants import (
    Check,
    Snapshot,
    evaluate,
    load_snapshot,
    read_locust_csv,
    wait_for_settle,
)
from tools.provision import create_run, delete_run, reset_shared_state

FARE = 85000


def clean() -> Snapshot:
    """3 seats. Seat 1 sold, seat 2 held, seat 3 had a failed payment and then expired."""
    return Snapshot(
        seats_total=3,
        fare_paise=FARE,
        bookings=[
            {"id": 1, "user_id": 11, "seat_id": 101, "status": "CONFIRMED", "key": "a"},
            {"id": 2, "user_id": 12, "seat_id": 102, "status": "HELD", "key": "b"},
            {"id": 3, "user_id": 13, "seat_id": 103, "status": "EXPIRED", "key": "c"},
        ],
        payments=[
            {"id": 7, "booking_id": 1, "status": "SUCCESS", "amount_paise": FARE, "gateway_ref": "gw_1", "attempts": 2},
            {"id": 8, "booking_id": 3, "status": "FAILED", "amount_paise": FARE, "gateway_ref": None, "attempts": 1},
        ],
        ledger=[{"key": "pay-7-2", "ref": "gw_1", "amount": FARE}],
        users_told_confirmed=[11],
        global_active_seat_dupes=0,
    )


def failed_names(snapshot: Snapshot) -> list[str]:
    return [c.name for c in evaluate(snapshot) if not c.ok]


def test_a_correct_run_passes_every_check():
    checks = evaluate(clean())
    assert len(checks) >= 18
    assert all(c.ok for c in checks), [c.name for c in checks if not c.ok]


def test_the_users_told_check_is_skipped_when_there_is_no_client_data():
    snap = clean()
    snap.users_told_confirmed = None
    assert not any("was told" in c.name for c in evaluate(snap))


# (description, mutation of a clean snapshot, fragment of the ONE check that must fail)
def _double_booking(s):
    s.bookings.append({"id": 4, "user_id": 14, "seat_id": 101, "status": "HELD", "key": "d"})

def _too_many_confirmed(s):
    s.seats_total = 0

def _duplicate_rows(s):
    s.bookings.append({"id": 4, "user_id": 12, "seat_id": 102, "status": "EXPIRED", "key": "e"})

def _user_two_active(s):
    s.bookings.append({"id": 4, "user_id": 12, "seat_id": 104, "status": "HELD", "key": "f"})

def _bad_booking_status(s):
    s.bookings[1]["status"] = "WEIRD"

def _sold_without_payment(s):
    s.payments = [p for p in s.payments if p["booking_id"] != 1]
    s.ledger = []

def _sold_with_failed_payment(s):
    s.payments[0]["status"] = "FAILED"

def _two_payments_one_booking(s):
    s.payments.append({"id": 9, "booking_id": 1, "status": "FAILED", "amount_paise": FARE, "gateway_ref": None, "attempts": 1})

def _orphan_payment(s):
    s.bookings[0]["status"] = "HELD"

def _no_gateway_ref(s):
    s.payments[0]["gateway_ref"] = None

def _double_charge(s):
    s.ledger.append({"key": "pay-7-3", "ref": "gw_x", "amount": FARE})

def _same_key_twice(s):
    s.ledger.append({"key": "pay-7-2", "ref": "gw_1", "amount": FARE})

def _paid_but_not_charged(s):
    s.ledger = []

def _failed_but_charged(s):
    s.ledger.append({"key": "pay-8-1", "ref": "gw_y", "amount": FARE})

def _left_pending(s):
    s.payments[1]["status"] = "PENDING"

def _stray_charge(s):
    s.ledger.append({"key": "pay-999-1", "ref": "gw_z", "amount": FARE})

def _unparseable_charge(s):
    s.ledger.append({"key": "mystery", "ref": "gw_z", "amount": FARE})

def _wrong_amount(s):
    s.ledger[0]["amount"] = FARE - 1
    s.payments[0]["amount_paise"] = FARE - 1

def _money_mismatch(s):
    s.payments[0]["amount_paise"] = FARE + 500

def _lied_to_user(s):
    s.users_told_confirmed = [11, 12]  # user 12 only holds a HELD seat

def _global_dupe(s):
    s.global_active_seat_dupes = 2

VIOLATIONS = [
    ("a seat booked twice", _double_booking, "no seat has more than one active booking"),
    ("a double booking somewhere else in the database", _global_dupe, "anywhere in the database"),
    ("more confirmed than seats", _too_many_confirmed, "no more seats confirmed than exist"),
    ("a duplicate request that created a second row", _duplicate_rows, "duplicate requests collapsed"),
    ("a user holding two seats", _user_two_active, "no user holds more than 1 active"),
    ("an invalid status", _bad_booking_status, "all statuses are valid"),
    ("a seat sold with no payment", _sold_without_payment, "no seat sold unpaid"),
    ("a seat sold against a FAILED payment", _sold_with_failed_payment, "no seat sold unpaid"),
    ("two payment rows for one booking", _two_payments_one_booking, "at most one payment row"),
    ("a paid payment whose booking is not confirmed", _orphan_payment, "zero orphan payments"),
    ("a paid payment with no gateway reference", _no_gateway_ref, "records the gateway reference"),
    ("a payment charged twice", _double_charge, "zero double charges"),
    ("one gateway key charged twice", _same_key_twice, "key was charged twice"),
    ("a paid payment the gateway never charged", _paid_but_not_charged, "charged exactly once"),
    ("money taken for a FAILED payment", _failed_but_charged, "no FAILED payment was ever charged"),
    ("a payment left PENDING", _left_pending, "left unresolved"),
    ("a charge with no payment", _stray_charge, "no stray charges"),
    ("a charge whose key is not a payment key", _unparseable_charge, "no stray charges"),
    ("a charge for the wrong amount", _wrong_amount, "equals the fare"),
    ("charged amount different from paid amount", _money_mismatch, "money conserved"),
    ("a user told 'confirmed' who is not", _lied_to_user, "was told"),
]


@pytest.mark.parametrize("description,mutate,fragment", VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_each_kind_of_corruption_is_detected(description, mutate, fragment):
    snap = clean()
    mutate(snap)
    failed = failed_names(snap)
    assert failed, f"the checker did not notice: {description}"
    assert any(fragment in name for name in failed), f"expected a failure matching {fragment!r}, got {failed}"


def test_scenario_checks_are_labelled_so_they_are_not_mistaken_for_system_promises():
    snap = clean()
    _user_two_active(snap)
    broken = [c for c in evaluate(snap) if not c.ok]
    assert [c.scope for c in broken] == ["scenario"]


def test_a_charged_but_pending_payment_is_reported_with_that_detail():
    snap = clean()
    _left_pending(snap)
    snap.ledger.append({"key": "pay-8-1", "ref": "gw_q", "amount": FARE})
    pending_check = next(c for c in evaluate(snap) if "unresolved" in c.name)
    assert not pending_check.ok and "1 of them already charged" in pending_check.detail


def test_holding_several_seats_is_fine_when_the_user_tried_for_several():
    snap = clean()
    snap.rounds = 3
    for i in range(2):  # user 12 now holds 3 active bookings (HELD + 2 more)
        snap.bookings.append({"id": 20 + i, "user_id": 12, "seat_id": 200 + i, "status": "HELD", "key": f"x{i}"})
    assert all(c.ok for c in evaluate(snap))
    snap.bookings.append({"id": 30, "user_id": 12, "seat_id": 300, "status": "HELD", "key": "x9"})  # a fourth
    assert any("no user holds more than 3" in c.name for c in evaluate(snap) if not c.ok)


def test_being_told_of_more_confirmations_than_exist_is_detected():
    snap = clean()
    snap.users_told_confirmed = [11, 11]  # told twice, but only one confirmed booking
    assert any("was told" in c.name for c in evaluate(snap) if not c.ok)


# ---- reading the real database and ledger ----------------------------------------------------

def test_a_real_run_through_the_services_passes_and_a_tampered_ledger_does_not(make_gateway):
    gateway = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0)
    with committed_world(n_users=3, n_seats=3) as w:
        run = {"schedule_id": w.schedule_id, "seat_ids": w.seat_ids, "fare_paise": 50000}
        with SessionLocal() as s:
            for i in range(2):  # two seats paid, one held and never paid
                b, _ = create_booking(s, user_id=w.user_ids[i], schedule_id=w.schedule_id,
                                      seat_id=w.seat_ids[i], idempotency_key=f"b{i}")
                pay_booking(s, user_id=w.user_ids[i], booking_id=b.id, idempotency_key=f"p{i}",
                            gateway=gateway, breaker=CircuitBreaker(1000, 10), sleep=no_sleep)
            create_booking(s, user_id=w.user_ids[2], schedule_id=w.schedule_id,
                           seat_id=w.seat_ids[2], idempotency_key="b2")

        with SessionLocal() as s:
            snapshot = load_snapshot(s, run, gateway=gateway, users_told_confirmed=w.user_ids[:2])
        assert snapshot.seats_total == 3
        assert len(snapshot.ledger) == 2
        assert all(c.ok for c in evaluate(snapshot)), failed_names(snapshot)

        # Tamper: the gateway takes money for payment 1 a second time.
        paid = snapshot.payments[0]["id"]
        gateway.charge(f"pay-{paid}-9", 50000)
        with SessionLocal() as s:
            tampered = load_snapshot(s, run, gateway=gateway)
        assert any("zero double charges" in n for n in failed_names(tampered))


def test_load_snapshot_ignores_other_schedules(make_gateway):
    gateway = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0)
    with committed_world(n_users=1, n_seats=1) as a, committed_world(n_users=1, n_seats=1) as b:
        with SessionLocal() as s:
            create_booking(s, user_id=a.user_ids[0], schedule_id=a.schedule_id, seat_id=a.seat_ids[0], idempotency_key="k")
            create_booking(s, user_id=b.user_ids[0], schedule_id=b.schedule_id, seat_id=b.seat_ids[0], idempotency_key="k")
        with SessionLocal() as s:
            snap = load_snapshot(s, {"schedule_id": a.schedule_id, "seat_ids": a.seat_ids, "fare_paise": 50000}, gateway=gateway)
        assert [x["user_id"] for x in snap.bookings] == [a.user_ids[0]]


def test_wait_for_settle_returns_at_once_when_nothing_is_pending():
    with committed_world(n_users=1, n_seats=1) as w:
        with SessionLocal() as s:
            waited = wait_for_settle(s, {"schedule_id": w.schedule_id}, timeout_s=5, log=lambda *_: None)
        assert waited < 2


def test_wait_for_settle_gives_up_after_the_timeout_if_a_payment_stays_pending():
    with committed_world(n_users=1, n_seats=1) as w:
        with SessionLocal() as s:
            b, _ = create_booking(s, user_id=w.user_ids[0], schedule_id=w.schedule_id,
                                  seat_id=w.seat_ids[0], idempotency_key="k")
            pay_booking(s, user_id=w.user_ids[0], booking_id=b.id, idempotency_key="p",
                        gateway=ScriptedGateway(default=GatewayTimeout()),
                        breaker=CircuitBreaker(1000, 10), sleep=no_sleep, attempts=2)
        with SessionLocal() as s:
            waited = wait_for_settle(s, {"schedule_id": w.schedule_id}, timeout_s=3, log=lambda *_: None)
            still = s.scalar(select(func.count()).select_from(Payment).where(Payment.status == "PENDING",
                             Payment.booking_id == b.id))
        assert waited >= 3 and still == 1


def test_locust_csv_is_parsed(tmp_path):
    header = ["Type", "Name", "Request Count", "Failure Count", "Median Response Time", "Average Response Time",
              "Min Response Time", "Max Response Time", "Average Content Size", "Requests/s", "Failures/s",
              "50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%", "99.99%", "100%"]
    row = ["POST", "POST /bookings", "100", "2", "40", "55", "5", "900", "300", "12.5", "0.2",
           "40", "50", "60", "70", "90", "120", "300", "450", "800", "900", "900"]
    agg = ["", "Aggregated", "100", "2", "40", "55", "5", "900", "300", "12.5", "0.2",
           "40", "50", "60", "70", "90", "120", "300", "450", "800", "900", "900"]
    path = tmp_path / "x_stats.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerows([header, row, agg])
    parsed = read_locust_csv(str(tmp_path / "x"))
    assert parsed["POST /bookings"] == {"requests": 100, "failures": 2, "median_ms": 40.0, "p95_ms": 120.0,
                                        "p99_ms": 450.0, "max_ms": 900.0, "rps": 12.5}
    assert "Aggregated" in parsed
    assert read_locust_csv(str(tmp_path / "missing")) is None


# ---- provisioning ----------------------------------------------------------------------------------

def test_provisioning_builds_an_isolated_world_and_cleans_up_exactly():
    run = create_run("unittest", users=5, seats=3)
    try:
        assert len(run["users"]) == 5 and len(run["seat_ids"]) == 3
        assert run["tag"].startswith("unittest-")
        # every ready-made token verifies and identifies the right user
        assert [user_id_from_token(u["token"]) for u in run["users"]] == [u["id"] for u in run["users"]]
        assert run["environment"]["hold_seconds"] == settings.hold_seconds
        with SessionLocal() as s:
            assert s.scalar(select(func.count()).select_from(Seat).where(Seat.schedule_id == run["schedule_id"])) == 3
            assert s.get(Schedule, run["schedule_id"]).fare_paise == run["fare_paise"]
    finally:
        counts = delete_run(run)
    assert counts["users"] == 5 and counts["seats"] == 3 and counts["schedules"] == 1 and counts["trains"] == 1
    with SessionLocal() as s:
        assert s.get(Schedule, run["schedule_id"]) is None
        assert s.get(Train, run["train_id"]) is None
        assert s.scalar(select(func.count()).select_from(User).where(User.id.in_([u["id"] for u in run["users"]]))) == 0


def test_two_runs_do_not_collide():
    a, b = create_run("same", 2, 2), create_run("same", 2, 2)
    try:
        assert a["tag"] != b["tag"] and a["schedule_id"] != b["schedule_id"]
        assert not {u["id"] for u in a["users"]} & {u["id"] for u in b["users"]}
    finally:
        delete_run(a)
        delete_run(b)


def test_deleting_a_run_removes_its_bookings_and_payments_too(make_gateway):
    run = create_run("withdata", users=2, seats=2)
    try:
        with SessionLocal() as s:
            b, _ = create_booking(s, user_id=run["users"][0]["id"], schedule_id=run["schedule_id"],
                                  seat_id=run["seat_ids"][0], idempotency_key="k")
            pay_booking(s, user_id=run["users"][0]["id"], booking_id=b.id, idempotency_key="p",
                        gateway=ScriptedGateway("gw_1"), breaker=CircuitBreaker(1000, 10), sleep=no_sleep)
    finally:
        counts = delete_run(run)
    assert counts["bookings"] == 1 and counts["payments"] == 1


def test_resetting_shared_state_clears_the_ledger_and_rate_limit_buckets():
    client = redis.Redis.from_url(settings.redis_url)
    client.set("rl:bucket:id:test-key", "x")
    client.hset("gw:stats", "calls", 5)
    reset_shared_state()
    assert client.exists("rl:bucket:id:test-key") == 0
    assert client.exists("gw:stats") == 0


# ---- the command-line entry points (a duplicated argparse option once broke the real run) ------------

def test_the_command_line_tools_work_end_to_end(tmp_path, monkeypatch, capsys):
    import sys

    from tools import check_invariants, provision

    run_file = tmp_path / "run.json"
    monkeypatch.setattr(sys, "argv", ["provision", "--name", "cli", "--users", "3", "--seats", "4",
                                      "--rounds", "2", "--out", str(run_file)])
    provision.main()
    run = json.loads(run_file.read_text())
    try:
        assert (len(run["users"]), len(run["seat_ids"]), run["rounds"]) == (3, 4, 2)

        report = tmp_path / "report.json"
        monkeypatch.setattr(sys, "argv", ["check", "--run", str(run_file), "--report", str(report)])
        assert check_invariants.main() == 0          # nothing happened, so nothing is wrong
        out = capsys.readouterr().out
        assert "checks passed" in out and "PASS" in out
        assert json.loads(report.read_text())["passed"] is True
    finally:
        monkeypatch.setattr(sys, "argv", ["check", "--run", str(run_file), "--cleanup"])
        check_invariants.main()
    with SessionLocal() as s:
        assert s.get(Schedule, run["schedule_id"]) is None  # --cleanup removed the run


def test_the_checker_exits_non_zero_when_a_check_fails(tmp_path, monkeypatch, capsys, make_gateway):
    import sys

    from tools import check_invariants

    gateway = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0)
    monkeypatch.setattr(check_invariants, "default_gateway", lambda: gateway)
    run = create_run("failcli", users=1, seats=1)
    try:
        gateway.charge("pay-999999-1", run["fare_paise"])   # money taken for no payment
        run_file = tmp_path / "run.json"
        run_file.write_text(json.dumps(run))
        monkeypatch.setattr(sys, "argv", ["check", "--run", str(run_file)])
        assert check_invariants.main() == 1
        assert "FAIL  no stray charges" in capsys.readouterr().out
    finally:
        delete_run(run)
