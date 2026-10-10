"""FastAPI router for Unified Chat Completions API.

Pipeline:
1. Cache Evaluation: Sub-20ms exact/semantic lookup via RedisSemanticCache.
2. Context Retrieval: Hybrid Dense (pgvector HNSW) + Sparse (PostgreSQL FTS) merged via RRF.
3. Resilient Routing: Primary Groq -> Fallback OpenAI/Mistral via Circuit Breaker & Tenacity.
4. Token & Cost Telemetry: tiktoken token counting, per-request cost, and cumulative dollars saved.
"""

import time
import uuid
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.core.redis import get_redis_client
from app.db.session import get_async_session
from app.repositories.search import SearchRepository
from app.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatTelemetry,
    ChatUsage,
)
from app.services.cache import RedisSemanticCache
from app.services.router import ResilientLLMRouter, get_llm_router
from app.services.rrf import fuse_hybrid_candidates
from app.services.token_analytics import (
    count_messages_tokens,
    count_text_tokens,
)

router = APIRouter(prefix="/chat", tags=["Chat Completions"])


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
    status_code=status.HTTP_200_OK,
    summary="Unified Resilient Chat Completions Gateway",
    description=(
        "Orchestrates enterprise RAG request pipeline: "
        "1. Checks Redis Semantic Cache for sub-20ms hits. "
        "2. Executes hybrid vector & full-text search with RRF fusion (if retrieval enabled). "
        "3. Routes request via LiteLLM with state-machine Circuit Breaking (Groq -> OpenAI/Mistral). "
        "4. Tracks token usage and cumulative dollars saved via tiktoken analytics."
    ),
)
async def chat_completions(
    payload: ChatCompletionRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> ChatCompletionResponse:
    """Unified endpoint tying together Cache -> Hybrid Search -> LLM Router -> Telemetry."""
    start_time = time.perf_counter()
    completion_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"
    created_epoch = int(time.time())

    cache_service = RedisSemanticCache(redis)
    router_service: ResilientLLMRouter = get_llm_router(redis)

    # Extract primary user query text
    user_messages = [m for m in payload.messages if m.role == "user"]
    query_text = user_messages[-1].content if user_messages else ""

    # --------------------------------------------------------------------------
    # STAGE 1: SUB-20ms SEMANTIC & EXACT CACHE CHECK
    # --------------------------------------------------------------------------
    if payload.use_cache and query_text:
        cache_result = await cache_service.get_or_lookup(
            tenant_id=payload.tenant_id,
            query=query_text,
            query_vector=payload.query_vector,
        )

        if cache_result.status == "CACHE_HIT" and cache_result.cached_response:
            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

            # Compute tokens and dollars saved
            prompt_tokens = count_messages_tokens(
                [m.model_dump() for m in payload.messages], model_name="gpt-4o-mini"
            )
            completion_tokens = count_text_tokens(
                cache_result.cached_response, model_name="gpt-4o-mini"
            )
            total_tokens = prompt_tokens + completion_tokens

            dollars_saved = await router_service.analytics.record_cache_hit_savings(
                tenant_id=payload.tenant_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            cumulative_saved = await router_service.analytics.get_cumulative_dollars_saved(
                tenant_id=payload.tenant_id
            )

            logger.info(
                "chat_completion_served_from_cache",
                tenant_id=payload.tenant_id,
                query=query_text,
                matched_key=cache_result.matched_key,
                similarity=cache_result.similarity,
                dollars_saved=dollars_saved,
                latency_ms=latency_ms,
            )

            return ChatCompletionResponse(
                id=completion_id,
                object="chat.completion",
                created=created_epoch,
                model="sentinel-semantic-cache",
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ChatMessage(
                            role="assistant", content=cache_result.cached_response
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=ChatUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ),
                telemetry=ChatTelemetry(
                    cache_hit=True,
                    provider="semantic_cache",
                    model="cache",
                    circuit_breaker_state=router_service.circuit_breaker.state.value,
                    fallback_triggered=False,
                    fallback_reason=None,
                    cost_usd=0.0,
                    dollars_saved=dollars_saved,
                    cumulative_dollars_saved=cumulative_saved,
                    retrieval_chunks_used=0,
                    latency_ms=latency_ms,
                ),
            )

    # --------------------------------------------------------------------------
    # STAGE 2: HYBRID SEARCH CONTEXT RETRIEVAL (IF ENABLED)
    # --------------------------------------------------------------------------
    retrieval_chunks_count = 0
    augmented_messages: list[dict[str, str]] = [
        {"role": m.role, "content": m.content} for m in payload.messages
    ]

    if payload.use_retrieval and query_text:
        try:
            search_repo = SearchRepository(session)
            dense_candidates, sparse_candidates = await search_repo.search_hybrid_candidates(
                tenant_id=payload.tenant_id,
                query_text=query_text,
                query_vector=payload.query_vector or [0.0] * 1536,
                dense_limit=payload.top_k_chunks * 2,
                sparse_limit=payload.top_k_chunks * 2,
            )

            fused_chunks = fuse_hybrid_candidates(
                dense_candidates=dense_candidates,
                sparse_candidates=sparse_candidates,
                k=60,
                top_k=payload.top_k_chunks,
            )

            if fused_chunks:
                retrieval_chunks_count = len(fused_chunks)
                context_snippets = [
                    f"[{i + 1}] Source: {chunk.chunk.document_name}\n{chunk.chunk.chunk_text}"
                    for i, chunk in enumerate(fused_chunks)
                ]
                context_block = (
                    "Context information retrieved from verified enterprise documents:\n\n"
                    + "\n\n".join(context_snippets)
                )

                # Prepend context to system message or insert as system instruction
                augmented_messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": (
                            "You are SentinelRAG's verified AI assistant. Use the following "
                            f"document context to answer the user query accurately:\n\n{context_block}"
                        ),
                    },
                )
                logger.info(
                    "hybrid_retrieval_augmented_prompt",
                    tenant_id=payload.tenant_id,
                    chunks_fused=retrieval_chunks_count,
                )
        except Exception as search_err:
            logger.warning(
                "hybrid_retrieval_degraded_continuing_unaugmented",
                tenant_id=payload.tenant_id,
                error=str(search_err),
            )

    # --------------------------------------------------------------------------
    # STAGE 3: RESILIENT MULTI-PROVIDER LLM ROUTING
    # --------------------------------------------------------------------------
    router_result = await router_service.complete(
        messages=augmented_messages,
        tenant_id=payload.tenant_id,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        preferred_model=payload.model,
    )

    # --------------------------------------------------------------------------
    # STAGE 4: WRITE TO SEMANTIC CACHE FOR SUBSEQUENT QUERIES
    # --------------------------------------------------------------------------
    if payload.use_cache and query_text and router_result.content:
        try:
            await cache_service.set(
                tenant_id=payload.tenant_id,
                query=query_text,
                query_vector=payload.query_vector,
                response=router_result.content,
                metadata={
                    "model": router_result.model_used,
                    "provider": router_result.provider,
                    "cost_usd": router_result.cost_usd,
                },
            )
        except Exception as cache_err:
            logger.warning("cache_write_failed_continuing", error=str(cache_err))

    # Calculate cumulative metrics for telemetry response
    cumulative_saved = await router_service.analytics.get_cumulative_dollars_saved(
        tenant_id=payload.tenant_id
    )
    total_latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

    return ChatCompletionResponse(
        id=completion_id,
        object="chat.completion",
        created=created_epoch,
        model=router_result.model_used,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=router_result.content),
                finish_reason="stop",
            )
        ],
        usage=ChatUsage(
            prompt_tokens=router_result.prompt_tokens,
            completion_tokens=router_result.completion_tokens,
            total_tokens=router_result.total_tokens,
        ),
        telemetry=ChatTelemetry(
            cache_hit=False,
            provider=router_result.provider,
            model=router_result.model_used,
            circuit_breaker_state=router_result.circuit_breaker_state,
            fallback_triggered=router_result.fallback_triggered,
            fallback_reason=router_result.fallback_reason,
            cost_usd=router_result.cost_usd,
            dollars_saved=0.0,
            cumulative_dollars_saved=cumulative_saved,
            retrieval_chunks_used=retrieval_chunks_count,
            latency_ms=total_latency_ms,
        ),
    )
