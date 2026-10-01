"""sqlalchemy models. importing this package registers every table on Base.metadata."""

from app.models.mapping import (
    ClarificationQuestion,
    FieldMapping,
    LlmCall,
    MappingOrigin,
    MappingStatus,
    QuestionStatus,
)
from app.models.project import (
    DatasetKind,
    OnboardingProject,
    ProjectStage,
    SourceDataset,
    SourceField,
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
    "DatasetKind",
    "FieldMapping",
    "LlmCall",
    "MappingOrigin",
    "MappingStatus",
    "OnboardingProject",
    "ProjectStage",
    "QuestionStatus",
    "SourceDataset",
    "SourceField",
    "TargetActivity",
    "TargetContact",
    "TargetOrganization",
    "TargetSubscription",
]
