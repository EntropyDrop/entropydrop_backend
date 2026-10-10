"""Persist sticker data independently of skins and publisher profiles."""
from urllib.parse import quote
from alembic import op
import sqlalchemy as sa

revision = "d2f81a604bc9"
down_revision = "c7a31d902ef4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("order_items", sa.Column("sticker_snapshot", sa.JSON(), nullable=True))
    bind = op.get_bind()
    items = sa.table("order_items", sa.column("id", sa.String()), sa.column("model_type", sa.String()),
        sa.column("refer_log_id", sa.String()), sa.column("source_snapshot", sa.JSON()), sa.column("sticker_snapshot", sa.JSON()))
    logs = sa.table("generation_logs", sa.column("id", sa.String()), sa.column("name", sa.String()),
        sa.column("prompt", sa.String()), sa.column("user_id", sa.String()), sa.column("is_deleted", sa.Boolean()))
    users = sa.table("users", sa.column("id", sa.String()), sa.column("username", sa.String()))
    for item in bind.execute(sa.select(items).where(items.c.model_type == "Cute DIY Kit")).mappings().all():
        source = item["source_snapshot"] or {}
        skin_id = source.get("skin_id") or item["refer_log_id"] or ""
        log = bind.execute(sa.select(logs).where(logs.c.id == skin_id)).mappings().first() if skin_id else None
        if log and log["is_deleted"]:
            log = None
        missing = []
        if "name" in source and source["name"]:
            name = source["name"]
        elif log:
            name = log["name"] or (log["prompt"] or "")[:100] or "Untitled"
        else:
            name = ""
            missing.append("skin_name")
        publisher_id = source.get("publisher_id") or (log["user_id"] if log else "") or ""
        user = bind.execute(sa.select(users).where(users.c.id == publisher_id)).mappings().first() if publisher_id else None
        if "publisher_name" in source:
            publisher_name = source["publisher_name"] or "Unknown publisher"
        elif user:
            publisher_name = user["username"] or "Unknown publisher"
        else:
            publisher_name = ""
            missing.append("publisher_name")
        if not publisher_id:
            missing.append("publisher_id")
        if not skin_id:
            missing.append("skin_id")
        snapshot = dict(schema_version=1, origin="legacy_backfill", brand="EntropyDrop", model_name="CUTE-7cm",
            skin_id=skin_id, skin_name=name, publisher_id=publisher_id, publisher_name=publisher_name,
            source_url=f"https://entropydrop.com/skin/?id={quote(skin_id, safe='')}" if skin_id else "",
            labels=dict(publisher="Published by", user_id="User ID", source="Skin"), missing_fields=missing)
        bind.execute(items.update().where(items.c.id == item["id"]).values(sticker_snapshot=snapshot))


def downgrade():
    op.drop_column("order_items", "sticker_snapshot")
