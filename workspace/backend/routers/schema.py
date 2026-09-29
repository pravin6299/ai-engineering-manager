from fastapi import APIRouter
from ..models import Base

router = APIRouter(prefix="/schema", tags=["Schema"])

@router.get("/tables")
async def list_tables():
    """Return a list of table names defined in the SQLAlchemy metadata."""
    return {"tables": list(Base.metadata.tables.keys())}
