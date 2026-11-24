import asyncio
import boto3

from .object_storage import ObjectStorage
from infrastructure.config.settings import get_settings


class S3ObjectStorage(ObjectStorage):
    def __init__(self):
        s = get_settings()
        if not s.aws_s3_bucket:
            raise RuntimeError("AWS_S3_BUCKET required")
        self.bucket = s.aws_s3_bucket
        self.client = boto3.client(
            "s3",
            region_name=s.aws_s3_region,
            endpoint_url=s.s3_endpoint_url,
            aws_access_key_id=s.aws_access_key_id,
            aws_secret_access_key=s.aws_secret_access_key,
        )

    async def put(self, key, data, content_type="application/octet-stream"):
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    async def put_file(self, key, file_path, content_type="application/octet-stream"):
        await asyncio.to_thread(
            self.client.upload_file,
            str(file_path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return key

    async def get(self, key):
        obj = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def delete(self, key):
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
