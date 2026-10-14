"""Train search, schedules with availability, my bookings, seat-map consistency, CORS.

Runs in-process on the rolled-back test session. The dev database also holds the
seeded demo train, so searches here use made-up place names that match nothing else.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.auth import create_access_token
from app.gateway import GatewayTimeout
from app.models import Schedule, Seat, Train
from tests.helpers import ScriptedGateway, hold, pay

ORIGIN = "http://localhost:5173"


def bearer(world, user="u1"):
    return {"Authorization": f"Bearer {create_access_token(world[user].id)}"}


def add_train(db, number, source, destination, name="Test Train"):
    train = Train(number=number, name=name, source=source, destination=destination)
    db.add(train)
    db.flush()
    db.commit()
    return train


# ---- train search ----------------------------------------------------------------

def test_search_finds_trains_by_source_case_insensitively(client, db):
    add_train(db, "ZT1", "Zzorigin Junction", "Qqdest Central")
    found = client.get("/trains", params={"source": "zzORIGIN"}).json()
    assert [t["number"] for t in found] == ["ZT1"]
    assert found[0]["destination"] == "Qqdest Central"


def test_search_by_destination_and_by_both(client, db):
    add_train(db, "ZT1", "Zzorigin Junction", "Qqdest Central")
    add_train(db, "ZT2", "Zzorigin Junction", "Other Place")
    by_dest = client.get("/trains", params={"destination": "qqdest"}).json()
    both = client.get("/trains", params={"source": "zzorigin", "destination": "other"}).json()
    assert [t["number"] for t in by_dest] == ["ZT1"]
    assert [t["number"] for t in both] == ["ZT2"]


def test_search_with_no_match_is_an_empty_list(client):
    assert client.get("/trains", params={"source": "nowhere-at-all"}).json() == []


def test_blank_filters_are_ignored(client, db):
    add_train(db, "ZT1", "Zzorigin Junction", "Qqdest Central")
    assert len(client.get("/trains", params={"source": "   "}).json()) >= 1


def test_wildcard_characters_in_the_search_are_literal(client, db):
    add_train(db, "ZT1", "Zzorigin Junction", "Qqdest Central")
    assert client.get("/trains", params={"source": "%"}).json() == []
    assert client.get("/trains", params={"source": "_"}).json() == []


# ---- schedules and availability ------------------------------------------------------

def schedules(client, world):
    return client.get(f"/trains/{world['s1'].train_id}/schedules").json()


def test_unknown_train_is_404(client):
    assert client.get("/trains/0/schedules").status_code == 404


def test_schedules_are_ordered_and_show_free_seats(client, world):
    out = schedules(client, world)
    assert [s["travel_date"] for s in out] == ["2030-01-01", "2030-01-02"]
    assert all((s["seats_total"], s["seats_available"], s["fare_paise"]) == (1, 1, 50000) for s in out)


def test_past_schedules_are_not_listed(client, world, db):
    db.add(Schedule(train_id=world["s1"].train_id, travel_date=date(2020, 1, 1),
                    departs_at=datetime(2020, 1, 1, tzinfo=timezone.utc), fare_paise=100))
    db.commit()
    assert [s["travel_date"] for s in schedules(client, world)] == ["2030-01-01", "2030-01-02"]


def test_a_hold_uses_up_availability_and_an_expired_one_gives_it_back(client, world, db):
    booking = hold(db, world, seconds=60)
    assert schedules(client, world)[0]["seats_available"] == 0

    db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id=:i"),
               {"i": booking.id})
    db.commit()
    db.expire_all()
    assert schedules(client, world)[0]["seats_available"] == 1


# ---- the seat map agrees with the booking rules ----------------------------------------

def seat_status(client, world):
    return client.get(f"/schedules/{world['s1'].id}/seats").json()[0]["status"]


def test_an_expired_hold_whose_payment_is_in_flight_still_shows_as_held(client, world, db):
    """Booking refuses such a seat (money may be moving), so the map must not
    offer it as available."""
    booking = hold(db, world, seconds=60)
    pay(db, world, booking, ScriptedGateway(default=GatewayTimeout()))  # leaves it PENDING
    db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id=:i"),
               {"i": booking.id})
    db.commit()
    db.expire_all()

    assert seat_status(client, world) == "HELD"
    assert schedules(client, world)[0]["seats_available"] == 0


def test_an_expired_hold_with_no_payment_shows_as_available(client, world, db):
    booking = hold(db, world, seconds=60)
    db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id=:i"),
               {"i": booking.id})
    db.commit()
    db.expire_all()
    assert seat_status(client, world) == "AVAILABLE"


# ---- my bookings -------------------------------------------------------------------------

def test_my_bookings_needs_a_token(client):
    assert client.get("/bookings").status_code == 401


def test_my_bookings_lists_only_mine_newest_first_with_details(client, world, db):
    first = hold(db, world, user="u1", key="b1")
    second = hold(db, world, user="u1", seat="seat2", sched="s2", key="b2")
    # another user's booking, on a seat of its own, must never show up for u1
    extra = Seat(schedule_id=world["s1"].id, coach="S2", seat_no=1)
    db.add(extra)
    db.commit()
    world["extra"] = extra
    hold(db, world, user="u2", seat="extra", key="other")

    rows = client.get("/bookings", headers=bearer(world, "u1")).json()
    assert [r["id"] for r in rows] == [second.id, first.id]
    top = rows[0]
    assert (top["coach"], top["seat_no"], top["train_number"], top["fare_paise"]) == ("S1", 1, "T1", 50000)
    assert top["travel_date"] == "2030-01-02"
    assert top["payment_status"] is None

    theirs = client.get("/bookings", headers=bearer(world, "u2")).json()
    assert len(theirs) == 1 and theirs[0]["seat_id"] == world["extra"].id
    assert not {first.id, second.id} & {r["id"] for r in theirs}  # none of u1's


def test_my_bookings_shows_the_payment_status(client, world, db):
    booking = hold(db, world)
    pay(db, world, booking, ScriptedGateway("gw_ok"))
    row = client.get("/bookings", headers=bearer(world)).json()[0]
    assert (row["status"], row["payment_status"]) == ("CONFIRMED", "SUCCESS")


# ---- CORS ------------------------------------------------------------------------------------

def preflight(client, origin):
    return client.options(
        "/bookings",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )


def test_preflight_from_the_dev_server_allows_the_custom_headers(client):
    r = preflight(client, ORIGIN)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed and "authorization" in allowed


def test_preflight_from_another_origin_is_refused(client):
    r = preflight(client, "http://evil.example")
    assert "access-control-allow-origin" not in r.headers
    assert r.status_code == 400


def test_error_responses_also_carry_cors_headers_and_expose_the_useful_ones(client, world, db):
    hold(db, world, user="u1", key="b1")  # take the seat
    r = client.post(
        "/bookings",
        json={"schedule_id": world["s1"].id, "seat_id": world["seat1"].id},
        headers={**bearer(world, "u2"), "Idempotency-Key": "k2", "Origin": ORIGIN},
    )
    assert r.status_code == 409
    assert r.headers["access-control-allow-origin"] == ORIGIN
    exposed = r.headers["access-control-expose-headers"].lower()
    assert "retry-after" in exposed and "idempotent-replay" in exposed
