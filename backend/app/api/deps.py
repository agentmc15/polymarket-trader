"""API dependencies."""
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session

# Type alias for database session dependency
AsyncSessionDep = Annotated[AsyncSession, Depends(get_async_session)]
