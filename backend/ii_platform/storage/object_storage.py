from abc import ABC, abstractmethod


class ObjectStorage(ABC):
    @abstractmethod
    async def put(self, key, data, content_type="application/octet-stream"):
        ...

    @abstractmethod
    async def put_file(self, key, file_path, content_type="application/octet-stream"):
        ...

    @abstractmethod
    async def get(self, key):
        ...

    @abstractmethod
    async def delete(self, key):
        ...
