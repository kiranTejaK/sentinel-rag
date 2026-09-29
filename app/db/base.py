"""SQLAlchemy 2.0 Declarative Base & Reusable Model Mixins.
Provides automated UUID primary keys and timezone-aware UTC timestamps.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy 2.0 ORM models in SentinelRAG."""

    pass


class UUIDPrimaryKeyMixin:
    """Mixin that injects an automated UUIDv4 primary key."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        doc="Unique identifier (UUIDv4) for the entity",
    )


def utc_now() -> datetime:
    """Returns the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


class TimestampMixin:
    """Mixin that injects created_at and updated_at UTC timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
        doc="UTC timestamp indicating when the entity was created",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        doc="UTC timestamp indicating when the entity was last updated",
    )
