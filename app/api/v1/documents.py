"""Document Chunk Ingestion & Retrieval API Routes.
Provides high-throughput async batch ingestion for vectors and metadata.
"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.session import get_async_session
from app.models.document import DocumentChunk
from app.schemas.document import BatchIngestRequest, BatchIngestResponse

router = APIRouter(prefix="/documents", tags=["Document Ingestion"])


@router.post(
    "/batch",
    response_model=BatchIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Batch Ingest Document Chunks with Vectors & Metadata",
    description=(
        "Asynchronously bulk-inserts document chunks, dense vector embeddings, "
        "and JSONB metadata into PostgreSQL in a single atomic transaction. "
        "The generated full-text TSVector column is computed automatically on insert."
    ),
)
async def batch_ingest_documents(
    payload: BatchIngestRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> BatchIngestResponse:
    """Ingests a batch of document chunks with 1536-dimensional embeddings.

    Performs an atomic multi-row INSERT ... RETURNING id execution.
    """
    start_time = time.perf_counter()

    chunk_records = [
        {
            "tenant_id": payload.tenant_id,
            "document_name": chunk.document_name,
            "chunk_text": chunk.chunk_text,
            "metadata_json": chunk.metadata_json,
            "embedding": chunk.embedding,
        }
        for chunk in payload.chunks
    ]

    # Execute atomic bulk insert returning all generated UUIDs
    stmt = insert(DocumentChunk).returning(DocumentChunk.id)
    result = await session.scalars(stmt, chunk_records)
    chunk_ids = list(result.all())
    await session.commit()

    duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

    logger.info(
        "document_chunks_batch_ingested",
        tenant_id=payload.tenant_id,
        count=len(chunk_ids),
        duration_ms=duration_ms,
    )

    return BatchIngestResponse(
        status="success",
        tenant_id=payload.tenant_id,
        inserted_count=len(chunk_ids),
        chunk_ids=chunk_ids,
        duration_ms=duration_ms,
    )
