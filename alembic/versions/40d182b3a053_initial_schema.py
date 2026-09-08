"""initial_schema

Revision ID: 40d182b3a053
Revises: 
Create Date: 2026-05-27 17:15:13.264775

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '40d182b3a053'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure postgres extension for pg_trgm is enabled
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    _historical_metadata().create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    pass


def _historical_metadata():
    """Frozen pre-Alembic schema (150538a); never import current ORM models."""
    metadata = sa.MetaData()
    sa.Table('users', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('email', sa.String(100), unique=True, index=True, nullable=False),
        sa.Column('username', sa.String(100), nullable=True),
        sa.Column('picture', sa.String(500), nullable=True),
        sa.Column('google_id', sa.String(100), unique=True, index=True, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.Column('priority_points', sa.Integer),
        sa.Column('terms_agreed', sa.Boolean),
        sa.Column('pro_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pro_level', sa.String(20)),
        sa.Column('paypal_subscription_id', sa.String(100), unique=True, index=True, nullable=True),
        sa.Column('paypal_subscription_status', sa.String(50), nullable=True),
    )
    sa.Table('generation_logs', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('prompt', sa.String(500), nullable=True),
        sa.Column('name', sa.String(100), nullable=True),
        sa.Column('mode', sa.String(50), nullable=False),
        sa.Column('source', sa.String(500), nullable=True),
        sa.Column('result', sa.String(500), nullable=True),
        sa.Column('edited_result', sa.String(500), nullable=True),
        sa.Column('edit_source_type', sa.String(50), nullable=True),
        sa.Column('user_id', sa.String(16), index=True, nullable=True),
        sa.Column('is_public', sa.Boolean, index=True),
        sa.Column('likes_count', sa.Integer),
        sa.Column('model_version', sa.String(50), nullable=True),
        sa.Column('parent', sa.String(16), index=True, nullable=True),
        sa.Column('seed', sa.Integer, nullable=True),
        sa.Column('n_step', sa.Integer, nullable=True),
        sa.Column('guidance', sa.Float, nullable=True),
        sa.Column('status', sa.String(20), index=True),
        sa.Column('error_msg', sa.Text, nullable=True),
        sa.Column('is_deleted', sa.Boolean, index=True),
        sa.Column('is_pro', sa.Boolean, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True), index=True),
        sa.Index('idx_gen_log_public_created', 'is_public', 'created_at'),
        sa.Index('idx_gen_log_name_trgm', 'name', postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'}),
    )
    sa.Table('collections', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('user_id', sa.String(16), index=True, nullable=False),
        sa.Column('is_public', sa.Boolean, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    sa.Table('collection_items', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('collection_id', sa.String(16), index=True, nullable=False),
        sa.Column('type', sa.String(50)),
        sa.Column('log_id', sa.String(16), index=True, nullable=True),
        sa.Column('data', sa.JSON, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True)),
    )
    sa.Table('user_likes', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('user_id', sa.String(16), index=True, nullable=False),
        sa.Column('log_id', sa.String(16), index=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Index('idx_user_like_user_log', 'user_id', 'log_id'),
    )
    sa.Table('shipping_addresses', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('user_id', sa.String(16), index=True, nullable=False),
        sa.Column('country', sa.String(100), nullable=False),
        sa.Column('phone', sa.String(50), nullable=False),
        sa.Column('zip_code', sa.String(20), nullable=False),
        sa.Column('state', sa.String(100), nullable=False),
        sa.Column('city', sa.String(100), nullable=False),
        sa.Column('detail_address', sa.Text, nullable=False),
        sa.Column('is_default', sa.Boolean),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    sa.Table('orders', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('user_id', sa.String(16), index=True, nullable=False),
        sa.Column('address_id', sa.String(16), index=True, nullable=True),
        sa.Column('order_type', sa.String(20)),
        sa.Column('status', sa.String(50), index=True),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('shipping_fee', sa.Float, nullable=False),
        sa.Column('total_price', sa.Float, nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paypal_order_id', sa.String(100), unique=True, nullable=True, index=True),
        sa.Column('goods_status', sa.String(50), nullable=True, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True), index=True),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    sa.Table('order_items', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('order_id', sa.String(16), index=True, nullable=False),
        sa.Column('skin_url', sa.String(255), nullable=True),
        sa.Column('model_type', sa.String(100), nullable=False),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('refer_log_id', sa.String(16), nullable=True, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True)),
    )
    sa.Table('model_sales_limits', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('model_type', sa.String(100), unique=True, index=True, nullable=False),
        sa.Column('order_type', sa.String(20), nullable=False),
        sa.Column('stock', sa.Integer),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    sa.Table('user_feedbacks', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('user_id', sa.String(16), index=True, nullable=True),
        sa.Column('log_id', sa.String(16), index=True, nullable=False),
        sa.Column('is_good', sa.Boolean, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True)),
    )
    sa.Table('external_ledger_entries', metadata,
        sa.Column('id', sa.String(220), primary_key=True, index=True),
        sa.Column('provider', sa.String(50), nullable=False, index=True),
        sa.Column('provider_account', sa.String(120), nullable=True),
        sa.Column('external_id', sa.String(180), nullable=False, index=True),
        sa.Column('entry_type', sa.String(50), nullable=False, index=True),
        sa.Column('category', sa.String(50), nullable=True, index=True),
        sa.Column('amount', sa.Float, nullable=False),
        sa.Column('gross_amount', sa.Float, nullable=True),
        sa.Column('fee_amount', sa.Float, nullable=True),
        sa.Column('net_amount', sa.Float, nullable=True),
        sa.Column('currency', sa.String(10)),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('public_description', sa.String(500), nullable=True),
        sa.Column('status', sa.String(50), index=True),
        sa.Column('source', sa.String(120), nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_payload', sa.JSON, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('provider', 'external_id', name='uq_external_ledger_provider_external_id'),
        sa.Index('idx_external_ledger_provider_posted', 'provider', 'posted_at'),
        sa.Index('idx_external_ledger_type_posted', 'entry_type', 'posted_at'),
    )
    sa.Table('ledger_sync_runs', metadata,
        sa.Column('id', sa.String(16), primary_key=True, index=True),
        sa.Column('provider', sa.String(50), nullable=False, index=True),
        sa.Column('source', sa.String(120), nullable=False),
        sa.Column('status', sa.String(50), nullable=False, index=True),
        sa.Column('range_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('range_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('records_inserted', sa.Integer),
        sa.Column('records_updated', sa.Integer),
        sa.Column('error_message', sa.Text, nullable=True),
        sa.Column('metadata_json', sa.JSON, nullable=True),
    )
    return metadata
