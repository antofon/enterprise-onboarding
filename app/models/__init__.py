"""sqlalchemy models. importing this package registers every table on Base.metadata."""

from app.models.mapping import (
    ClarificationQuestion,
    FieldMapping,
    LlmCall,
    MappingOrigin,
    MappingStatus,
    QuestionStatus,
)
from app.models.migration import (
    FailureStage,
    MigrationFailure,
    MigrationRun,
    RunKind,
    RunStatus,
    ValidationIssueRow,
)
from app.models.project import (
    DatasetKind,
    OnboardingProject,
    ProjectStage,
    SourceDataset,
    SourceField,
)
from app.models.report import (
    ReadinessReport,
    ReadinessStatus,
    ReconciliationResult,
    ReconciliationStatus,
)
from app.models.target import (
    LIVE_NAMESPACE,
    TargetActivity,
    TargetContact,
    TargetOrganization,
    TargetSubscription,
)

__all__ = [
    "LIVE_NAMESPACE",
    "ClarificationQuestion",
    "FailureStage",
    "MigrationFailure",
    "MigrationRun",
    "RunKind",
    "RunStatus",
    "ValidationIssueRow",
    "DatasetKind",
    "FieldMapping",
    "LlmCall",
    "MappingOrigin",
    "MappingStatus",
    "OnboardingProject",
    "ProjectStage",
    "QuestionStatus",
    "ReadinessReport",
    "ReadinessStatus",
    "ReconciliationResult",
    "ReconciliationStatus",
    "SourceDataset",
    "SourceField",
    "TargetActivity",
    "TargetContact",
    "TargetOrganization",
    "TargetSubscription",
]
