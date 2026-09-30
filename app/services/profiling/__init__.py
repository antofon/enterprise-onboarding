"""source data profiling. deterministic, pandas + plain python, no model involved.

load_source   -> a dataframe of raw string values (csv, json export, or a paginated api)
profile_frame -> per-column types, null rates, uniqueness, formats, malformed counts, samples
assess        -> the quality issues a human should look at, with severities
infer_references -> which columns point at which other dataset's key, and how many miss
"""

from app.services.profiling.loaders import load_source, resolve_source_path
from app.services.profiling.profiler import profile_frame
from app.services.profiling.quality import assess
from app.services.profiling.relationships import infer_references
from app.services.profiling.types import (
    DatasetProfile,
    FieldProfile,
    QualityIssue,
    Reference,
    Severity,
)

__all__ = [
    "DatasetProfile",
    "FieldProfile",
    "QualityIssue",
    "Reference",
    "Severity",
    "assess",
    "infer_references",
    "load_source",
    "profile_frame",
    "resolve_source_path",
]
