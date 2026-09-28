import pytest
from pydantic import ValidationError

from app.schemas.project import ProjectCreate


def test_names_are_trimmed() -> None:
    p = ProjectCreate(customer_name="  Apex  ", project_name=" go-live ")
    assert p.customer_name == "Apex"
    assert p.project_name == "go-live"
    assert p.target_environment == "staging"
    assert p.source_systems == []


def test_blank_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(customer_name=" ", project_name="x")
