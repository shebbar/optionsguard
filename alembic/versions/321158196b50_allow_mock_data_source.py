"""allow mock data_source

Revision ID: 321158196b50
Revises: 7cdbeec0dbb7
Create Date: 2026-10-15 12:21:10.613154

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '321158196b50'
down_revision: Union[str, Sequence[str], None] = '7cdbeec0dbb7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Alembic's autogenerate doesn't detect CHECK constraint changes -- written by hand.
    op.drop_constraint("ck_data_source", "checks", type_="check")
    op.create_check_constraint("ck_data_source", "checks", "data_source IN ('manual','mock','kite')")


def downgrade() -> None:
    op.drop_constraint("ck_data_source", "checks", type_="check")
    op.create_check_constraint("ck_data_source", "checks", "data_source IN ('manual','kite')")
