"""Payment state machine.

    CREATED -> PENDING -> SUCCESS            (terminal)
                       -> FAILED -> PENDING  (user retries while the hold lasts)

The same rules are enforced inside Postgres by the payments_status_guard
trigger (migration 0002), so a bug here cannot corrupt the data.
"""

ALLOWED_TRANSITIONS = {
    ("CREATED", "PENDING"),
    ("PENDING", "SUCCESS"),
    ("PENDING", "FAILED"),
    ("FAILED", "PENDING"),
}


class IllegalTransition(Exception):
    pass


def transition(payment, new_status: str) -> None:
    if (payment.status, new_status) not in ALLOWED_TRANSITIONS:
        raise IllegalTransition(f"{payment.status} -> {new_status}")
    payment.status = new_status
