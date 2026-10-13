"""Token and Cost Analytics Tracker using tiktoken.

Calculates:
- Exact token consumption (prompt and completion) via BPE tokenizers
- Granular cost per request based on provider/model pricing tables
- Cumulative "Dollars Saved" and token savings when queries are served from the Semantic Cache
- Persistent analytics tracking in Redis with high-concurrency atomic counters
"""

from typing import Any

import redis.asyncio as aioredis
import tiktoken

from app.core.logging import logger

# Model Pricing Table (USD per 1,000,000 tokens)
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Groq Llama Models (Ultra-low latency)
    "groq/llama-3.3-70b-versatile": {"prompt": 0.59, "completion": 0.79},
    "groq/llama3-70b-8192": {"prompt": 0.59, "completion": 0.79},
    "groq/llama-3.1-8b-instant": {"prompt": 0.05, "completion": 0.08},
    "groq/llama3-8b-8192": {"prompt": 0.05, "completion": 0.08},
    # OpenAI Models
    "openai/gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "openai/gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00},
    # Mistral Models
    "mistral/mistral-small-latest": {"prompt": 0.20, "completion": 0.60},
    "mistral/mistral-large-latest": {"prompt": 2.00, "completion": 6.00},
}

DEFAULT_PRICING: dict[str, float] = {"prompt": 0.15, "completion": 0.60}


def get_model_rates(model_name: str) -> dict[str, float]:
    """Resolves token rates for given model identifier with normalized fallback."""
    normalized = model_name.lower().strip()
    if normalized in MODEL_PRICING:
        return MODEL_PRICING[normalized]

    for key, rates in MODEL_PRICING.items():
        if key in normalized or normalized in key:
            return rates

    return DEFAULT_PRICING


