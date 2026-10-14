"""HTTP behaviour of the payment endpoints (in-process, rolled back)."""

import pytest

from app.auth import create_access_token
from app.circuit_breaker import CircuitBreaker
from app.config import settings
from app.gateway import GatewayDeclined, GatewayTimeout
from app.main import app
from app.routers.payments import get_breaker, get_gateway
from tests.helpers import ScriptedGateway


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(settings, "retry_base_delay", 0.0)


@pytest.fixture
def use_gateway(client):
    """Install a gateway for the endpoints. `client` clears overrides afterwards."""

    def install(gateway):
        app.dependency_overrides[get_gateway] = lambda: gateway
        app.dependency_overrides[get_breaker] = lambda: CircuitBreaker(1000, 10)

    return install


def headers(world, user="u1", key=None):
    h = {"Authorization": f"Bearer {create_access_token(world[user].id)}"}
    if key:
        h["Idempotency-Key"] = key
    return h


def hold_seat(client, world, user="u1", seat="seat1", sched="s1", key="b1"):
    r = client.post(
        "/bookings",
        json={"schedule_id": world[sched].id, "seat_id": world[seat].id},
        headers=headers(world, user, key),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def pay(client, world, booking_id, key="p1", user="u1"):
    return client.post(f"/bookings/{booking_id}/pay", headers=headers(world, user, key))


def test_successful_payment_returns_200_and_confirms_the_booking(client, world, use_gateway):
    use_gateway(ScriptedGateway("gw_ok"))
    bid = hold_seat(client, world)

    r = pay(client, world, bid)
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["gateway_ref"], body["amount_paise"]) == ("SUCCESS", "gw_ok", 50000)

    assert client.get(f"/bookings/{bid}", headers=headers(world)).json()["status"] == "CONFIRMED"
    assert client.get(f"/bookings/{bid}/payment", headers=headers(world)).json()["id"] == body["id"]


def test_replaying_a_paid_request_returns_200_with_the_replay_header(client, world, use_gateway):
    use_gateway(ScriptedGateway("gw_ok"))  # a second gateway call would raise
    bid = hold_seat(client, world)
    first = pay(client, world, bid)
    again = pay(client, world, bid)

    assert again.status_code == 200
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json()["id"] == first.json()["id"]


def test_a_decline_returns_402_and_the_user_can_retry_with_a_new_key(client, world, use_gateway):
    use_gateway(ScriptedGateway(GatewayDeclined(), "gw_ok"))
    bid = hold_seat(client, world)

    failed = pay(client, world, bid, key="p1")
    assert failed.status_code == 402
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["last_error"] == "declined"

    retried = pay(client, world, bid, key="p2")
    assert retried.status_code == 200
    assert retried.json()["attempts"] == 2


def test_an_unresolved_payment_returns_202(client, world, use_gateway):
    use_gateway(ScriptedGateway(default=GatewayTimeout()))
    bid = hold_seat(client, world)

    r = pay(client, world, bid)
    assert r.status_code == 202
    assert r.json()["status"] == "PENDING"

    again = pay(client, world, bid)  # still in progress, no second gateway call
    assert again.status_code == 202
    assert again.headers["Idempotent-Replay"] == "true"


def test_paying_requires_a_token_and_an_idempotency_key(client, world, use_gateway):
    use_gateway(ScriptedGateway())
    bid = hold_seat(client, world)
    assert client.post(f"/bookings/{bid}/pay", headers={"Idempotency-Key": "k"}).status_code == 401
    no_key = client.post(f"/bookings/{bid}/pay", headers=headers(world))
    assert no_key.status_code == 422


def test_another_users_booking_is_404(client, world, use_gateway):
    use_gateway(ScriptedGateway())
    bid = hold_seat(client, world, user="u1")
    assert pay(client, world, bid, user="u2").status_code == 404
    assert client.get(f"/bookings/{bid}/payment", headers=headers(world, "u2")).status_code == 404


def test_payment_not_found_before_paying(client, world, use_gateway):
    use_gateway(ScriptedGateway())
    bid = hold_seat(client, world)
    assert client.get(f"/bookings/{bid}/payment", headers=headers(world)).status_code == 404


def test_an_expired_hold_returns_409(client, world, use_gateway, monkeypatch):
    use_gateway(ScriptedGateway())
    monkeypatch.setattr(settings, "hold_seconds", 0)
    bid = hold_seat(client, world)
    r = pay(client, world, bid)
    assert r.status_code == 409
    assert r.json()["detail"].startswith("booking_not_payable")


def test_a_key_already_used_by_another_booking_returns_409(client, world, use_gateway):
    use_gateway(ScriptedGateway("gw_1"))
    b1 = hold_seat(client, world, key="b1")
    b2 = hold_seat(client, world, seat="seat2", sched="s2", key="b2")
    assert pay(client, world, b1, key="same").status_code == 200
    r = pay(client, world, b2, key="same")
    assert r.status_code == 409
    assert r.json()["detail"] == "idempotency_key_already_used"


def test_health_reports_the_circuit_state(client):
    assert client.get("/health").json()["payment_circuit"] in {"closed", "open", "half_open"}
