"""flatten the target models into the list of fields the source data has to land in.
this is what schema comparison classifies against and what the mapping prompt sees."""

from __future__ import annotations

import enum
import types
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import EmailStr, HttpUrl
from pydantic_core import PydanticUndefined

from app.target.schema import ENTITIES

_SCALARS: dict[Any, str] = {
    str: "string",
    int: "integer",
    bool: "boolean",
    Decimal: "decimal",
    date: "date",
    datetime: "datetime",
    HttpUrl: "url",
    EmailStr: "email",
}


@dataclass(frozen=True)
class TargetField:
    entity: str
    name: str
    path: str
    type: str
    required: bool
    nullable: bool
    enum_values: tuple[str, ...] | None
    description: str
    default: Any = None
    constraints: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if isinstance(d["default"], enum.Enum):
            d["default"] = d["default"].value
        d["enum_values"] = list(self.enum_values) if self.enum_values else None
        return d


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _type_info(annotation: Any) -> tuple[str, bool, tuple[str, ...] | None]:
    annotation = _strip_annotated(annotation)
    nullable = False
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        non_null = [a for a in args if a is not type(None)]
        nullable = len(non_null) < len(args)
        annotation = _strip_annotated(non_null[0])
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return "enum", nullable, tuple(m.value for m in annotation)
    if get_origin(annotation) is list:
        inner = _strip_annotated(get_args(annotation)[0]) if get_args(annotation) else str
        return f"list[{_SCALARS.get(inner, 'string')}]", nullable, None
    return _SCALARS.get(annotation, "string"), nullable, None


def _constraints(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """the useful validation keywords for one property from the json schema, refs resolved."""
    if "$ref" in prop:
        prop = defs.get(prop["$ref"].split("/")[-1], {})
    if "anyOf" in prop:
        candidates = [c for c in prop["anyOf"] if c.get("type") != "null"]
        merged: dict[str, Any] = {}
        for c in candidates:
            merged.update(_constraints(c, defs))
        return merged
    keep = ("pattern", "minLength", "maxLength", "minimum", "maximum", "format", "maxItems")
    return {k: prop[k] for k in keep if k in prop}


def target_field_catalog() -> list[TargetField]:
    out: list[TargetField] = []
    for entity, model in ENTITIES.items():
        schema = model.model_json_schema()
        defs = schema.get("$defs", {})
        props = schema.get("properties", {})
        for name, info in model.model_fields.items():
            type_label, nullable, enum_values = _type_info(info.annotation)
            default = None if info.default is PydanticUndefined else info.default
            out.append(
                TargetField(
                    entity=entity,
                    name=name,
                    path=f"{entity}.{name}",
                    type=type_label,
                    required=info.is_required(),
                    nullable=nullable,
                    enum_values=enum_values,
                    description=info.description or "",
                    default=default,
                    constraints=_constraints(props.get(name, {}), defs),
                )
            )
    return out


def catalog_by_path() -> dict[str, TargetField]:
    return {f.path: f for f in target_field_catalog()}
