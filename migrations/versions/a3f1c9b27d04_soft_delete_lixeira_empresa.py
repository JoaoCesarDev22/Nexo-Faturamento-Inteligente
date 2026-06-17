"""soft delete (lixeira) em empresa: coluna deletado_em

Revision ID: a3f1c9b27d04
Revises: d61b5e68082c
Create Date: 2026-06-16

Adiciona a coluna `deletado_em` (TIMESTAMP NULL) à tabela `empresa` para
suportar o Sistema de Lixeira (Soft Delete + Hard Delete). NULL = empresa
ativa; preenchido = movida para a lixeira. Coluna anulável e sem default,
portanto compatível com as linhas existentes (todas viram "ativas").
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a3f1c9b27d04"
down_revision = "d61b5e68082c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("empresa", sa.Column("deletado_em", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("empresa", "deletado_em")
