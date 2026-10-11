import logging
import sys

import structlog

from app.core.config import settings


def setup_logging() -> None:
    """Configures structured JSON logging with correlation ID support.
    Completely eliminates raw print statements across the service.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # In production, use JSONRenderer. In development, use JSON if requested or console
    if settings.ENVIRONMENT == "development" and settings.DEBUG:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.LOG_LEVEL)

    # Silence overly verbose third-party loggers
    for noisy in ["uvicorn.access", "sqlalchemy.engine", "asyncio"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = structlog.get_logger("sentinel.gateway")
