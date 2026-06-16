"""Context entry model — stores conversation snippets for retrieval."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import ARRAY, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Context(Base):
    """A stored context snippet from a conversation."""

    __tablename__ = "contexts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary = Column(
        String(500), nullable=True, comment="Short description of this snippet"
    )
    content = Column(Text, nullable=False, comment="The actual context text")
    keywords = Column(
        String(500), nullable=True, comment="Comma-separated keywords for search"
    )
    embedding = Column(ARRAY(Float), nullable=True, comment="384-dim vector embedding")
    token_count = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    session = relationship("Session", back_populates="contexts")
