from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_add_contract_type_to_symbols"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("symbols", sa.Column("contract_type", sa.Text()))


def downgrade() -> None:
    op.drop_column("symbols", "contract_type")
