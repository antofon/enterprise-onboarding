"""Meridian's own tables: what the platform stores once a write is accepted.

These live in the `target` schema and are only ever reached through the target api in
`app/api/target.py`, never by the onboarding code directly. The migration layer talks http.

Every row belongs to a namespace. `LIVE_NAMESPACE` (the all-zero uuid) is the real platform
data; any other namespace is one dry run's staging area, created when the run starts and
purged when it is thrown away. Namespaces are isolated: a dry run that fails to create an
organization also fails its contacts, which is the whole point of rehearsing.

Identifiers are unique per namespace per entity, so re-sending the same record is a no-op and
a retry after a timeout cannot double-write.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import TARGET_SCHEMA, Base

LIVE_NAMESPACE = uuid.UUID(int=0)


class TargetRow:
    """columns every stored record carries, whatever the entity."""

    namespace: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=LIVE_NAMESPACE
    )
    # sha256 of the accepted payload: an identical re-send is a no-op, a different one is a 409
    content_hash: Mapped[str] = mapped_column(String(64))
    written_by_request_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def _org_fk(column: str) -> ForeignKeyConstraint:
    """children hang off an organization inside the same namespace."""
    return ForeignKeyConstraint(
        ["namespace", column],
        [
            f"{TARGET_SCHEMA}.organizations.namespace",
            f"{TARGET_SCHEMA}.organizations.organization_id",
        ],
        ondelete="CASCADE",
        name=f"fk_{column}_organization",
    )


class TargetOrganization(TargetRow, Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": TARGET_SCHEMA}

    organization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    lifecycle_status: Mapped[str] = mapped_column(String(32))
    industry: Mapped[str] = mapped_column(String(32))
    account_priority: Mapped[str] = mapped_column(String(32))
    billing_country: Mapped[str] = mapped_column(String(2))
    billing_region: Mapped[str | None] = mapped_column(String(10))
    website: Mapped[str | None] = mapped_column(String(500))
    annual_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    customer_since: Mapped[date | None] = mapped_column(Date)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)


class TargetContact(TargetRow, Base):
    __tablename__ = "contacts"
    __table_args__ = (_org_fk("organization_id"), {"schema": TARGET_SCHEMA})

    contact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(320))
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(32))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class TargetSubscription(TargetRow, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (_org_fk("organization_id"), {"schema": TARGET_SCHEMA})

    subscription_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    plan: Mapped[str] = mapped_column(String(32))
    billing_cycle: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    seats: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    renewal_date: Mapped[date] = mapped_column(Date)
    mrr_usd: Mapped[Decimal] = mapped_column(Numeric(15, 2))


class TargetActivity(TargetRow, Base):
    __tablename__ = "activities"
    __table_args__ = (_org_fk("organization_id"), {"schema": TARGET_SCHEMA})

    activity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    description: Mapped[str | None] = mapped_column(Text)


TARGET_TABLES: dict[str, type[Any]] = {
    "organization": TargetOrganization,
    "contact": TargetContact,
    "subscription": TargetSubscription,
    "activity": TargetActivity,
}

# the id column per entity, and the order a migration has to write them in
ID_COLUMN: dict[str, str] = {
    "organization": "organization_id",
    "contact": "contact_id",
    "subscription": "subscription_id",
    "activity": "activity_id",
}
WRITE_ORDER: tuple[str, ...] = ("organization", "contact", "subscription", "activity")
