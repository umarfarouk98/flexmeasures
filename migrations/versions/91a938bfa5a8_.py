"""empty message

Revision ID: 91a938bfa5a8
Revises: 4b6cebbdf473
Create Date: 2018-06-08 15:46:28.950464

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "91a938bfa5a8"
down_revision = "4b6cebbdf473"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("power", sa.Column("horizon", sa.String(length=6), nullable=False))
    op.add_column("price", sa.Column("horizon", sa.String(length=6), nullable=False))
    op.add_column("weather", sa.Column("horizon", sa.String(length=6), nullable=False))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("weather", "horizon")
    op.drop_column("price", "horizon")
    op.drop_column("power", "horizon")
    # ### end Alembic commands ###
