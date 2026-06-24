"""Session CRUD routes."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session as SASession

from app.database import get_db
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session
from app.schemas import SessionCreate, SessionList, SessionOut
from app.services import EmbeddingService
from app.services.summarizer import Summarizer

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(body: SessionCreate, db: SASession = Depends(get_db)):
    """Create a new chat session."""
    session = Session(title=body.title, project=body.project, metadata_=body.metadata)
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_with_count(session, db)


@router.get("", response_model=SessionList)
def list_sessions(
    project: str | None = None,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: SASession = Depends(get_db),
):
    """List sessions, newest first. Optionally filter by project and status."""
    q = db.query(Session)
    if project:
        q = q.filter(Session.project == project)
    if status:
        if status == "active":
            # NULL = legacy sessions before status column existed
            q = q.filter((Session.status == "active") | Session.status.is_(None))
        else:
            q = q.filter(Session.status == status)
    total = q.count()
    sessions = q.order_by(Session.updated_at.desc()).offset(skip).limit(limit).all()
    return SessionList(
        sessions=[_session_with_count(s, db) for s in sessions],
        total=total,
    )


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: UUID, db: SASession = Depends(get_db)):
    """Get a single session by ID."""
    session = db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_with_count(session, db)


@router.post("/{session_id}/archive", response_model=SessionOut)
def archive_session(session_id: UUID, db: SASession = Depends(get_db)):
    """Archive a session: mark as archived + consolidate messages into a context entry."""
    session = db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == "archived":
        raise HTTPException(status_code=400, detail="Session already archived")

    session.status = "archived"
    session.archived_at = datetime.now(timezone.utc)

    messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    if messages:
        summarizer = Summarizer()
        msg_tuples = [(m.role, m.content) for m in messages]
        result = summarizer.summarize(
            msg_tuples,
            session_title=session.title,
            project=session.project,
        )

        ctx = Context(
            session_id=session_id,
            summary=result.get("title")
            or f"Archived session: {session.title or 'Untitled'}",
            content=result.get("summary") or "",
            keywords=result.get("keywords") or (session.project or ""),
            token_count=sum(m.tokens_used or 0 for m in messages),
        )
        try:
            ctx.embedding = EmbeddingService.embed(ctx.content or "")
        except Exception as e:
            print(f"⚠ Archive embedding failed: {e}")
        db.add(ctx)

    db.commit()
    db.refresh(session)
    return _session_with_count(session, db)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: UUID, db: SASession = Depends(get_db)):
    """Delete a session and all its messages/contexts."""
    session = db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()


def _session_with_count(session: Session, db: SASession) -> SessionOut:
    """Hydrate SessionOut with message count."""
    count = (
        db.query(func.count(Message.id))
        .filter(Message.session_id == session.id)
        .scalar()
    )
    return SessionOut(
        id=session.id,
        title=session.title,
        project=session.project,
        created_at=session.created_at,
        updated_at=session.updated_at,
        metadata_=session.metadata_,
        status=session.status or "active",
        archived_at=session.archived_at,
        message_count=count or 0,
    )
