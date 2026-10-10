"""Immutable sticker text assembled from trusted order sources."""
from urllib.parse import quote
import models
import schemas


def sticker_snapshot(db, log, model_type, language="en"):
    if model_type != "Cute DIY Kit":
        return None
    publisher = db.query(models.User).filter_by(id=log.user_id).first() if log.user_id else None
    return schemas.OrderStickerSnapshot(
        origin="order", brand="EntropyDrop", model_name="CUTE-7cm",
        skin_id=log.id, skin_name=log.name or (log.prompt or "")[:100] or "Untitled",
        publisher_id=log.user_id or "", publisher_name=(publisher.username or "Unknown publisher") if publisher else "Unknown publisher",
        source_url=f"https://entropydrop.com/skin/?id={quote(log.id, safe='')}",
        labels=schemas.StickerLabels(publisher="发布者", user_id="用户 ID", source="皮肤") if language == "zh-hans"
            else schemas.StickerLabels(publisher="Published by", user_id="User ID", source="Skin"),
    ).model_dump(mode="json")
