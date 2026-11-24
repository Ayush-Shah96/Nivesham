from ii_platform.web.provider_base import DataProvider
class VendorProvider(DataProvider):
    name="authorized-vendor"
    def __init__(self,client): self.client=client
    async def fetch(self,*args,**kwargs): return await self.client.fetch(*args,**kwargs)
