from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ii_platform.database.models import Company
class CompanyMaster:
    def __init__(self,db:AsyncSession): self.db=db
    async def upsert(self,**data):
        c=None
        if data.get("isin"): c=(await self.db.execute(select(Company).where(Company.isin==data["isin"]))).scalar_one_or_none()
        if not c and data.get("nse_symbol"): c=(await self.db.execute(select(Company).where(Company.nse_symbol==data["nse_symbol"]))).scalar_one_or_none()
        if not c: c=Company(**data); self.db.add(c)
        else:
            for k,v in data.items(): setattr(c,k,v)
        await self.db.flush(); return c
