"""Add Sign in with ChatGPT credentials, billing source, and job error codes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'c3b9e1f0a5d2'
down_revision: str | None = 'a17c42d89e30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('model_display_name', sa.String(128), nullable=True))
    op.add_column('jobs', sa.Column('billing_source', sa.String(16), nullable=True))
    op.add_column('jobs', sa.Column('error_code', sa.String(64), nullable=True))

    op.create_table(
        'chatgpt_credentials',
        sa.Column('user_id', sa.String(128), primary_key=True),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('email', sa.String(320), nullable=True),
        sa.Column('client_id', sa.String(255), nullable=False),
        sa.Column('ext_agent_host_id', sa.String(255), nullable=True),
        sa.Column('scopes', sa.Text(), nullable=False, server_default=''),
        sa.Column('access_token_enc', sa.Text(), nullable=False),
        sa.Column('refresh_token_enc', sa.Text(), nullable=True),
        sa.Column('id_token_enc', sa.Text(), nullable=True),
        sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('earliest_refresh_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('needs_reauth', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('value', sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
    op.drop_table('chatgpt_credentials')
    op.drop_column('jobs', 'error_code')
    op.drop_column('jobs', 'billing_source')
    op.drop_column('jobs', 'model_display_name')
