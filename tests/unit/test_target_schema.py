import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.target.catalog import catalog_by_path, target_field_catalog
from app.target.schema import Contact, Organization, Subscription

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def test_organization_minimal_and_defaults() -> None:
    org = Organization(
        organization_id="49128",
        name="Apex Equipment Services",
        lifecycle_status="active",
        billing_country="US",
    )
    assert org.industry == "other"
    assert org.account_priority == "standard"
    assert org.tags == []


def test_identifier_and_country_patterns() -> None:
    with pytest.raises(ValidationError):
        Organization(organization_id="", name="x", lifecycle_status="active", billing_country="US")
    with pytest.raises(ValidationError):
        Organization(
            organization_id="a b", name="x", lifecycle_status="active", billing_country="US"
        )
    with pytest.raises(ValidationError):
        Organization(
            organization_id="ok", name="x", lifecycle_status="active", billing_country="USA"
        )


def test_unknown_enum_value_and_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        Organization(
            organization_id="1", name="x", lifecycle_status="On Hold", billing_country="US"
        )
    with pytest.raises(ValidationError):
        Organization(
            organization_id="1",
            name="x",
            lifecycle_status="active",
            billing_country="US",
            customer_tier="Gold",
        )


def test_contact_phone_must_be_e164() -> None:
    ok = Contact(
        contact_id="c1",
        organization_id="1",
        email="jane@example.com",
        first_name="Jane",
        last_name="Smith",
        phone="+14155552671",
    )
    assert ok.role == "other"
    with pytest.raises(ValidationError):
        Contact(
            contact_id="c1",
            organization_id="1",
            email="jane@example.com",
            first_name="Jane",
            last_name="Smith",
            phone="(415) 555-2671",
        )
    with pytest.raises(ValidationError):
        Contact(
            contact_id="c1",
            organization_id="1",
            email="not-an-email",
            first_name="Jane",
            last_name="Smith",
        )


def test_subscription_renewal_after_start() -> None:
    base = dict(
        subscription_id="s1",
        organization_id="1",
        plan="enterprise",
        billing_cycle="annual",
        status="active",
        seats=25,
        mrr_usd=Decimal("4200.00"),
    )
    Subscription(**base, start_date=date(2025, 1, 1), renewal_date=date(2026, 1, 1))
    with pytest.raises(ValidationError):
        Subscription(**base, start_date=date(2026, 1, 1), renewal_date=date(2025, 12, 31))
    with pytest.raises(ValidationError):
        Subscription(
            **{**base, "seats": 0}, start_date=date(2025, 1, 1), renewal_date=date(2026, 1, 1)
        )


def test_catalog_describes_every_field() -> None:
    catalog = catalog_by_path()
    assert catalog["organization.organization_id"].required is True
    assert catalog["organization.organization_id"].constraints["pattern"].startswith("^[A-Za-z0-9]")
    assert catalog["organization.lifecycle_status"].type == "enum"
    assert catalog["organization.lifecycle_status"].enum_values == (
        "prospect",
        "active",
        "inactive",
        "churned",
    )
    assert catalog["organization.website"].type == "url"
    assert catalog["organization.website"].nullable is True
    assert catalog["organization.annual_revenue_usd"].type == "decimal"
    assert catalog["organization.tags"].type == "list[string]"
    assert catalog["contact.email"].type == "email"
    assert catalog["contact.is_primary"].default is False
    assert catalog["subscription.renewal_date"].type == "date"
    assert catalog["activity.occurred_at"].type == "datetime"
    entities = {f.entity for f in target_field_catalog()}
    assert entities == {"organization", "contact", "subscription", "activity"}


def test_committed_json_schemas_are_fresh() -> None:
    """target_platform/schema/*.json must match the models. regenerate with
    `uv run python scripts/export_target_schema.py`."""
    from export_target_schema import OUT, render

    for name, content in render().items():
        path = OUT / name
        assert path.exists(), f"{path} missing, run scripts/export_target_schema.py"
        assert path.read_text() == content, f"{path} is stale, run scripts/export_target_schema.py"
