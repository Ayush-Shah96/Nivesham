class YouTubeCollector:
    def __init__(self,client): self.client=client
    async def fetch(self,channel_id): return await self.client.fetch(channel_id)
