"""SentinelRAG API v1 router aggregator."""

from fastapi import APIRouter

from app.api.v1.cache import router as cache_router
from app.api.v1.chat import router as chat_router
from app.api.v1.documents import router as documents_router
from app.api.v1.search import router as search_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(documents_router)
api_v1_router.include_router(search_router)
api_v1_router.include_router(cache_router)
api_v1_router.include_router(chat_router)

__all__ = ["api_v1_router"]