def get_tokenizer_for_model(model_name: str) -> tiktoken.Encoding:
    """Returns tiktoken Encoding instance with graceful fallback to cl100k_base."""
    try:
        # Normalize provider prefixes like 'openai/gpt-4o-mini' -> 'gpt-4o-mini'
        bare_model = model_name.split("/")[-1]
        return tiktoken.encoding_for_model(bare_model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(text: str, model_name: str = "gpt-4o-mini") -> int:
    """Computes exact token count for raw string using tiktoken."""
    if not text:
        return 0
    tokenizer = get_tokenizer_for_model(model_name)
    return len(tokenizer.encode(text))


def count_messages_tokens(messages: list[dict[str, Any]], model_name: str = "gpt-4o-mini") -> int:
    """Computes standard chat message format token count including chat markup overhead."""
    tokenizer = get_tokenizer_for_model(model_name)
    num_tokens = 0
    for message in messages:
        num_tokens += 3  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        for key, value in message.items():
            if value:
                num_tokens += len(tokenizer.encode(str(value)))
            if key == "name":
                num_tokens += 1
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens


def calculate_request_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculates total dollar cost for a completion request."""
    rates = get_model_rates(model_name)
    prompt_cost = (prompt_tokens / 1_000_000.0) * rates["prompt"]
    completion_cost = (completion_tokens / 1_000_000.0) * rates["completion"]
    return round(prompt_cost + completion_cost, 8)


class TokenAnalyticsTracker:
    """Manages real-time cost calculation and cumulative 'Dollars Saved' metrics in Redis."""

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self.redis = redis_client
        # In-memory accumulators as fallback/warm cache
        self._in_memory_dollars_spent: float = 0.0
        self._in_memory_dollars_saved: float = 0.0
        self._in_memory_tokens_processed: int = 0
        self._in_memory_cache_hits: int = 0
        self._in_memory_requests: int = 0

    async def record_llm_execution(
        self,
        tenant_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Records an active LLM generation, updates cumulative spend, and returns cost."""
        cost = calculate_request_cost(model_name, prompt_tokens, completion_tokens)
        total_tokens = prompt_tokens + completion_tokens

        self._in_memory_dollars_spent += cost
        self._in_memory_tokens_processed += total_tokens
        self._in_memory_requests += 1

        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.incrby(f"analytics:tenant:{tenant_id}:prompt_tokens", prompt_tokens)
                pipe.incrby(f"analytics:tenant:{tenant_id}:completion_tokens", completion_tokens)
                pipe.incrbyfloat(f"analytics:tenant:{tenant_id}:dollars_spent", cost)
                pipe.incrby("analytics:global:prompt_tokens", prompt_tokens)
                pipe.incrby("analytics:global:completion_tokens", completion_tokens)
                pipe.incrbyfloat("analytics:global:dollars_spent", cost)
                pipe.incrby("analytics:global:total_requests", 1)
                await pipe.execute()
            except Exception as e:
                logger.warning("redis_analytics_spend_increment_failed", error=str(e))

        return cost

    async def record_cache_hit_savings(
        self,
        tenant_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        baseline_model: str = "openai/gpt-4o-mini",
    ) -> float:
        """Calculates and records dollars saved when a response is served directly from Day 4's cache."""
        dollars_saved = calculate_request_cost(baseline_model, prompt_tokens, completion_tokens)
        total_tokens = prompt_tokens + completion_tokens

        self._in_memory_dollars_saved += dollars_saved
        self._in_memory_cache_hits += 1
        self._in_memory_requests += 1

        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.incrbyfloat(f"analytics:tenant:{tenant_id}:dollars_saved", dollars_saved)
                pipe.incrby(f"analytics:tenant:{tenant_id}:cached_tokens_saved", total_tokens)
                pipe.incrby(f"analytics:tenant:{tenant_id}:cache_hits", 1)
                pipe.incrbyfloat("analytics:global:dollars_saved", dollars_saved)
                pipe.incrby("analytics:global:cached_tokens_saved", total_tokens)
                pipe.incrby("analytics:global:cache_hits", 1)
                pipe.incrby("analytics:global:total_requests", 1)
                await pipe.execute()
            except Exception as e:
                logger.warning("redis_analytics_savings_increment_failed", error=str(e))

        return dollars_saved

    async def get_cumulative_dollars_saved(self, tenant_id: str | None = None) -> float:
        """Returns total cumulative dollars saved across all cache hits."""
        if self.redis is not None:
            try:
                key = (
                    f"analytics:tenant:{tenant_id}:dollars_saved"
                    if tenant_id
                    else "analytics:global:dollars_saved"
                )
                val = await self.redis.get(key)
                if val is not None:
                    return round(float(val), 6)
            except Exception as e:
                logger.warning("redis_get_dollars_saved_failed", error=str(e))

        return round(self._in_memory_dollars_saved, 6)

    async def get_summary_metrics(self, tenant_id: str | None = None) -> dict[str, Any]:
        """Returns comprehensive analytics snapshot."""
        if self.redis is not None:
            try:
                prefix = f"analytics:tenant:{tenant_id}" if tenant_id else "analytics:global"
                pipe = self.redis.pipeline()
                pipe.get(f"{prefix}:dollars_spent")
                pipe.get(f"{prefix}:dollars_saved")
                pipe.get(f"{prefix}:prompt_tokens")
                pipe.get(f"{prefix}:completion_tokens")
                pipe.get(f"{prefix}:cache_hits")
                pipe.get(f"{prefix}:total_requests")
                res = await pipe.execute()

                spent = float(res[0]) if res[0] is not None else self._in_memory_dollars_spent
                saved = float(res[1]) if res[1] is not None else self._in_memory_dollars_saved
                p_tokens = int(res[2]) if res[2] is not None else 0
                c_tokens = int(res[3]) if res[3] is not None else 0
                hits = int(res[4]) if res[4] is not None else self._in_memory_cache_hits
                reqs = int(res[5]) if res[5] is not None else self._in_memory_requests

                return {
                    "dollars_spent": round(spent, 6),
                    "dollars_saved": round(saved, 6),
                    "prompt_tokens": p_tokens,
                    "completion_tokens": c_tokens,
                    "total_tokens": p_tokens + c_tokens,
                    "cache_hits": hits,
                    "total_requests": reqs,
                    "cache_hit_ratio": round(hits / max(1, reqs), 4),
                }
            except Exception as e:
                logger.warning("redis_analytics_metrics_fetch_failed", error=str(e))

        return {
            "dollars_spent": round(self._in_memory_dollars_spent, 6),
            "dollars_saved": round(self._in_memory_dollars_saved, 6),
            "tokens_processed": self._in_memory_tokens_processed,
            "cache_hits": self._in_memory_cache_hits,
            "total_requests": self._in_memory_requests,
            "cache_hit_ratio": round(
                self._in_memory_cache_hits / max(1, self._in_memory_requests), 4
            ),
        }
