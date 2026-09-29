"""SentinelRAG API v1 router aggregator."""

from fastapi import APIRouter

from app.api.v1.documents import router as documents_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(documents_router)

__all__ = ["api_v1_router"]
