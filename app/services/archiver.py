"""Background auto-archiver — marks old inactive sessions as archived."""

import threading
import time
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session
from app.services import EmbeddingService
from app.services.summarizer import Summarizer
from app.utils import strip_thinking

# Сессии без обновлений дольше этого периода — авто-архивация
AUTO_ARCHIVE_DAYS = 7
# Интервал проверки (секунд)
CHECK_INTERVAL = 60

# Глобальный экземпляр суммаризатора (ленивая инициализация)
_summarizer: Summarizer | None = None


def _get_summarizer() -> Summarizer:
    global _summarizer
    if _summarizer is None:
        _summarizer = Summarizer()
    return _summarizer


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
    if not messages:
        db.commit()
        print(f"✓ Auto-archived session {session.id} ({session.title}) — no messages")
        return

    # Try LLM summarization first
    summarizer = _get_summarizer()
    msg_tuples = [(m.role, m.content) for m in messages]
    llm_result = summarizer.summarize(msg_tuples)

    if llm_result.get("summary"):
        # LLM summarization succeeded — create ONE consolidated context
        summary = (
            llm_result.get("title")
            or f"Archived session: {session.title or 'Untitled'}"
        )
        keywords = llm_result.get("keywords") or (session.project or "")
        consolidated = llm_result["summary"]

        ctx = Context(
            session_id=session.id,
            summary=summary,
            content=consolidated,
            keywords=keywords,
            token_count=sum(m.tokens_used or 0 for m in messages),
        )
        try:
            ctx.embedding = EmbeddingService.embed(consolidated)
        except Exception as e:
            print(f"⚠ Auto-archive embedding failed for summary: {e}")
        db.add(ctx)
        print(
            f"✓ Auto-archived session {session.id} ({session.title}) "
            f"— LLM summary ({len(consolidated)} chars)"
        )
    else:
        # Fallback: per-message contexts (original behavior)
        summary = f"Archived session: {session.title or 'Untitled'}"
        for m in messages:
            clean_content = strip_thinking(m.content)
            ctx = Context(
                session_id=session.id,
                summary=summary,
                content=clean_content,
                keywords=session.project or "",
                token_count=m.tokens_used,
            )
            try:
                ctx.embedding = EmbeddingService.embed(clean_content)
            except Exception as e:
                print(f"⚠ Auto-archive embedding failed for msg {m.id}: {e}")
            db.add(ctx)
        print(
            f"✓ Auto-archived session {session.id} ({session.title}) "
            f"— {len(messages)} raw messages"
        )

    db.commit()


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
