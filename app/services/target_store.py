"""the target platform's own write logic: what Meridian accepts, refuses, and treats as a no-op.

This is the platform's side of the seam, not the onboarding tool's. It knows nothing about
projects, mappings or migrations. Everything here is a rule a real target system would enforce
whatever client was calling it:

  the id contract      re-sending a record with identical content is a no-op, so a retry after a
                       timeout cannot double-write; the same id with different content is a 409
  relationships        a contact, subscription or activity needs its organization to exist first,
                       in the same namespace
  one primary          at most one contact per organization may be the primary
  unique email         inside one organization
  lifecycle            an inactive or churned organization cannot hold a trial or active
                       subscription

The field-level rules (types, enums, patterns, renewal after start) are in the pydantic models
in `app/target/schema.py` and are already enforced before anything gets here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ConflictError
from app.models.target import ID_COLUMN, LIVE_NAMESPACE, TARGET_TABLES, WRITE_ORDER
from app.target.schema import ENTITIES, LifecycleStatus, SubscriptionStatus, TargetModel

ACTIVE_SUBSCRIPTION_STATUSES = (SubscriptionStatus.trial.value, SubscriptionStatus.active.value)
DORMANT_ORG_STATUSES = (LifecycleStatus.inactive.value, LifecycleStatus.churned.value)


class MissingRelationshipError(AppError):
    """the record points at a parent that is not in this namespace."""

    status_code = 422
    error_type = "missing_relationship"


class TargetRuleError(AppError):
    """the write is well formed but breaks one of the platform's cross-record rules."""

    status_code = 422
    error_type = "business_rule_violation"


class TargetUnavailableError(AppError):
    """the platform itself failed. the caller may retry; ids make that safe."""

    status_code = 500
    error_type = "target_unavailable"


@dataclass(frozen=True)
class WriteOutcome:
    entity: str
    record_id: str
    created: bool
    unchanged: bool
    namespace: uuid.UUID
    changed_fields: tuple[str, ...] = ()


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def payload_of(record: TargetModel) -> dict[str, Any]:
    """the accepted record as plain json types, which is also what the hash is taken over."""
    return record.model_dump(mode="json")


def row_payload(entity: str, row: Any) -> dict[str, Any]:
    """a stored row back in the shape of the api model, so two versions can be compared."""
    model = ENTITIES[entity]
    return {name: getattr(row, name) for name in model.model_fields}


def _columns(entity: str, payload: dict[str, Any]) -> dict[str, Any]:
    model = ENTITIES[entity]
    return {name: payload[name] for name in model.model_fields if name in payload}


def _organization_exists(
    session: Session, namespace: uuid.UUID, organization_id: str
) -> Any | None:
    table = TARGET_TABLES["organization"]
    return session.execute(
        select(table).where(table.namespace == namespace, table.organization_id == organization_id)
    ).scalar_one_or_none()


def _check_relationships(session: Session, entity: str, payload: dict[str, Any], ns: uuid.UUID):
    if entity == "organization":
        return None
    organization_id = payload["organization_id"]
    parent = _organization_exists(session, ns, organization_id)
    if parent is None:
        raise MissingRelationshipError(
            f"no organization {organization_id} in this namespace; create it first",
            details={"entity": entity, "organization_id": organization_id, "namespace": str(ns)},
        )
    return parent


