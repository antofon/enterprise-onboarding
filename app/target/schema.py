"""Meridian's public data model, as pydantic. these models are the contract: the validation
engine checks every transformed record against them, and the target api rejects anything that
does not pass. field-level rules live here; cross-record rules are documented at the bottom and
enforced by the validation engine and the target api."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, model_validator

PLATFORM_NAME = "Meridian"


class LifecycleStatus(enum.StrEnum):
    prospect = "prospect"
    active = "active"
    inactive = "inactive"
    churned = "churned"


class Industry(enum.StrEnum):
    manufacturing = "manufacturing"
    construction = "construction"
    energy = "energy"
    logistics = "logistics"
    healthcare = "healthcare"
    technology = "technology"
    professional_services = "professional_services"
    other = "other"


class AccountPriority(enum.StrEnum):
    standard = "standard"
    priority = "priority"
    strategic = "strategic"


class ContactRole(enum.StrEnum):
    primary = "primary"
    billing = "billing"
    technical = "technical"
    executive = "executive"
    other = "other"


class Plan(enum.StrEnum):
    starter = "starter"
    professional = "professional"
    enterprise = "enterprise"


class BillingCycle(enum.StrEnum):
    monthly = "monthly"
    annual = "annual"


class SubscriptionStatus(enum.StrEnum):
    trial = "trial"
    active = "active"
    past_due = "past_due"
    cancelled = "cancelled"


class ActivityKind(enum.StrEnum):
    invoice = "invoice"
    payment = "payment"
    support_ticket = "support_ticket"
    login = "login"
    note = "note"


Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="stable id chosen by the integrator; writes with the same id are idempotent",
    ),
]
CountryCode = Annotated[
    str, Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")
]
E164Phone = Annotated[
    str, Field(pattern=r"^\+[1-9]\d{6,14}$", description="E.164, e.g. +14155552671")
]
Money = Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=2)]


class TargetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=False)


class Organization(TargetModel):
    """a customer account on the platform. everything else hangs off one of these."""

    organization_id: Identifier = Field(
        description="the customer's legacy account number usually lands here"
    )
    name: str = Field(min_length=1, max_length=200)
    lifecycle_status: LifecycleStatus
    industry: Industry = Industry.other
    account_priority: AccountPriority = Field(
        default=AccountPriority.standard,
        description="how the account is treated operationally; not the same thing as the plan",
    )
    billing_country: CountryCode
    billing_region: str | None = Field(
        default=None, max_length=10, description="state or province code, e.g. CA, TX, ON"
    )
    website: HttpUrl | None = None
    annual_revenue_usd: Money | None = None
    employee_count: int | None = Field(default=None, ge=0)
    customer_since: date | None = Field(
        default=None, description="when the relationship started, per the customer's records"
    )
    tags: list[str] = Field(default_factory=list, max_length=10)


class Contact(TargetModel):
    """a person at an organization."""

    contact_id: Identifier
    organization_id: Identifier = Field(description="must reference an existing organization")
    email: EmailStr = Field(description="unique within the organization")
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: E164Phone | None = None
    role: ContactRole = ContactRole.other
    is_primary: bool = Field(default=False, description="at most one per organization")


class Subscription(TargetModel):
    """what the organization pays for."""

    subscription_id: Identifier
    organization_id: Identifier = Field(description="must reference an existing organization")
    plan: Plan
    billing_cycle: BillingCycle
    status: SubscriptionStatus
    seats: int = Field(ge=1)
    start_date: date
    renewal_date: date = Field(description="must be after start_date")
    mrr_usd: Money = Field(description="monthly recurring revenue in USD")

    @model_validator(mode="after")
    def _renewal_after_start(self) -> Subscription:
        if self.renewal_date <= self.start_date:
            raise ValueError("renewal_date must be after start_date")
        return self


class Activity(TargetModel):
    """optional history: invoices, payments, tickets, logins, notes."""

    activity_id: Identifier
    organization_id: Identifier = Field(description="must reference an existing organization")
    kind: ActivityKind
    occurred_at: datetime
    amount_usd: Money | None = None
    description: str | None = Field(default=None, max_length=1000)


ENTITIES: dict[str, type[TargetModel]] = {
    "organization": Organization,
    "contact": Contact,
    "subscription": Subscription,
    "activity": Activity,
}

# cross-record rules. the validation engine checks these before a dry run and the target api
# enforces the ones it can see (existence, uniqueness, one primary contact).
CROSS_RECORD_RULES: tuple[str, ...] = (
    "an organization must exist before any contact, subscription or activity references it",
    "organization_id, contact_id, subscription_id and activity_id are unique per entity",
    "re-sending an id with identical content is a no-op; different content is a 409 conflict",
    "at most one contact per organization may have is_primary = true",
    "an organization with contacts should have exactly one primary contact (warning, not a reject)",
    "email is unique within an organization",
    "an inactive or churned organization cannot hold a trial or active subscription",
)
