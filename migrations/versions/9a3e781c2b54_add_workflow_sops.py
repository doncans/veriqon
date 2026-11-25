"""add tenant-scoped workflow SOPs

Revision ID: 9a3e781c2b54
Revises: 4c3945d9946a
Create Date: 2026-11-20 18:47:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a3e781c2b54"
down_revision: Union[str, Sequence[str], None] = "4c3945d9946a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_sops",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_api_key_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.String(length=20000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_workflow_sops_tenant_actor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            name="fk_workflow_sops_tenant_workflow",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "version",
            name="uq_workflow_sop_tenant_workflow_version",
        ),
    )
    op.create_index(op.f("ix_workflow_sops_tenant_id"), "workflow_sops", ["tenant_id"])
    op.create_index(op.f("ix_workflow_sops_workflow_id"), "workflow_sops", ["workflow_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_workflow_sops_workflow_id"), table_name="workflow_sops")
    op.drop_index(op.f("ix_workflow_sops_tenant_id"), table_name="workflow_sops")
    op.drop_table("workflow_sops")
