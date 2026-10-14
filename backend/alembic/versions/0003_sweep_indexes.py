"""partial indexes for the background sweeps

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only HELD rows are indexed, so the index stays tiny however many
    # bookings accumulate.
    op.create_index(
        "ix_bookings_held_expiry",
        "bookings",
        ["hold_expires_at"],
        postgresql_where=sa.text("status = 'HELD'"),
    )
    op.create_index(
        "ix_payments_pending_updated",
        "payments",
        ["updated_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_payments_pending_updated", table_name="payments")
    op.drop_index("ix_bookings_held_expiry", table_name="bookings")
