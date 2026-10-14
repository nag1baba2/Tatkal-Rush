"""payment state machine enforced in the database

Adds payments.last_error and a trigger that only allows the legal payment
status transitions, so even buggy application code cannot move a payment
backwards (for example SUCCESS -> FAILED).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION payments_status_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'CREATED' THEN
            RAISE EXCEPTION 'a payment must start as CREATED, not %', NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'CREATED' AND NEW.status = 'PENDING') OR
        (OLD.status = 'PENDING' AND NEW.status IN ('SUCCESS', 'FAILED')) OR
        (OLD.status = 'FAILED'  AND NEW.status = 'PENDING')
    ) THEN
        RAISE EXCEPTION 'illegal payment status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("payments", sa.Column("last_error", sa.String(100), nullable=True))
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER payments_status_guard "
        "BEFORE INSERT OR UPDATE OF status ON payments "
        "FOR EACH ROW EXECUTE FUNCTION payments_status_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS payments_status_guard ON payments")
    op.execute("DROP FUNCTION IF EXISTS payments_status_guard()")
    op.drop_column("payments", "last_error")
