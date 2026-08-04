"""create target control plane schema

Revision ID: 20260804_0001
Revises:
Create Date: 2026-08-04 00:00:00.000000
"""

from alembic import op

from app.db.models import Base

revision = "20260804_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
