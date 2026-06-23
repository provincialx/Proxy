"""Chat session model."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Session(Base):
    """A single chat session / conversation."""

    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=True)
    project = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Project identifier (e.g. repo name or path)",
    )
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    status = Column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
        comment="active | archived",
    )
    archived_at = Column(
        DateTime(timezone=True), nullable=True, comment="When session was archived"
    )
    metadata_ = Column("metadata", Text, nullable=True, comment="JSON-encoded metadata")

    messages = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan"
    )
    contexts = relationship(
        "Context", back_populates="session", cascade="all, delete-orphan"
    )
