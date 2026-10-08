"""
Health check endpoint.
"""

from fastapi import APIRouter
from sqlalchemy import text as sa_text

from pm_agent.app.db.session import get_session_factory

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """Health check endpoint — verifies app is running and DB is reachable."""
    db_status = "unknown"

    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            result = await session.execute(sa_text("SELECT 1"))
            result.scalar()
            db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "ok" if db_status == "connected" else "degraded",
        "db": db_status,
    }
