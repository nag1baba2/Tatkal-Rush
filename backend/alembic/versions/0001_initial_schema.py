"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "trains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("destination", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trains")),
        sa.UniqueConstraint("number", name=op.f("uq_trains_number")),
    )

    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("train_id", sa.Integer(), nullable=False),
        sa.Column("travel_date", sa.Date(), nullable=False),
        sa.Column("departs_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fare_paise", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
        sa.ForeignKeyConstraint(
            ["train_id"], ["trains.id"], name=op.f("fk_schedules_train_id_trains")
        ),
        sa.UniqueConstraint(
            "train_id", "travel_date", name=op.f("uq_schedules_train_id")
        ),
        sa.CheckConstraint("fare_paise > 0", name=op.f("ck_schedules_fare_positive")),
    )

    op.create_table(
        "seats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("coach", sa.String(10), nullable=False),
        sa.Column("seat_no", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seats")),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["schedules.id"], name=op.f("fk_seats_schedule_id_schedules")
        ),
        sa.UniqueConstraint(
            "schedule_id", "coach", "seat_no", name=op.f("uq_seats_schedule_id")
        ),
        sa.UniqueConstraint("id", "schedule_id", name=op.f("uq_seats_id")),
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("seat_id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_bookings_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["seat_id", "schedule_id"],
            ["seats.id", "seats.schedule_id"],
            name=op.f("fk_bookings_seat_id_seats"),
        ),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name=op.f("uq_bookings_user_id")
        ),
        sa.CheckConstraint(
            "status IN ('HELD','CONFIRMED','EXPIRED','CANCELLED')",
            name=op.f("ck_bookings_status_valid"),
        ),
        sa.CheckConstraint(
            "status <> 'HELD' OR hold_expires_at IS NOT NULL",
            name=op.f("ck_bookings_held_has_expiry"),
        ),
    )
    # Partial unique index: at most one ACTIVE booking per seat.
    op.create_index(
        "uq_bookings_active_seat",
        "bookings",
        ["seat_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('HELD','CONFIRMED')"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("gateway_ref", sa.String(100), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["bookings.id"], name=op.f("fk_payments_booking_id_bookings")
        ),
        sa.UniqueConstraint("booking_id", name=op.f("uq_payments_booking_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payments_idempotency_key")),
        sa.CheckConstraint(
            "status IN ('CREATED','PENDING','SUCCESS','FAILED')",
            name=op.f("ck_payments_status_valid"),
        ),
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_payments_amount_positive")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_payments_attempts_nonnegative")),
    )


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_index("uq_bookings_active_seat", table_name="bookings")
    op.drop_table("bookings")
    op.drop_table("seats")
    op.drop_table("schedules")
    op.drop_table("trains")
    op.drop_table("users")
