"""Pydantic schemas for Unified Chat Completions API with Resilience & Telemetry."""

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """Single message in a conversational thread."""

    role: Literal["system", "user", "assistant"] = Field(
        ..., description="Message author role"
    )
    content: str = Field(..., min_length=1, description="Message text payload")


class ChatCompletionRequest(BaseModel):
    """Inbound request for unified chat completion."""

    messages: list[ChatMessage] = Field(
        ..., min_length=1, description="Conversation history leading up to completion"
    )
    tenant_id: str = Field(
        default="default", min_length=1, max_length=128, description="Tenant isolation boundary"
    )
    model: str | None = Field(
        default=None, description="Explicit model selection; defaults to primary router model"
    )
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="Sampling temperature"
    )
    max_tokens: int = Field(
        default=1024, ge=1, le=8192, description="Maximum completion tokens to generate"
    )
    use_cache: bool = Field(
        default=True, description="Enable sub-20ms exact and semantic cache check"
    )
    use_retrieval: bool = Field(
        default=False, description="Augment prompt with hybrid dense/sparse search chunks"
    )
    query_vector: list[float] | None = Field(
        default=None, description="Optional 1536-dim embedding for semantic cache & vector search"
    )
    top_k_chunks: int = Field(
        default=3, ge=1, le=20, description="Number of context chunks to fuse into prompt"
    )


class ChatCompletionChoice(BaseModel):
    """Completion candidate item."""

    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatUsage(BaseModel):
    """Token consumption breakdown."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatTelemetry(BaseModel):
    """High-observability SentinelRAG operational telemetry."""

    cache_hit: bool
    provider: str
    model: str
    circuit_breaker_state: str
    fallback_triggered: bool
    fallback_reason: str | None = None
    cost_usd: float
    dollars_saved: float
    cumulative_dollars_saved: float
    retrieval_chunks_used: int = 0
    latency_ms: float


class ChatCompletionResponse(BaseModel):
    """RFC-aligned OpenAI-compatible chat completion payload with Sentinel telemetry."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatUsage
    telemetry: ChatTelemetry
