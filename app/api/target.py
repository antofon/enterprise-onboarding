"""Meridian's public write api, simulated.

This is the system the migration writes into. It is mounted in the same process as the
onboarding tool for the reviewer's convenience, but the migration layer only ever reaches it
over http, with a bearer token, one record per request, exactly as it would a real platform.

Two things make it useful as a rehearsal target rather than a stub:

  namespaces      `X-Meridian-Namespace: <uuid>` writes into an isolated staging area instead of
                  the live data. A dry run uses its own run id, so nothing it does can touch
                  production, and it can be inspected afterwards and purged.
  faults          the platform can be told to misbehave (500, timeout, malformed body) so the
                  migration's retry and reporting paths are exercised on purpose. Off by
                  default: a plain run should only fail where the customer's data is bad.
"""

from __future__ import annotations

import random
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.db import DbSession
from app.core.errors import NotFoundError, UnauthorizedError
from app.core.logging import get_logger
from app.models.target import ID_COLUMN, LIVE_NAMESPACE, TARGET_TABLES
from app.schemas.common import ErrorEnvelope
from app.services import target_store
from app.target.schema import PLATFORM_NAME, Activity, Contact, Organization, Subscription

log = get_logger(__name__)

router = APIRouter(prefix="/target/v1", tags=[f"mock target platform ({PLATFORM_NAME})"])

WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "already stored with identical content, nothing changed"},
    401: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope, "description": "id taken, duplicate primary contact or email"},
    422: {"model": ErrorEnvelope, "description": "missing relationship or rule violation"},
    500: {"model": ErrorEnvelope, "description": "the platform failed; the write can be retried"},
}

Namespace = Annotated[
    uuid.UUID | None,
    Header(
        alias="X-Meridian-Namespace",
        description="write into this staging namespace instead of the live data",
    ),
]
Fault = Annotated[
    str | None,
    Header(
        alias="X-Meridian-Fault",
        description="force a fault on this request: server_error, timeout or malformed",
    ),
]


class TargetWriteResult(BaseModel):
    platform: str = PLATFORM_NAME
    entity: str
    id: str
    namespace: uuid.UUID
    created: bool = Field(description="false when the record was already there, unchanged")
    unchanged: bool


class NamespacePurged(BaseModel):
    namespace: uuid.UUID
    removed: dict[str, int]


class TargetCounts(BaseModel):
    namespace: uuid.UUID
    counts: dict[str, int]


def _require_token(authorization: str | None) -> None:
    expected = f"Bearer {get_settings().target_api_token}"
    if authorization != expected:
        raise UnauthorizedError(
            f"{PLATFORM_NAME}: missing or invalid bearer token",
            details={"hint": "Authorization: Bearer <TARGET_API_TOKEN>"},
        )


PLURALS = {
    "organizations": "organization",
    "contacts": "contact",
    "subscriptions": "subscription",
    "activities": "activity",
}


def _entity_from_plural(entity_plural: str) -> str:
    entity = PLURALS.get(entity_plural)
    if entity is None or entity not in TARGET_TABLES:
        raise NotFoundError(
            f"{PLATFORM_NAME} has no {entity_plural} collection",
            details={"known": sorted(PLURALS)},
        )
    return entity


def _namespace(value: uuid.UUID | None) -> uuid.UUID:
    return value or LIVE_NAMESPACE


def _chosen_fault(entity: str, record_id: str, forced: str | None) -> str | None:
    """a forced fault, or one drawn deterministically from the configured rate. the draw is
    seeded with the record id so the same run fails on the same records every time."""
    settings = get_settings()
    modes = settings.target_fault_mode_list
    if forced:
        if forced not in modes and forced not in ("server_error", "timeout", "malformed"):
            return None
        return forced
    if settings.target_fault_rate <= 0 or not modes:
        return None
    draw = random.Random(f"{settings.target_fault_seed}:{entity}:{record_id}")
    if draw.random() >= settings.target_fault_rate:
        return None
    return draw.choice(modes)


MALFORMED_BODY = {"ok": "probably", "note": "this is not the documented write response"}


