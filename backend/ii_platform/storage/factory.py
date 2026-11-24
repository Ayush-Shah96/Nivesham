from pathlib import Path
import shutil

from infrastructure.config.settings import get_settings
from .object_storage import ObjectStorage
from .s3_adapter import S3ObjectStorage


class LocalObjectStorage(ObjectStorage):
    def __init__(self, root):
        self.root = Path(root)

    async def put(self, key, data, content_type="application/octet-stream"):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(key)

    async def put_file(self, key, file_path, content_type="application/octet-stream"):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        await __import__("asyncio").to_thread(shutil.copyfile, file_path, p)
        return str(key)

    async def get(self, key):
        return (self.root / key).read_bytes()

    async def delete(self, key):
        p = self.root / key
        if p.exists():
            p.unlink()


def get_object_storage():
    s = get_settings()
    return S3ObjectStorage() if s.storage_type == "s3" else LocalObjectStorage(s.storage_path)
