"""deterministic transformation: approved mappings plus reviewed configuration, no model calls.

config.py       the customer's value maps and rule settings, loaded from yaml
converters.py   the normalizers, pure functions of one value
record_rules.py the customer's rules that need the whole record, or every record
plan.py         approved mappings resolved into the chain that will run on each column
engine.py       the pass over the rows
"""

from app.services.transform.config import TransformationConfig, get_config, load_config
from app.services.transform.engine import TransformResult, transform_project
from app.services.transform.plan import TransformationPlan, build_plan
from app.services.transform.types import RecordDraft, RecordIssue

__all__ = [
    "RecordDraft",
    "RecordIssue",
    "TransformResult",
    "TransformationConfig",
    "TransformationPlan",
    "build_plan",
    "get_config",
    "load_config",
    "transform_project",
]
