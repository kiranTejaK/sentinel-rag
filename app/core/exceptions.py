from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.logging import logger


class ProblemDetailException(Exception):
    """Base exception adhering to RFC 7807 (Problem Details for HTTP APIs)."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str,
        type_uri: str = "about:blank",
        invalid_params: list[dict[str, Any]] | None = None,
    ):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.type_uri = type_uri
        self.invalid_params = invalid_params or []


class RateLimitExceededException(ProblemDetailException):
    def __init__(
        self, detail: str = "Tenant token-bucket rate limit exceeded.", retry_after_ms: int = 1000
    ):
        super().__init__(
            status=429,
            title="Too Many Requests",
            detail=detail,
            type_uri="https://sentinelrag.io/errors/rate-limit-exceeded",
        )
        self.retry_after_ms = retry_after_ms


class DatabaseConnectionException(ProblemDetailException):
    def __init__(self, detail: str = "Database connection pool failure."):
        super().__init__(
            status=503,
            title="Service Unavailable",
            detail=detail,
            type_uri="https://sentinelrag.io/errors/database-unavailable",
        )


async def problem_detail_exception_handler(
    request: Request, exc: ProblemDetailException
) -> JSONResponse:
    """RFC 7807 problem detail response generator."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.warning(
        "http_problem_detail",
        status=exc.status,
        title=exc.title,
        detail=exc.detail,
        request_id=request_id,
        path=request.url.path,
    )

    headers = {"Content-Type": "application/problem+json"}
    if isinstance(exc, RateLimitExceededException):
        headers["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))

    return JSONResponse(
        status_code=exc.status,
        content={
            "type": exc.type_uri,
            "title": exc.title,
            "status": exc.status,
            "detail": exc.detail,
            "instance": request.url.path,
            "request_id": request_id,
            "invalid_params": exc.invalid_params,
        },
        headers=headers,
    )
