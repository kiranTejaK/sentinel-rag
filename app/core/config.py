from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SentinelRAG Core Application Settings.
    Strictly validated via Pydantic v2 BaseSettings.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Server & Environment
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = Field(
        default="development", description="Current operating environment"
    )
    DEBUG: bool = Field(default=False, description="Debug mode flag")
    HOST: str = Field(default="0.0.0.0", description="Bind host")
    PORT: int = Field(default=8000, description="Bind port")
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Log verbosity level"
    )
    APP_SECRET: str = Field(
        default="dev-insecure-secret-key-change-in-production-32bytes",
        min_length=16,
        description="Application secret key for signing tokens and state",
    )

    # Database Settings (PostgreSQL 16 + pgvector)
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://sentinel:sentinel_secret@localhost:5432/sentinel_db",
        description="Asyncpg database connection string",
    )
    DB_POOL_SIZE: int = Field(
        default=20, ge=5, le=100, description="Max steady-state connection pool size"
    )
    DB_MAX_OVERFLOW: int = Field(
        default=10, ge=0, le=50, description="Max burst connection overflow"
    )
    DB_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=120, description="Query execution timeout")

    # Redis Settings (Semantic Cache & Rate Limiting)
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL",
    )
    REDIS_POOL_SIZE: int = Field(
        default=50, ge=10, le=200, description="Redis connection pool limit"
    )
    REDIS_TIMEOUT_MS: int = Field(
        default=500, ge=50, le=5000, description="Redis network timeout in ms"
    )

    # Semantic Cache Configuration
    SEMANTIC_CACHE_THRESHOLD: float = Field(
        default=0.08, ge=0.01, le=0.50, description="Cosine distance threshold for semantic hit"
    )
    SEMANTIC_CACHE_TTL_SECONDS: int = Field(
        default=86400, ge=60, description="Default TTL for cached generations (24h)"
    )
    EMBEDDING_MODEL: str = Field(
        default="text-embedding-3-small", description="Embedding model name"
    )
    EMBEDDING_DIMENSION: int = Field(
        default=1536, ge=128, le=4096, description="Embedding vector size"
    )

    # Rate Limiting Defaults
    RATE_LIMIT_DEFAULT_CAPACITY: int = Field(
        default=100, ge=1, description="Burst bucket token capacity"
    )
    RATE_LIMIT_DEFAULT_REFILL_RATE_PER_SEC: int = Field(
        default=10, ge=1, description="Refill tokens per second"
    )

    # LLM Router Defaults (LiteLLM)
    DEFAULT_PRIMARY_MODEL: str = Field(
        default="groq/llama-3.3-70b-versatile",
        description="Primary upstream model identifier (ultra-low latency)",
    )
    DEFAULT_FALLBACK_MODEL_1: str = Field(
        default="openai/gpt-4o-mini", description="Secondary fallback model"
    )
    DEFAULT_FALLBACK_MODEL_2: str = Field(
        default="mistral/mistral-small-latest", description="Tertiary fallback model"
    )

    # Provider API Keys
    OPENAI_API_KEY: str | None = Field(default=None, description="OpenAI API Key")
    ANTHROPIC_API_KEY: str | None = Field(default=None, description="Anthropic API Key")
    GROQ_API_KEY: str | None = Field(default=None, description="Groq API Key")
    MISTRAL_API_KEY: str | None = Field(default=None, description="Mistral API Key")

    # Circuit Breaker Config
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Consecutive errors to trip breaker (5 consecutive failures)",
    )
    CIRCUIT_BREAKER_RECOVERY_TIME_SECONDS: int = Field(
        default=60,
        ge=5,
        le=300,
        description="Cool-down duration before half-open probe (60s recovery)",
    )
    CIRCUIT_BREAKER_TIMEOUT_SECONDS: float = Field(
        default=4.0, ge=0.5, le=60.0, description="Single LLM request timeout"
    )

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_asyncpg_protocol(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                f"DATABASE_URL must start with 'postgresql+asyncpg://', got '{v.split('://')[0]}://...'"
            )
        return v

    @field_validator("REDIS_URL")
    @classmethod
    def validate_redis_protocol(cls, v: str) -> str:
        if not (v.startswith("redis://") or v.startswith("rediss://")):
            raise ValueError("REDIS_URL must begin with 'redis://' or 'rediss://'")
        return v


settings = Settings()
