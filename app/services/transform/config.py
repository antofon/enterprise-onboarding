"""the customer-specific half of the transformation stage, loaded from reviewed configuration.

The normalizers are code. Which of the customer's words mean which of Meridian's values, what to
do with a word that is not on the list, which record rules are on and with what thresholds: that
is configuration, one file per customer, read-only at runtime and never written by a model.

A second customer is a second file, which is the point.
"""

from __future__ import annotations

import enum
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import Settings, get_settings
from app.core.errors import AppError


class ConfigError(AppError):
    """the configuration file is missing or does not say what it has to."""

    status_code = 500
    error_type = "transformation_config_invalid"


class UnmappedPolicy(enum.StrEnum):
    """what happens to a value that is not in the map. never "guess"."""

    error = "error"
    default = "default"
    null = "null"


class ValueMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str | None = Field(default=None, description="the numbered customer rule this comes from")
    description: str | None = None
    unmapped: UnmappedPolicy = UnmappedPolicy.error
    map: dict[str, str] = Field(default_factory=dict)

    @field_validator("rule", mode="before")
    @classmethod
    def _rule_as_text(cls, value: Any) -> Any:
        return None if value is None else str(value)

    @field_validator("map", mode="before")
    @classmethod
    def _lowercase_keys(cls, value: Any) -> Any:
        """keys are matched case-insensitively, so they are stored lowercased.

        A bare `ON` or `NO` in yaml is a boolean, not a province code, and the resulting error is
        confusing two hundred lines into a config file. It is named here instead.
        """
        if not isinstance(value, dict):
            return value
        out: dict[str, Any] = {}
        for key, mapped in value.items():
            if isinstance(mapped, bool) or mapped is None:
                raise ValueError(
                    f"{key!r} maps to the yaml literal {mapped!r}; quote the value "
                    "(ON, OFF, YES, NO, TRUE, FALSE and NULL are yaml booleans and nulls)"
                )
            out[str(key).strip().lower()] = mapped
        return out


class FieldOverride(BaseModel):
    """the converter chain for one target field, where its type is not enough to choose one."""

    model_config = ConfigDict(extra="forbid")

    converters: list[str]
    emits: list[str] = Field(
        default_factory=list, description="every target path this chain fills; default just itself"
    )
    note: str | None = None


class RecordRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    rule: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rule", mode="before")
    @classmethod
    def _rule_as_text(cls, value: Any) -> Any:
        return None if value is None else str(value)


class TransformationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer: str
    version: str
    as_of: date = Field(
        description="every date rule is measured from here, so a run next month reproduces today"
    )
    date_formats: list[str]
    datetime_formats: list[str]
    null_placeholders: list[str] = Field(default_factory=list)
    value_maps: dict[str, ValueMap] = Field(default_factory=dict)
    field_overrides: dict[str, FieldOverride] = Field(default_factory=dict)
    record_rules: dict[str, RecordRuleConfig] = Field(default_factory=dict)
    cross_dataset_rules: dict[str, RecordRuleConfig] = Field(default_factory=dict)
    validation_rules: dict[str, RecordRuleConfig] = Field(
        default_factory=dict,
        description="checks from the customer's rules that flag a conflict without changing data",
    )

    @property
    def null_placeholder_set(self) -> set[str]:
        return {p.strip().lower() for p in self.null_placeholders}

    def value_map(self, target_path: str) -> ValueMap | None:
        return self.value_maps.get(target_path)

    def override(self, target_path: str) -> FieldOverride | None:
        return self.field_overrides.get(target_path)

    def enabled_record_rules(self) -> dict[str, RecordRuleConfig]:
        return {k: v for k, v in self.record_rules.items() if v.enabled}

    def enabled_cross_dataset_rules(self) -> dict[str, RecordRuleConfig]:
        return {k: v for k, v in self.cross_dataset_rules.items() if v.enabled}

    def summary(self) -> dict[str, Any]:
        """what a run records about the configuration it used."""
        return {
            "customer": self.customer,
            "version": self.version,
            "as_of": self.as_of.isoformat(),
            "value_maps": sorted(self.value_maps),
            "record_rules": sorted(self.enabled_record_rules()),
            "cross_dataset_rules": sorted(self.enabled_cross_dataset_rules()),
            "validation_rules": sorted(k for k, v in self.validation_rules.items() if v.enabled),
        }


def load_config(path: str | None = None, settings: Settings | None = None) -> TransformationConfig:
    settings = settings or get_settings()
    location = path or settings.transformation_config
    file = Path(location)
    if not file.is_file():
        raise ConfigError(
            f"no transformation configuration at {location}",
            details={"path": location},
        )
    try:
        payload = yaml.safe_load(file.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"{location} is not valid yaml", details={"reason": str(exc)[:300]}
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"{location} must be a mapping", details={"path": location})
    try:
        config = TransformationConfig.model_validate(payload)
    except Exception as exc:
        raise ConfigError(
            f"{location} is missing something the transformation stage needs",
            details={"reason": str(exc)[:500]},
        ) from exc
    _check_converters_exist(config)
    return config


def _check_converters_exist(config: TransformationConfig) -> None:
    """a typo in a converter name is a configuration error found at load time, not at row 4,000."""
    from app.services.transform.converters import REGISTRY

    for path, override in config.field_overrides.items():
        unknown = [c for c in override.converters if c not in REGISTRY]
        if unknown:
            raise ConfigError(
                f"{path} asks for converters that do not exist: {', '.join(unknown)}",
                details={"known": sorted(REGISTRY)},
            )


@lru_cache
def get_config() -> TransformationConfig:
    """the configured file, read once per process."""
    return load_config()


__all__ = [
    "ConfigError",
    "FieldOverride",
    "RecordRuleConfig",
    "TransformationConfig",
    "UnmappedPolicy",
    "ValueMap",
    "get_config",
    "load_config",
]
