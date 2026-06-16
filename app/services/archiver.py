"""Background auto-archiver — marks old inactive sessions as archived."""

import threading
import time
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session
from app.services import EmbeddingService

# Сессии без обновлений дольше этого периода — авто-архивация
AUTO_ARCHIVE_DAYS = 7
# Интервал проверки (секунд)
CHECK_INTERVAL = 60


def _archive_session(db, session: Session) -> None:
    """Mark session as archived and consolidate its messages."""
    session.status = "archived"
    session.archived_at = datetime.now(timezone.utc)

    messages = (
        db.query(Message)
        .filter(Message.session_id == session.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    if messages:
        summary = f"Archived session: {session.title or 'Untitled'}"

        for m in messages:
            ctx = Context(
                session_id=session.id,
                summary=summary,
                content=m.content,
                keywords=session.project or "",
                token_count=m.tokens_used,
            )
            try:
                ctx.embedding = EmbeddingService.embed(m.content)
            except Exception as e:
                print(f"⚠ Auto-archive embedding failed for msg {m.id}: {e}")
            db.add(ctx)

    db.commit()
    print(f"✓ Auto-archived session {session.id} ({session.title})")


def _archive_loop() -> None:
    """Background loop: check for stale sessions every CHECK_INTERVAL seconds."""
    cutoff = timedelta(days=AUTO_ARCHIVE_DAYS)
    while True:
        try:
            db = SessionLocal()
            try:
                stale_cutoff = datetime.now(timezone.utc) - cutoff
                stale_sessions = (
                    db.query(Session)
                    .filter(
                        Session.status == "active",
                        Session.updated_at < stale_cutoff,
                    )
                    .all()
                )
                for session in stale_sessions:
                    _archive_session(db, session)
                if stale_sessions:
                    print(f"  Archived {len(stale_sessions)} stale session(s)")
            finally:
                db.close()
        except Exception as e:
            print(f"⚠ Auto-archiver error: {e}")
        time.sleep(CHECK_INTERVAL)


def start_archiver() -> threading.Thread:
    """Start the background archiver daemon thread."""
    thread = threading.Thread(target=_archive_loop, daemon=True, name="auto-archiver")
    thread.start()
    print(
        f"✓ Auto-archiver started (interval={CHECK_INTERVAL}s, idle_days={AUTO_ARCHIVE_DAYS})"
    )
    return thread
