"""add_forum_models

Revision ID: 8d5fb3684bc5
Revises: 40d182b3a053
Create Date: 2026-06-08 15:04:33.481638

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d5fb3684bc5'
down_revision: Union[str, None] = '40d182b3a053'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _index_or_constraint_exists(table_name: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    index_names = {idx["name"] for idx in inspector.get_indexes(table_name)}
    constraint_names = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint["name"]
    }
    return name in index_names or name in constraint_names


def _create_index_if_missing(table_name: str, name: str, columns: list[str], unique: bool = False) -> None:
    if not _index_or_constraint_exists(table_name, name):
        op.create_index(name, table_name, columns, unique=unique)


def upgrade() -> None:
    # Keep this migration safe for legacy databases where some forum tables
    # may already have been created by ORM startup before Alembic owned schema.
    if not _table_exists('forum_comments'):
        op.create_table('forum_comments',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column('post_id', sa.String(length=16), nullable=False),
        sa.Column('parent_id', sa.String(length=16), nullable=True),
        sa.Column('user_id', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
    _create_index_if_missing('forum_comments', 'ix_forum_comments_created_at', ['created_at'])
    _create_index_if_missing('forum_comments', 'ix_forum_comments_id', ['id'])
    _create_index_if_missing('forum_comments', 'ix_forum_comments_parent_id', ['parent_id'])
    _create_index_if_missing('forum_comments', 'ix_forum_comments_post_id', ['post_id'])
    _create_index_if_missing('forum_comments', 'ix_forum_comments_user_id', ['user_id'])

    if not _table_exists('forum_notifications'):
        op.create_table('forum_notifications',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column('user_id', sa.String(length=16), nullable=False),
        sa.Column('sender_id', sa.String(length=16), nullable=True),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('post_id', sa.String(length=16), nullable=True),
        sa.Column('comment_id', sa.String(length=16), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_comment_id', ['comment_id'])
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_created_at', ['created_at'])
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_id', ['id'])
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_is_read', ['is_read'])
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_post_id', ['post_id'])
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_sender_id', ['sender_id'])
    _create_index_if_missing('forum_notifications', 'ix_forum_notifications_user_id', ['user_id'])

    if not _table_exists('forum_post_likes'):
        op.create_table('forum_post_likes',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column('user_id', sa.String(length=16), nullable=False),
        sa.Column('post_id', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'post_id', name='uq_forum_post_like_user_post')
        )
    _create_index_if_missing('forum_post_likes', 'ix_forum_post_likes_id', ['id'])
    _create_index_if_missing('forum_post_likes', 'ix_forum_post_likes_post_id', ['post_id'])
    _create_index_if_missing('forum_post_likes', 'ix_forum_post_likes_user_id', ['user_id'])
    _create_index_if_missing('forum_post_likes', 'uq_forum_post_like_user_post', ['user_id', 'post_id'], unique=True)

    if not _table_exists('forum_posts'):
        op.create_table('forum_posts',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=True),
        sa.Column('image', sa.String(length=500), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.String(length=16), nullable=False),
        sa.Column('likes_count', sa.Integer(), nullable=True),
        sa.Column('views_count', sa.Integer(), nullable=True),
        sa.Column('body_type', sa.String(length=50), nullable=True),
        sa.Column('multi_color_type', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
    _create_index_if_missing('forum_posts', 'ix_forum_posts_category', ['category'])
    _create_index_if_missing('forum_posts', 'ix_forum_posts_created_at', ['created_at'])
    _create_index_if_missing('forum_posts', 'ix_forum_posts_id', ['id'])
    _create_index_if_missing('forum_posts', 'ix_forum_posts_user_id', ['user_id'])


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_forum_posts_user_id'), table_name='forum_posts')
    op.drop_index(op.f('ix_forum_posts_id'), table_name='forum_posts')
    op.drop_index(op.f('ix_forum_posts_created_at'), table_name='forum_posts')
    op.drop_index(op.f('ix_forum_posts_category'), table_name='forum_posts')
    op.drop_table('forum_posts')
    op.drop_index(op.f('ix_forum_post_likes_user_id'), table_name='forum_post_likes')
    op.drop_index(op.f('ix_forum_post_likes_post_id'), table_name='forum_post_likes')
    op.drop_index(op.f('ix_forum_post_likes_id'), table_name='forum_post_likes')
    op.drop_table('forum_post_likes')
    op.drop_index(op.f('ix_forum_notifications_user_id'), table_name='forum_notifications')
    op.drop_index(op.f('ix_forum_notifications_sender_id'), table_name='forum_notifications')
    op.drop_index(op.f('ix_forum_notifications_post_id'), table_name='forum_notifications')
    op.drop_index(op.f('ix_forum_notifications_is_read'), table_name='forum_notifications')
    op.drop_index(op.f('ix_forum_notifications_id'), table_name='forum_notifications')
    op.drop_index(op.f('ix_forum_notifications_created_at'), table_name='forum_notifications')
    op.drop_index(op.f('ix_forum_notifications_comment_id'), table_name='forum_notifications')
    op.drop_table('forum_notifications')
    op.drop_index(op.f('ix_forum_comments_user_id'), table_name='forum_comments')
    op.drop_index(op.f('ix_forum_comments_post_id'), table_name='forum_comments')
    op.drop_index(op.f('ix_forum_comments_parent_id'), table_name='forum_comments')
    op.drop_index(op.f('ix_forum_comments_id'), table_name='forum_comments')
    op.drop_index(op.f('ix_forum_comments_created_at'), table_name='forum_comments')
    op.drop_table('forum_comments')
    # ### end Alembic commands ###
