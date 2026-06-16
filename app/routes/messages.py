"""Message routes — add & list messages per session."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session as SASession

from app.database import get_db
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session
from app.schemas import MessageCreate, MessageOut
from app.services import EmbeddingService
from app.utils import strip_thinking

router = APIRouter(prefix="/messages", tags=["messages"])


@router.post("", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def create_message(body: MessageCreate, db: SASession = Depends(get_db)):
    """Add a message to a session."""
    session = db.get(Session, body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    clean_content = strip_thinking(body.content)

    message = Message(
        session_id=body.session_id,
        role=body.role,
        content=clean_content,
        tokens_used=body.tokens_used,
    )
    db.add(message)

    # Auto-save as context for semantic search
    ctx = Context(
        session_id=body.session_id,
        content=clean_content,
        keywords=session.project or "",
        token_count=body.tokens_used,
    )
    # Generate embedding — non-critical, message saves even if embedding fails
    try:
        ctx.embedding = EmbeddingService.embed(clean_content)
    except Exception as embed_err:
        print(f"⚠ Embedding failed (message saved without vector): {embed_err}")
    db.add(ctx)

    # Bump session updated_at
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(message)
    return message


@router.get("/{session_id}", response_model=list[MessageOut])
def list_messages(
    session_id: UUID,
    skip: int = 0,
    limit: int = 200,
    db: SASession = Depends(get_db),
):
    """Get all messages for a session, oldest first."""
    session = db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return messages
