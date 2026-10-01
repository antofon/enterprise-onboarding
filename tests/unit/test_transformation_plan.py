"""picking the normalizer. the target's own type and constraints choose it; configuration
overrides only where a type cannot know."""

from __future__ import annotations

import pytest

from app.services.transform.config import load_config
from app.services.transform.plan import BASE_CHAIN, chain_for
from app.target.catalog import catalog_by_path


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def catalog():
    return catalog_by_path()


@pytest.mark.parametrize(
    ("path", "converter", "why"),
    [
        ("organization.lifecycle_status", "enum_map", "enum target"),
        ("organization.customer_since", "date", "date target"),
        ("activity.occurred_at", "datetime", "datetime target"),
        ("organization.annual_revenue_usd", "money", "decimal target"),
        ("organization.employee_count", "integer", "integer target"),
        ("contact.is_primary", "boolean", "boolean target"),
        ("contact.email", "email", "email target"),
        ("organization.website", "url", "url target"),
        ("organization.tags", "string_list", "list[string] target"),
        ("organization.name", "string", "string target"),
        ("organization.organization_id", "identifier", "identifier constraint"),
        ("contact.organization_id", "identifier", "identifier constraint"),
    ],
)
def test_the_target_type_chooses_the_normalizer(config, catalog, path, converter, why) -> None:
    chain, emits, chosen_by, _ = chain_for(catalog[path], config)
    assert chain == BASE_CHAIN + (converter,)
    assert emits == (path,)
    assert chosen_by == why


def test_every_chain_starts_by_trimming_and_reading_placeholders_as_empty(config, catalog) -> None:
    for target in catalog.values():
        chain, _, _, _ = chain_for(target, config)
        assert chain[:2] == BASE_CHAIN, target.path


def test_configuration_overrides_a_type_only_where_it_has_to(config, catalog) -> None:
    chain, emits, chosen_by, note = chain_for(catalog["contact.first_name"], config)
    assert chain == BASE_CHAIN + ("person_name_split",)
    assert emits == ("contact.first_name", "contact.last_name")
    assert chosen_by == "configured override"
    assert note

    phone, _, _, _ = chain_for(catalog["contact.phone"], config)
    assert phone == BASE_CHAIN + ("phone_e164",)

    country, _, _, _ = chain_for(catalog["organization.billing_country"], config)
    assert country == BASE_CHAIN + ("country_code",)


def test_a_configured_converter_that_does_not_exist_is_a_load_error() -> None:
    from app.services.transform.config import (
        ConfigError,
        TransformationConfig,
        _check_converters_exist,
    )

    config = TransformationConfig.model_validate(
        {
            "customer": "Test",
            "version": "1",
            "as_of": "2026-10-01",
            "date_formats": ["%Y-%m-%d"],
            "datetime_formats": ["%Y-%m-%dT%H:%M:%S%z"],
            "field_overrides": {"contact.phone": {"converters": ["make_it_nice"]}},
        }
    )
    with pytest.raises(ConfigError) as caught:
        _check_converters_exist(config)
    assert "make_it_nice" in str(caught.value)


def test_a_yaml_boolean_in_a_value_map_is_refused_with_an_explanation() -> None:
    from pydantic import ValidationError

    from app.services.transform.config import ValueMap

    with pytest.raises(ValidationError) as caught:
        ValueMap.model_validate({"map": {"ontario": True}})
    assert "yaml booleans" in str(caught.value)