def _check_contact_rules(
    session: Session, payload: dict[str, Any], ns: uuid.UUID, record_id: str
) -> None:
    table = TARGET_TABLES["contact"]
    organization_id = payload["organization_id"]
    if payload.get("is_primary"):
        other = session.execute(
            select(table.contact_id).where(
                table.namespace == ns,
                table.organization_id == organization_id,
                table.is_primary.is_(True),
                table.contact_id != record_id,
            )
        ).scalar_one_or_none()
        if other is not None:
            raise ConflictError(
                f"organization {organization_id} already has a primary contact ({other})",
                error_type="duplicate_primary_contact",
                details={"organization_id": organization_id, "existing_contact_id": other},
            )
    clash = session.execute(
        select(table.contact_id).where(
            table.namespace == ns,
            table.organization_id == organization_id,
            func.lower(table.email) == str(payload["email"]).lower(),
            table.contact_id != record_id,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise ConflictError(
            f"email {payload['email']} already belongs to contact {clash} "
            f"in organization {organization_id}",
            error_type="duplicate_contact_email",
            details={"organization_id": organization_id, "existing_contact_id": clash},
        )


def _check_subscription_rules(parent: Any, payload: dict[str, Any]) -> None:
    if (
        parent.lifecycle_status in DORMANT_ORG_STATUSES
        and payload["status"] in ACTIVE_SUBSCRIPTION_STATUSES
    ):
        raise TargetRuleError(
            f"organization {parent.organization_id} is {parent.lifecycle_status} and cannot hold "
            f"a {payload['status']} subscription",
            details={
                "organization_id": parent.organization_id,
                "lifecycle_status": parent.lifecycle_status,
                "subscription_status": payload["status"],
            },
        )


def write_record(
    session: Session,
    entity: str,
    record: TargetModel,
    *,
    namespace: uuid.UUID = LIVE_NAMESPACE,
    request_id: str | None = None,
) -> WriteOutcome:
    """create the record, or confirm it is already there unchanged. anything else is an error."""
    if entity not in TARGET_TABLES:
        raise AppError(f"unknown entity {entity}", status_code=404, error_type="not_found")
    table = TARGET_TABLES[entity]
    id_column = ID_COLUMN[entity]
    payload = payload_of(record)
    record_id = payload[id_column]
    incoming_hash = content_hash(payload)

    existing = session.execute(
        select(table).where(table.namespace == namespace, getattr(table, id_column) == record_id)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash == incoming_hash:
            return WriteOutcome(
                entity, record_id, created=False, unchanged=True, namespace=namespace
            )
        stored = row_payload(entity, existing)
        changed = tuple(sorted(k for k, v in payload.items() if str(stored.get(k)) != str(v)))
        raise ConflictError(
            f"{entity} {record_id} already exists with different content",
            error_type="duplicate_identifier",
            details={
                "entity": entity,
                "record_id": record_id,
                "changed_fields": list(changed),
                "hint": "re-sending an identical record is a no-op; changing one is not a create",
            },
        )

    parent = _check_relationships(session, entity, payload, namespace)
    if entity == "contact":
        _check_contact_rules(session, payload, namespace, record_id)
    if entity == "subscription":
        _check_subscription_rules(parent, payload)

    row = table(
        namespace=namespace,
        content_hash=incoming_hash,
        written_by_request_id=request_id,
        **_columns(entity, payload),
    )
    session.add(row)
    session.flush()
    return WriteOutcome(entity, record_id, created=True, unchanged=False, namespace=namespace)


def counts(session: Session, namespace: uuid.UUID = LIVE_NAMESPACE) -> dict[str, int]:
    out: dict[str, int] = {}
    for entity, table in TARGET_TABLES.items():
        out[entity] = int(
            session.execute(
                select(func.count()).select_from(table).where(table.namespace == namespace)
            ).scalar_one()
        )
    return out


def list_records(
    session: Session,
    entity: str,
    *,
    namespace: uuid.UUID = LIVE_NAMESPACE,
    organization_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    table = TARGET_TABLES[entity]
    stmt = select(table).where(table.namespace == namespace)
    if organization_id is not None and entity != "organization":
        stmt = stmt.where(table.organization_id == organization_id)
    stmt = stmt.order_by(getattr(table, ID_COLUMN[entity])).limit(limit).offset(offset)
    return [row_payload(entity, row) for row in session.execute(stmt).scalars()]


def get_record(
    session: Session, entity: str, record_id: str, *, namespace: uuid.UUID = LIVE_NAMESPACE
) -> dict[str, Any] | None:
    table = TARGET_TABLES[entity]
    row = session.execute(
        select(table).where(
            table.namespace == namespace, getattr(table, ID_COLUMN[entity]) == record_id
        )
    ).scalar_one_or_none()
    return None if row is None else row_payload(entity, row)


def purge_namespace(session: Session, namespace: uuid.UUID) -> dict[str, int]:
    """throw away a staging namespace. the live namespace is refused."""
    if namespace == LIVE_NAMESPACE:
        raise AppError(
            "the live namespace cannot be purged through this api",
            status_code=422,
            error_type="invalid_namespace",
            details={"namespace": str(namespace)},
        )
    removed: dict[str, int] = {}
    # children first: the organization foreign key would cascade anyway, but deleting in order
    # means the counts reported back are the real per-entity numbers
    for entity in reversed(WRITE_ORDER):
        table = TARGET_TABLES[entity]
        result = session.execute(delete(table).where(table.namespace == namespace))
        removed[entity] = int(getattr(result, "rowcount", 0) or 0)
    session.flush()
    return removed
