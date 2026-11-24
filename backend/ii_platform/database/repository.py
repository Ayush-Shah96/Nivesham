from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

async def get_by_id(session: AsyncSession, model, object_id: int):
    return await session.get(model, object_id)

async def list_recent(session: AsyncSession, model, order_column, limit: int = 100):
    return (await session.execute(select(model).order_by(order_column.desc()).limit(limit))).scalars().all()

async def add_and_flush(session: AsyncSession, obj):
    session.add(obj)
    await session.flush()
    return obj
