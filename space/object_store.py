"""Space Market objects live beside the standalone PostgreSQL volume."""
import os
from pathlib import Path
import re
import tempfile
from config import settings


def object_path(key):
    if not re.fullmatch(r"space-market/resources/[A-Za-z0-9_./-]+", key) or ".." in key.split("/"):
        raise ValueError("Invalid Space object key")
    root = Path(settings.SPACE_OBJECT_DIR).resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid Space object path")
    return path


def get_cdn_url(key):
    return settings.SPACE_PUBLIC_API_URL.rstrip("/") + "/space/objects/" + key


def upload_to_s3(file_content, key, is_public=True, content_type=None):
    path = object_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            name = output.name
            output.write(file_content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    return get_cdn_url(key)


def download_from_s3(key, is_public=True):
    return object_path(key).read_bytes()


def delete_from_s3_strict(key, is_public=True):
    object_path(key).unlink(missing_ok=True)


def invalidate_cdn_object(key):
    # These responses are deliberately never cached by the CDN.
    return False
