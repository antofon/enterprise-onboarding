"""baseline: the onboarding and target schemas as create_all built them

Every table the app had when alembic took over, generated from the models against an empty
database and checked against the databases create_all had built: no difference. A database from
before this revision is stamped with it rather than rebuilt (see app/core/migrations.py).

Revision ID: 0001
Revises:
Create Date: 2026-10-02 17:14:43.683711
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('create schema if not exists "onboarding"')
    op.execute('create schema if not exists "target"')
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=False),
        sa.Column("project_name", sa.String(length=200), nullable=False),
        sa.Column("source_systems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_environment", sa.String(length=50), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "stage",
            sa.Enum(
                "created",
                "profiled",
                "mapped",
                "in_review",
                "ready_to_transform",
                "validated",
                "dry_run_complete",
                "reported",
                name="project_stage",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_table(
        "organizations",
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("industry", sa.String(length=32), nullable=False),
        sa.Column("account_priority", sa.String(length=32), nullable=False),
        sa.Column("billing_country", sa.String(length=2), nullable=False),
        sa.Column("billing_region", sa.String(length=10), nullable=True),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("annual_revenue_usd", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("customer_since", sa.Date(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("namespace", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("written_by_request_id", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("organization_id", "namespace"),
        schema="target",
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("dataset", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_llm_calls_input_hash"),
        "llm_calls",
        ["input_hash"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_llm_calls_project_id"),
        "llm_calls",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "migration_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("validation", "dry_run", name="run_kind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running", "completed", "failed", name="run_status", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("namespace", sa.UUID(), nullable=True),
        sa.Column("target_environment", sa.String(length=50), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("config_version", sa.String(length=50), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("totals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("issue_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applied_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalizations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("issues_truncated", sa.Boolean(), nullable=False),
        sa.Column("failures_truncated", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_runs_kind"),
        "migration_runs",
        ["kind"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_runs_project_id"),
        "migration_runs",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_runs_status"),
        "migration_runs",
        ["status"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "source_datasets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("csv", "json", "api", name="dataset_kind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("column_count", sa.Integer(), nullable=True),
        sa.Column("profiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_source_dataset_project_name"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_source_datasets_project_id"),
        "source_datasets",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "activities",
        sa.Column("activity_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_usd", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("namespace", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("written_by_request_id", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["namespace", "organization_id"],
            ["target.organizations.namespace", "target.organizations.organization_id"],
            name="fk_organization_id_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("activity_id", "namespace"),
        schema="target",
    )
    op.create_index(
        op.f("ix_target_activities_organization_id"),
        "activities",
        ["organization_id"],
        unique=False,
        schema="target",
    )
    op.create_table(
        "contacts",
        sa.Column("contact_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("namespace", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("written_by_request_id", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["namespace", "organization_id"],
            ["target.organizations.namespace", "target.organizations.organization_id"],
            name="fk_organization_id_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("contact_id", "namespace"),
        schema="target",
    )
    op.create_index(
        op.f("ix_target_contacts_organization_id"),
        "contacts",
        ["organization_id"],
        unique=False,
        schema="target",
    )
    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("billing_cycle", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("renewal_date", sa.Date(), nullable=False),
        sa.Column("mrr_usd", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("namespace", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("written_by_request_id", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["namespace", "organization_id"],
            ["target.organizations.namespace", "target.organizations.organization_id"],
            name="fk_organization_id_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subscription_id", "namespace"),
        schema="target",
    )
    op.create_index(
        op.f("ix_target_subscriptions_organization_id"),
        "subscriptions",
        ["organization_id"],
        unique=False,
        schema="target",
    )
    op.create_table(
        "field_mappings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("source_field", sa.String(length=200), nullable=False),
        sa.Column("inferred_type", sa.String(length=32), nullable=True),
        sa.Column("entity", sa.String(length=32), nullable=True),
        sa.Column("target_path", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "suggested",
                "needs_clarification",
                "approved",
                "rejected",
                "ignored",
                name="mapping_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "origin",
            sa.Enum(
                "model", "heuristic", "manual", name="mapping_origin", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("transformation_required", sa.Boolean(), nullable=False),
        sa.Column("transformation", sa.Text(), nullable=True),
        sa.Column("clarification_required", sa.Boolean(), nullable=False),
        sa.Column("comparison_class", sa.String(length=32), nullable=True),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("llm_call_id", sa.UUID(), nullable=True),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["onboarding.source_datasets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "dataset_id", "source_field", name="uq_field_mapping_project_field"
        ),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_field_mappings_dataset_id"),
        "field_mappings",
        ["dataset_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_field_mappings_project_id"),
        "field_mappings",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_field_mappings_status"),
        "field_mappings",
        ["status"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "migration_failures",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("record_id", sa.String(length=64), nullable=True),
        sa.Column("dataset", sa.String(length=100), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column(
            "stage",
            sa.Enum("target", "blocked", name="failure_stage", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("response_excerpt", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["onboarding.migration_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_failures_entity"),
        "migration_failures",
        ["entity"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_failures_error_type"),
        "migration_failures",
        ["error_type"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_failures_record_id"),
        "migration_failures",
        ["record_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_migration_failures_run_id"),
        "migration_failures",
        ["run_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "reconciliation_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "balanced",
                "discrepancies",
                name="reconciliation_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("namespace", sa.UUID(), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("totals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("discrepancies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ledger", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["onboarding.migration_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_reconciliation_results_project_id"),
        "reconciliation_results",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_reconciliation_results_run_id"),
        "reconciliation_results",
        ["run_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "source_fields",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("inferred_type", sa.String(length=32), nullable=False),
        sa.Column("null_pct", sa.Float(), nullable=False),
        sa.Column("unique_pct", sa.Float(), nullable=False),
        sa.Column("distinct_count", sa.Integer(), nullable=False),
        sa.Column("sample_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["onboarding.source_datasets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "name", name="uq_source_field_dataset_name"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_source_fields_dataset_id"),
        "source_fields",
        ["dataset_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "validation_issues",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("dataset", sa.String(length=100), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("record_id", sa.String(length=64), nullable=True),
        sa.Column("field", sa.String(length=100), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("value", sa.String(length=300), nullable=True),
        sa.Column("rule", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["onboarding.migration_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_validation_issues_entity"),
        "validation_issues",
        ["entity"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_validation_issues_error_type"),
        "validation_issues",
        ["error_type"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_validation_issues_project_id"),
        "validation_issues",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_validation_issues_record_id"),
        "validation_issues",
        ["record_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_validation_issues_run_id"),
        "validation_issues",
        ["run_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_validation_issues_severity"),
        "validation_issues",
        ["severity"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "clarification_questions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("mapping_id", sa.UUID(), nullable=True),
        sa.Column("dataset_name", sa.String(length=100), nullable=True),
        sa.Column("source_field", sa.String(length=200), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "origin",
            sa.Enum(
                "model", "heuristic", "manual", name="question_origin", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "answered",
                "withdrawn",
                name="question_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.String(length=100), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["mapping_id"], ["onboarding.field_mappings.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_clarification_questions_mapping_id"),
        "clarification_questions",
        ["mapping_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_clarification_questions_project_id"),
        "clarification_questions",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_clarification_questions_status"),
        "clarification_questions",
        ["status"],
        unique=False,
        schema="onboarding",
    )
    op.create_table(
        "readiness_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("reconciliation_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "READY",
                "READY WITH CONDITIONS",
                "BLOCKED",
                name="readiness_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("summary_origin", sa.String(length=20), nullable=False),
        sa.Column("llm_call_id", sa.UUID(), nullable=True),
        sa.Column("generated_by", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["llm_call_id"], ["onboarding.llm_calls.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["onboarding.projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"], ["onboarding.reconciliation_results.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["onboarding.migration_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="onboarding",
    )
    op.create_index(
        op.f("ix_onboarding_readiness_reports_project_id"),
        "readiness_reports",
        ["project_id"],
        unique=False,
        schema="onboarding",
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_onboarding_readiness_reports_project_id"),
        table_name="readiness_reports",
        schema="onboarding",
    )
    op.drop_table("readiness_reports", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_clarification_questions_status"),
        table_name="clarification_questions",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_clarification_questions_project_id"),
        table_name="clarification_questions",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_clarification_questions_mapping_id"),
        table_name="clarification_questions",
        schema="onboarding",
    )
    op.drop_table("clarification_questions", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_validation_issues_severity"),
        table_name="validation_issues",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_validation_issues_run_id"),
        table_name="validation_issues",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_validation_issues_record_id"),
        table_name="validation_issues",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_validation_issues_project_id"),
        table_name="validation_issues",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_validation_issues_error_type"),
        table_name="validation_issues",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_validation_issues_entity"),
        table_name="validation_issues",
        schema="onboarding",
    )
    op.drop_table("validation_issues", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_source_fields_dataset_id"),
        table_name="source_fields",
        schema="onboarding",
    )
    op.drop_table("source_fields", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_reconciliation_results_run_id"),
        table_name="reconciliation_results",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_reconciliation_results_project_id"),
        table_name="reconciliation_results",
        schema="onboarding",
    )
    op.drop_table("reconciliation_results", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_migration_failures_run_id"),
        table_name="migration_failures",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_migration_failures_record_id"),
        table_name="migration_failures",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_migration_failures_error_type"),
        table_name="migration_failures",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_migration_failures_entity"),
        table_name="migration_failures",
        schema="onboarding",
    )
    op.drop_table("migration_failures", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_field_mappings_status"),
        table_name="field_mappings",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_field_mappings_project_id"),
        table_name="field_mappings",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_field_mappings_dataset_id"),
        table_name="field_mappings",
        schema="onboarding",
    )
    op.drop_table("field_mappings", schema="onboarding")
    op.drop_index(
        op.f("ix_target_subscriptions_organization_id"), table_name="subscriptions", schema="target"
    )
    op.drop_table("subscriptions", schema="target")
    op.drop_index(
        op.f("ix_target_contacts_organization_id"), table_name="contacts", schema="target"
    )
    op.drop_table("contacts", schema="target")
    op.drop_index(
        op.f("ix_target_activities_organization_id"), table_name="activities", schema="target"
    )
    op.drop_table("activities", schema="target")
    op.drop_index(
        op.f("ix_onboarding_source_datasets_project_id"),
        table_name="source_datasets",
        schema="onboarding",
    )
    op.drop_table("source_datasets", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_migration_runs_status"),
        table_name="migration_runs",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_migration_runs_project_id"),
        table_name="migration_runs",
        schema="onboarding",
    )
    op.drop_index(
        op.f("ix_onboarding_migration_runs_kind"), table_name="migration_runs", schema="onboarding"
    )
    op.drop_table("migration_runs", schema="onboarding")
    op.drop_index(
        op.f("ix_onboarding_llm_calls_project_id"), table_name="llm_calls", schema="onboarding"
    )
    op.drop_index(
        op.f("ix_onboarding_llm_calls_input_hash"), table_name="llm_calls", schema="onboarding"
    )
    op.drop_table("llm_calls", schema="onboarding")
    op.drop_table("organizations", schema="target")
    op.drop_table("projects", schema="onboarding")
