"""accounts.caldav_password_enc (separate CalDAV app-password)

Yandex scopes app-passwords per service, so the IMAP password is rejected by CalDAV.
This optional, Fernet-encrypted column holds a dedicated calendar password; NULL falls
back to the mailbox password.

Revision ID: a4c7e1f9d2b3
Revises: f3b8d5c2a9e1
Create Date: 2026-07-04 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'a4c7e1f9d2b3'
down_revision: str | None = 'f3b8d5c2a9e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('caldav_password_enc', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('caldav_password_enc')
