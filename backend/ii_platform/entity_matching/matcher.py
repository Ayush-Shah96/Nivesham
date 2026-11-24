from sqlalchemy import or_,select
from sqlalchemy.ext.asyncio import AsyncSession
from ii_platform.database.models import Company
class EntityMatcher:
    def __init__(self,db:AsyncSession): self.db=db
    async def match(self,value):
        q=value.strip()
        stmt=select(Company).where(or_(Company.isin==q,Company.nse_symbol==q.upper(),Company.bse_code==q,Company.legal_name.ilike(q)))
        c=(await self.db.execute(stmt)).scalar_one_or_none()
        if c:return c
        for c in (await self.db.execute(select(Company))).scalars().all():
            if q.lower() in {str(x).lower() for x in (c.aliases or [])}: return c
        return None
