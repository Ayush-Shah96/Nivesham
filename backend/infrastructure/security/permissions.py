from fastapi import Depends, HTTPException
from infrastructure.security.auth import get_current_user
from ii_platform.database.models import User, UserRole

def require_role(*roles:UserRole):
    allowed=set(roles)
    async def guard(user:User=Depends(get_current_user)):
        if user.role not in allowed: raise HTTPException(status_code=403,detail="Insufficient role")
        return user
    return guard
