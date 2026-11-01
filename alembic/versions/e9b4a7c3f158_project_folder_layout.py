"""project folder layout: per-project meeting digest + optional folder_path

folder_path becomes optional because a project can now live under the shared
app_settings['projects_root'] instead of an explicitly chosen directory.

Revision ID: e9b4a7c3f158
Revises: d1f6c8b4e207
Create Date: 2026-08-16 17:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e9b4a7c3f158'
down_revision: str | None = 'd1f6c8b4e207'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('project_meetings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('digest', sa.Text(), nullable=True))
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('folder_path', existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE projects SET folder_path = '' WHERE folder_path IS NULL")
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('folder_path', existing_type=sa.Text(), nullable=False)
    with op.batch_alter_table('project_meetings', schema=None) as batch_op:
        batch_op.drop_column('digest')
