"""Initial schema: every table in icp_chatbot.store.models.

The first revision creates the tables from the model metadata. From the second revision on, write
explicit operations (op.add_column and so on) so old revisions stay reproducible.

Revision ID: 0001
Revises:
"""

from alembic import op

from icp_chatbot.store.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
