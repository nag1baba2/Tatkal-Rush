"""HTTP-level behaviour: auth and booking endpoints (in-process, rolled back)."""

from app.auth import create_access_token


def auth(world, user="u1", **kw):
    return {"Authorization": f"Bearer {create_access_token(world[user].id, **kw)}"}


def booking_request(client, world, *, key="k1", user="u1", seat="seat1", sched="s1"):
    return client.post(
        "/bookings",
        json={"schedule_id": world[sched].id, "seat_id": world[seat].id},
        headers={**auth(world, user), "Idempotency-Key": key},
    )


# ---- auth ---------------------------------------------------------------

def test_register_then_login_returns_a_token(client):
    r = client.post("/auth/register", json={"email": "New@Example.com", "password": "password123"})
    assert r.status_code == 201
    assert r.json()["email"] == "new@example.com"  # normalised
    assert "password" not in r.text

    r = client.post("/auth/login", json={"email": "new@example.com", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"
    assert r.json()["access_token"]


def test_duplicate_registration_is_409(client):
    body = {"email": "dup@example.com", "password": "password123"}
    assert client.post("/auth/register", json=body).status_code == 201
    assert client.post("/auth/register", json=body).status_code == 409


def test_wrong_password_and_unknown_email_look_the_same(client):
    client.post("/auth/register", json={"email": "x@example.com", "password": "password123"})
    wrong = client.post("/auth/login", json={"email": "x@example.com", "password": "nope-nope-nope"})
    unknown = client.post("/auth/login", json={"email": "who@example.com", "password": "password123"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_weak_password_and_bad_email_are_rejected(client):
    assert client.post("/auth/register", json={"email": "a@example.com", "password": "short"}).status_code == 422
    assert client.post("/auth/register", json={"email": "not-an-email", "password": "password123"}).status_code == 422


def test_booking_requires_a_valid_token(client, world):
    body = {"schedule_id": world["s1"].id, "seat_id": world["seat1"].id}
    headers = {"Idempotency-Key": "k"}
    assert client.post("/bookings", json=body, headers=headers).status_code == 401
    bad = {**headers, "Authorization": "Bearer not.a.token"}
    assert client.post("/bookings", json=body, headers=bad).status_code == 401
    expired = {**headers, **auth(world, expires_minutes=-1)}
    assert client.post("/bookings", json=body, headers=expired).status_code == 401


# ---- booking ------------------------------------------------------------

def test_booking_then_retry_returns_the_same_booking(client, world):
    first = booking_request(client, world, key="abc")
    assert first.status_code == 201
    assert first.json()["status"] == "HELD"

    retry = booking_request(client, world, key="abc")
    assert retry.status_code == 200
    assert retry.headers["Idempotent-Replay"] == "true"
    assert retry.json()["id"] == first.json()["id"]


def test_second_user_gets_409_for_a_taken_seat(client, world):
    assert booking_request(client, world, user="u1", key="a").status_code == 201
    r = booking_request(client, world, user="u2", key="b")
    assert r.status_code == 409
    assert r.json()["detail"] == "seat_unavailable"


def test_missing_idempotency_key_is_422(client, world):
    r = client.post(
        "/bookings",
        json={"schedule_id": world["s1"].id, "seat_id": world["seat1"].id},
        headers=auth(world),
    )
    assert r.status_code == 422


def test_same_key_for_a_different_seat_is_422(client, world):
    assert booking_request(client, world, key="k").status_code == 201
    r = booking_request(client, world, key="k", seat="seat2", sched="s2")
    assert r.status_code == 422
    assert r.json()["detail"] == "idempotency_key_reused_with_different_request"


def test_unknown_seat_is_404(client, world):
    r = client.post(
        "/bookings",
        json={"schedule_id": world["s1"].id, "seat_id": 0},
        headers={**auth(world), "Idempotency-Key": "k"},
    )
    assert r.status_code == 404


def test_only_the_owner_can_read_a_booking(client, world):
    booking_id = booking_request(client, world, user="u1").json()["id"]
    assert client.get(f"/bookings/{booking_id}", headers=auth(world, "u1")).status_code == 200
    assert client.get(f"/bookings/{booking_id}", headers=auth(world, "u2")).status_code == 404


def test_seat_map_reflects_holds(client, world):
    sid = world["s1"].id
    assert client.get(f"/schedules/{sid}/seats").json()[0]["status"] == "AVAILABLE"
    booking_request(client, world)
    assert client.get(f"/schedules/{sid}/seats").json()[0]["status"] == "HELD"
    assert client.get("/schedules/0/seats").status_code == 404


def test_a_refused_database_connection_is_a_retryable_503_not_a_500(client, world, monkeypatch):
    """Postgres answering 'too many clients' is transient overload. The client must be told to
    retry (the idempotency key makes that safe), not given a generic 500."""
    from sqlalchemy.exc import OperationalError

    import app.routers.schedules as schedules

    def refuse(*_args, **_kwargs):
        raise OperationalError("connect", {}, Exception("FATAL: sorry, too many clients already"))

    monkeypatch.setattr(schedules, "seat_states", refuse)
    response = client.get(f"/schedules/{world['s1'].id}/seats")
    assert response.status_code == 503
    assert response.json() == {"detail": "server_busy"}
    assert response.headers["Retry-After"] == "1"
