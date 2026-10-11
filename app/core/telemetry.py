"""Prometheus metrics instrumentation for SentinelRAG AI Gateway.

Partitioned metrics:
- http_request_duration_seconds: Histogram partitioned by endpoint and status.
- cache_operations_total: Counter partitioned by hit/miss/tenant.
- tokens_consumed_total: Counter partitioned by tenant and model.
- cost_saved_usd_total: Counter partitioned by tenant.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

# 1. HTTP Request Latency partitioned by endpoint and HTTP status code
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds partitioned by endpoint and status code.",
    ["endpoint", "status"],
    buckets=(
        0.005,
        0.01,
        0.025,
        0.05,
        0.075,
        0.1,
        0.25,
        0.5,
        0.75,
        1.0,
        2.5,
        5.0,
        10.0,
    ),
)

# 2. Semantic Cache operations partitioned by operation result (hit/miss) and tenant
CACHE_OPERATIONS_TOTAL = Counter(
    "cache_operations_total",
    "Total count of semantic and exact cache operations partitioned by result and tenant.",
    ["result", "tenant"],
)

# 3. LLM Tokens consumed counter partitioned by tenant and model
TOKENS_CONSUMED_TOTAL = Counter(
    "tokens_consumed_total",
    "Total LLM tokens consumed partitioned by tenant and model.",
    ["tenant", "model"],
)

# 4. Total cumulative cost saved in USD via semantic cache hits partitioned by tenant
COST_SAVED_USD_TOTAL = Counter(
    "cost_saved_usd_total",
    "Total cumulative dollar cost saved via semantic cache hits partitioned by tenant.",
    ["tenant"],
)


def metrics_response() -> Response:
    """Exports Prometheus telemetry formatted according to the latest exposition standards."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
