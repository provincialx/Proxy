"""Pydantic schemas for API request/response."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# ── Session ────────────────────────────────────────────────


class SessionCreate(BaseModel):
    title: str | None = None
    project: str | None = None
    metadata: str | None = None


class SessionOut(BaseModel):
    id: UUID
    title: str | None
    project: str | None
    created_at: datetime
    updated_at: datetime
    metadata: str | None = Field(None, alias="metadata_")
    message_count: int = 0

    model_config = {"from_attributes": True, "populate_by_name": True}


class SessionList(BaseModel):
    sessions: list[SessionOut]
    total: int


# ── Message ────────────────────────────────────────────────


class MessageCreate(BaseModel):
    session_id: UUID
    role: str = Field(..., pattern=r"^(user|assistant|system)$")
    content: str
    tokens_used: int | None = None


class MessageOut(BaseModel):
    id: UUID
    session_id: UUID
    role: str
    content: str
    tokens_used: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Context ────────────────────────────────────────────────


class ContextCreate(BaseModel):
    session_id: UUID
    summary: str | None = None
    content: str
    keywords: str | None = None
    token_count: int | None = None


class ContextOut(BaseModel):
    id: UUID
    session_id: UUID
    summary: str | None
    content: str
    keywords: str | None
    token_count: int | None
    created_at: datetime
    score: float | None = None

    model_config = {"from_attributes": True}


class ContextSearchResult(BaseModel):
    results: list[ContextOut]
    query: str


class ContextSearch(BaseModel):
    query: str
    limit: int = 10
