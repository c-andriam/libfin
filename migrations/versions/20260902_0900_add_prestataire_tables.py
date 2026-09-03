"""add merchants and liquidity providers (prestataires)

Revision ID: f41c98b2d7e0
Revises: 5e1636c9c627
Created: 2026-09-02 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'f41c98b2d7e0'
down_revision = '5e1636c9c627'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'merchants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('legal_name', sa.String(length=128), nullable=False),
        sa.Column('email', sa.String(length=256), nullable=False),
        sa.Column('target_wallet', sa.String(length=42), nullable=False),
        sa.Column('api_key', sa.String(length=64), nullable=False),
        sa.Column(
            'status',
            sa.Enum('ACTIVE', 'INACTIVE', 'SUSPENDED', name='merchantstatus'),
            nullable=False,
        ),
        sa.Column('fee_rate', sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column('kyc_verified', sa.Boolean(), nullable=False),
        sa.Column('kyc_document_ref', sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('target_wallet'),
        sa.UniqueConstraint('api_key'),
    )
    op.create_index('ix_merchants_email', 'merchants', ['email'], unique=False)
    op.create_index('ix_merchants_id', 'merchants', ['id'], unique=False)
    op.create_index('ix_merchants_status', 'merchants', ['status'], unique=False)

    op.create_table(
        'liquidity_providers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('contact', sa.String(length=256), nullable=True),
        sa.Column('email', sa.String(length=256), nullable=False),
        sa.Column('source_wallet', sa.String(length=42), nullable=False),
        sa.Column('settlement_wallet', sa.String(length=42), nullable=False),
        sa.Column(
            'status',
            sa.Enum('ACTIVE', 'INACTIVE', name='liquidityproviderstatus'),
            nullable=False,
        ),
        sa.Column('daily_limit_units', sa.Numeric(precision=38, scale=0), nullable=True),
        sa.Column('settlement_token_symbol', sa.String(length=12), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('settlement_wallet'),
    )
    op.create_index('ix_liquidity_providers_id', 'liquidity_providers', ['id'], unique=False)
    op.create_index(
        'ix_liquidity_providers_source_wallet',
        'liquidity_providers',
        ['source_wallet'],
        unique=False,
    )
    op.create_index(
        'ix_liquidity_providers_status', 'liquidity_providers', ['status'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_liquidity_providers_status', table_name='liquidity_providers')
    op.drop_index('ix_liquidity_providers_source_wallet', table_name='liquidity_providers')
    op.drop_index('ix_liquidity_providers_id', table_name='liquidity_providers')
    op.drop_table('liquidity_providers')

    op.drop_index('ix_merchants_status', table_name='merchants')
    op.drop_index('ix_merchants_id', table_name='merchants')
    op.drop_index('ix_merchants_email', table_name='merchants')
    op.drop_table('merchants')

    op.execute("DROP TYPE IF EXISTS merchantstatus")
    op.execute("DROP TYPE IF EXISTS liquidityproviderstatus")