def _write(
    session: DbSession,
    entity: str,
    record: Any,
    namespace: uuid.UUID | None,
    forced_fault: str | None,
    request_id: str | None,
    response: Response,
) -> Any:
    ns = _namespace(namespace)
    record_id = getattr(record, ID_COLUMN[entity])
    fault = _chosen_fault(entity, record_id, forced_fault)
    if fault == "server_error":
        log.warning("target_fault", mode=fault, entity=entity, record_id=record_id)
        raise target_store.TargetUnavailableError(
            f"{PLATFORM_NAME} is temporarily unavailable",
            details={"entity": entity, "record_id": record_id, "injected": True},
        )
    if fault == "timeout":
        # the write still lands. a client that gives up and retries gets `unchanged`, which is
        # exactly the behaviour the id contract exists for.
        log.warning("target_fault", mode=fault, entity=entity, record_id=record_id)
        time.sleep(get_settings().target_fault_timeout_seconds)

    outcome = target_store.write_record(
        session, entity, record, namespace=ns, request_id=request_id
    )
    session.commit()
    if fault == "malformed":
        log.warning("target_fault", mode=fault, entity=entity, record_id=record_id)
        return JSONResponse(status_code=200, content=MALFORMED_BODY)
    response.status_code = status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    return TargetWriteResult(
        entity=entity,
        id=outcome.record_id,
        namespace=ns,
        created=outcome.created,
        unchanged=outcome.unchanged,
    )


@router.post(
    "/organizations",
    response_model=TargetWriteResult,
    status_code=status.HTTP_201_CREATED,
    summary="create a organization",
    responses=WRITE_RESPONSES,
)
def create_organization(
    body: Organization,
    session: DbSession,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
    x_meridian_fault: Fault = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> Any:
    _require_token(authorization)
    return _write(
        session,
        "organization",
        body,
        x_meridian_namespace,
        x_meridian_fault,
        x_request_id,
        response,
    )


@router.post(
    "/contacts",
    response_model=TargetWriteResult,
    status_code=status.HTTP_201_CREATED,
    summary="create a contact",
    responses=WRITE_RESPONSES,
)
def create_contact(
    body: Contact,
    session: DbSession,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
    x_meridian_fault: Fault = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> Any:
    _require_token(authorization)
    return _write(
        session,
        "contact",
        body,
        x_meridian_namespace,
        x_meridian_fault,
        x_request_id,
        response,
    )


@router.post(
    "/subscriptions",
    response_model=TargetWriteResult,
    status_code=status.HTTP_201_CREATED,
    summary="create a subscription",
    responses=WRITE_RESPONSES,
)
def create_subscription(
    body: Subscription,
    session: DbSession,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
    x_meridian_fault: Fault = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> Any:
    _require_token(authorization)
    return _write(
        session,
        "subscription",
        body,
        x_meridian_namespace,
        x_meridian_fault,
        x_request_id,
        response,
    )


@router.post(
    "/activities",
    response_model=TargetWriteResult,
    status_code=status.HTTP_201_CREATED,
    summary="create a activity",
    responses=WRITE_RESPONSES,
)
def create_activity(
    body: Activity,
    session: DbSession,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
    x_meridian_fault: Fault = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> Any:
    _require_token(authorization)
    return _write(
        session,
        "activity",
        body,
        x_meridian_namespace,
        x_meridian_fault,
        x_request_id,
        response,
    )


@router.get(
    "/counts",
    response_model=TargetCounts,
    summary="how many records of each entity the platform holds",
    responses={401: {"model": ErrorEnvelope}},
)
def record_counts(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
) -> TargetCounts:
    _require_token(authorization)
    ns = _namespace(x_meridian_namespace)
    return TargetCounts(namespace=ns, counts=target_store.counts(session, ns))


@router.get(
    "/{entity_plural}",
    response_model=list[dict],
    summary="list stored records of one entity",
    responses={401: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}},
)
def list_records(
    entity_plural: str,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
    organization_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    _require_token(authorization)
    entity = _entity_from_plural(entity_plural)
    return target_store.list_records(
        session,
        entity,
        namespace=_namespace(x_meridian_namespace),
        organization_id=organization_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{entity_plural}/{record_id}",
    response_model=dict,
    summary="one stored record",
    responses={401: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}},
)
def get_record(
    entity_plural: str,
    record_id: str,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_meridian_namespace: Namespace = None,
) -> dict:
    _require_token(authorization)
    entity = _entity_from_plural(entity_plural)
    found = target_store.get_record(
        session, entity, record_id, namespace=_namespace(x_meridian_namespace)
    )
    if found is None:
        raise NotFoundError(
            f"no {entity} {record_id} in this namespace", details={"record_id": record_id}
        )
    return found


@router.delete(
    "/namespaces/{namespace}",
    response_model=NamespacePurged,
    summary="throw away a staging namespace; the live namespace is refused",
    responses={401: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
)
def purge_namespace(
    namespace: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> NamespacePurged:
    _require_token(authorization)
    removed = target_store.purge_namespace(session, namespace)
    session.commit()
    log.info("target_namespace_purged", namespace=str(namespace), **removed)
    return NamespacePurged(namespace=namespace, removed=removed)
