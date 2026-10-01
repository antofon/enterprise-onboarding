"""the transformation plan: what the approved mappings mean, field by field, before any row runs.

The plan is derived, not stored, the same way the schema comparison is: approved mappings plus the
target catalog plus the customer's configuration, and nothing else. That means it can be read
before a migration ("show me what you are about to do to every column") and that two runs from the
same approvals do the same thing.

Choosing the normalizer is the target's job, not a guess: the field's type and its own constraints
pick the chain, and the customer's configuration overrides it only where a type cannot know (one
name column becoming two, a phone format, a country vocabulary).

A dataset emits records for an entity only when that entity's identifier has an approved mapping.
Everything else mapped into another entity becomes context: the account row's primary contact
email reaches the contacts that way, without inventing a contact record out of an email address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import InvalidStateError
from app.models.mapping import FieldMapping, MappingStatus
from app.models.project import OnboardingProject
from app.models.target import ID_COLUMN, WRITE_ORDER
from app.services.transform.config import TransformationConfig, get_config
from app.target.catalog import TargetField, catalog_by_path

BASE_CHAIN: tuple[str, ...] = ("trim", "blank_to_null")

# the normalizer a target type asks for, before any override
BY_TYPE: dict[str, str] = {
    "enum": "enum_map",
    "date": "date",
    "datetime": "datetime",
    "decimal": "money",
    "integer": "integer",
    "boolean": "boolean",
    "email": "email",
    "url": "url",
    "list[string]": "string_list",
    "string": "string",
}
IDENTIFIER_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_.-]*$"


@dataclass(frozen=True)
class FieldRule:
    """one approved mapping, resolved into the chain that will run on every row."""

    dataset: str
    source_field: str
    target_path: str
    entity: str
    emits: tuple[str, ...]
    converters: tuple[str, ...]
    chosen_by: str
    note: str | None = None
    value_map: str | None = None

    @property
    def target_field(self) -> str:
        return self.target_path.split(".", 1)[1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source_field": self.source_field,
            "target_path": self.target_path,
            "entity": self.entity,
            "emits": list(self.emits),
            "converters": list(self.converters),
            "chosen_by": self.chosen_by,
            "note": self.note,
            "value_map": self.value_map,
        }


@dataclass(frozen=True)
class DroppedField:
    """a source column that will not travel, and why. the readiness report lists these."""

    dataset: str
    source_field: str
    status: str
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source_field": self.source_field,
            "status": self.status,
            "why": self.why,
        }


@dataclass(frozen=True)
class DatasetPlan:
    dataset: str
    kind: str
    location: str
    emits: tuple[str, ...]
    field_rules: tuple[FieldRule, ...] = ()
    context_rules: tuple[FieldRule, ...] = ()
    dropped: tuple[DroppedField, ...] = ()

    def rules_for(self, entity: str) -> tuple[FieldRule, ...]:
        return tuple(r for r in self.field_rules if r.entity == entity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "kind": self.kind,
            "location": self.location,
            "emits": list(self.emits),
            "field_rules": [r.as_dict() for r in self.field_rules],
            "context_rules": [r.as_dict() for r in self.context_rules],
            "dropped": [d.as_dict() for d in self.dropped],
        }


@dataclass(frozen=True)
class TransformationPlan:
    customer: str
    config_version: str
    as_of: date
    datasets: tuple[DatasetPlan, ...] = ()
    record_rules: tuple[str, ...] = ()
    cross_dataset_rules: tuple[str, ...] = ()
    value_maps: tuple[str, ...] = ()
    entities: tuple[str, ...] = field(default=())

    def dataset(self, name: str) -> DatasetPlan:
        for plan in self.datasets:
            if plan.dataset == name:
                return plan
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "customer": self.customer,
            "config_version": self.config_version,
            "as_of": self.as_of.isoformat(),
            "entities": list(self.entities),
            "record_rules": list(self.record_rules),
            "cross_dataset_rules": list(self.cross_dataset_rules),
            "value_maps": list(self.value_maps),
            "datasets": [d.as_dict() for d in self.datasets],
        }


def chain_for(
    target: TargetField, config: TransformationConfig
) -> tuple[tuple[str, ...], tuple[str, ...], str, str | None]:
    """the converter chain for one target field: (chain, emits, why, note)."""
    override = config.override(target.path)
    if override is not None:
        emits = tuple(override.emits) if override.emits else (target.path,)
        return (
            BASE_CHAIN + tuple(override.converters),
            emits,
            "configured override",
            override.note,
        )
    if target.type == "string" and target.constraints.get("pattern") == IDENTIFIER_PATTERN:
        return BASE_CHAIN + ("identifier",), (target.path,), "identifier constraint", None
    converter = BY_TYPE.get(target.type, "string")
    return BASE_CHAIN + (converter,), (target.path,), f"{target.type} target", None


def _drop_reason(mapping: FieldMapping) -> str:
    if mapping.status == MappingStatus.rejected:
        return "a reviewer rejected the proposed target"
    if mapping.status == MappingStatus.ignored:
        return "a reviewer marked the column as not migrating"
    return "no target field was approved for this column"


def _check_ready(project: OnboardingProject, mappings: list[FieldMapping]) -> None:
    if not mappings:
        raise InvalidStateError(
            "there are no mappings to transform; run the mapping step first",
            details={"project_id": str(project.id)},
        )
    undecided = [m for m in mappings if not m.decided]
    if undecided:
        raise InvalidStateError(
            f"{len(undecided)} mappings are still undecided; every column has to be approved, "
            "rejected or ignored before a transformation runs",
            details={
                "undecided": [f"{m.dataset.name}.{m.source_field}" for m in undecided][:20],
                "undecided_count": len(undecided),
            },
        )
    approved = [m for m in mappings if m.status == MappingStatus.approved and m.target_path]
    if not approved:
        raise InvalidStateError(
            "no mapping is approved, so there is nothing to transform",
            details={"project_id": str(project.id)},
        )


def build_plan(
    session: Session,
    project: OnboardingProject,
    *,
    config: TransformationConfig | None = None,
) -> TransformationPlan:
    """resolve the project's approved mappings into a plan, or explain what is missing."""
    from app.services.mapping import list_mappings

    config = config or get_config()
    catalog = catalog_by_path()
    mappings = list_mappings(session, project)
    _check_ready(project, mappings)

    by_dataset: dict[str, list[FieldMapping]] = {}
    for mapping in mappings:
        by_dataset.setdefault(mapping.dataset.name, []).append(mapping)

    dataset_plans: list[DatasetPlan] = []
    entities_seen: set[str] = set()
    value_maps_used: set[str] = set()
    for dataset in project.datasets:
        rows = by_dataset.get(dataset.name, [])
        approved = [m for m in rows if m.status == MappingStatus.approved and m.target_path]
        dropped = tuple(
            DroppedField(dataset.name, m.source_field, m.status.value, _drop_reason(m))
            for m in rows
            if m.status != MappingStatus.approved or not m.target_path
        )
        # an entity travels from this dataset only if its identifier is mapped
        id_paths = {f"{entity}.{column}" for entity, column in ID_COLUMN.items()}
        emitted = {m.target_path.split(".", 1)[0] for m in approved if m.target_path in id_paths}
        field_rules: list[FieldRule] = []
        context_rules: list[FieldRule] = []
        for mapping in approved:
            target = catalog.get(str(mapping.target_path))
            if target is None:
                raise InvalidStateError(
                    f"{dataset.name}.{mapping.source_field} is approved onto "
                    f"{mapping.target_path}, which is not a target field any more",
                    details={"dataset": dataset.name, "source_field": mapping.source_field},
                )
            chain, emits, chosen_by, note = chain_for(target, config)
            vmap = config.value_map(target.path)
            rule = FieldRule(
                dataset=dataset.name,
                source_field=mapping.source_field,
                target_path=target.path,
                entity=target.entity,
                emits=emits,
                converters=chain,
                chosen_by=chosen_by,
                note=note or (mapping.transformation if mapping.transformation_required else None),
                value_map=target.path if vmap else None,
            )
            if vmap:
                value_maps_used.add(target.path)
            (field_rules if target.entity in emitted else context_rules).append(rule)
        entities_seen |= emitted
        dataset_plans.append(
            DatasetPlan(
                dataset=dataset.name,
                kind=dataset.kind.value,
                location=dataset.location,
                emits=tuple(e for e in WRITE_ORDER if e in emitted),
                field_rules=tuple(field_rules),
                context_rules=tuple(context_rules),
                dropped=dropped,
            )
        )

    if not entities_seen:
        raise InvalidStateError(
            "no dataset has an approved identifier mapping, so no records can be built. "
            "map a source column onto one of "
            + ", ".join(f"{e}.{c}" for e, c in ID_COLUMN.items()),
            details={"datasets": [d.dataset for d in dataset_plans]},
        )

    return TransformationPlan(
        customer=config.customer,
        config_version=config.version,
        as_of=config.as_of,
        datasets=tuple(dataset_plans),
        record_rules=tuple(sorted(config.enabled_record_rules())),
        cross_dataset_rules=tuple(sorted(config.enabled_cross_dataset_rules())),
        value_maps=tuple(sorted(value_maps_used)),
        entities=tuple(e for e in WRITE_ORDER if e in entities_seen),
    )


__all__ = [
    "BASE_CHAIN",
    "DatasetPlan",
    "DroppedField",
    "FieldRule",
    "TransformationPlan",
    "build_plan",
    "chain_for",
]
