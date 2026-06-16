"""Admin routes — sync, daemon, DB reset."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import SessionLocal
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Daemon state ────────────────────────────────────────────
_daemon_stop = threading.Event()
_daemon_thread: threading.Thread | None = None
_daemon_interval = 60


class SyncResult(BaseModel):
    synced: int
    total: int
    errors: list[str]


class ProjectsResult(BaseModel):
    projects: list[str]


class DaemonStatus(BaseModel):
    running: bool
    interval: int


class DbResetResult(BaseModel):
    sessions_deleted: int
    messages_deleted: int
    contexts_deleted: int
    cache_deleted: bool
    sent_cache_cleared: bool = False


# ── Sync ────────────────────────────────────────────────────


@router.get("/projects", response_model=ProjectsResult)
def admin_projects():
    """List available projects from archived Zed threads."""
    from sync_agent import get_available_projects

    return ProjectsResult(projects=get_available_projects())


@router.post("/sync", response_model=SyncResult)
def admin_sync(projects: str | None = None):
    """Run one sync pass — send archived Zed threads to CacheProxy.

    Query param: ?projects=proj1,proj2  (comma-separated, optional)
    """
    from app.config import settings
    from sync_agent import run_sync

    project_list = None
    if projects:
        project_list = [p.strip() for p in projects.split(",") if p.strip()]

    res = run_sync(projects=project_list)
    return SyncResult(**res)


# ── Daemon ──────────────────────────────────────────────────


def _daemon_loop(interval: int) -> None:
    """Daemon loop — runs sync every N seconds until stop event."""
    from sync_agent import run_sync

    while not _daemon_stop.is_set():
        try:
            run_sync()
        except Exception as e:
            print(f"⚠ Daemon sync error: {e}")
        _daemon_stop.wait(timeout=interval)


@router.post("/daemon/start")
def admin_daemon_start(interval: int = 60):
    """Start the sync daemon in background."""
    global _daemon_thread, _daemon_stop, _daemon_interval

    if _daemon_thread and _daemon_thread.is_alive():
        raise HTTPException(status_code=409, detail="Daemon already running")

    _daemon_stop.clear()
    _daemon_interval = interval
    _daemon_thread = threading.Thread(
        target=_daemon_loop, args=(interval,), daemon=True, name="sync-daemon"
    )
    _daemon_thread.start()
    return {"status": "started", "interval": interval}


@router.post("/daemon/stop")
def admin_daemon_stop():
    """Stop the sync daemon."""
    global _daemon_thread

    if not _daemon_thread or not _daemon_thread.is_alive():
        raise HTTPException(status_code=409, detail="Daemon not running")

    _daemon_stop.set()
    _daemon_thread.join(timeout=10)
    _daemon_thread = None
    return {"status": "stopped"}


@router.get("/daemon/status", response_model=DaemonStatus)
def admin_daemon_status():
    """Check if daemon is running."""
    global _daemon_thread, _daemon_interval
    return DaemonStatus(
        running=bool(_daemon_thread and _daemon_thread.is_alive()),
        interval=_daemon_interval,
    )


# ── DB Reset ────────────────────────────────────────────────


@router.post("/db-reset", response_model=DbResetResult)
def admin_db_reset():
    """Delete all data from DB and remove model cache."""
    db = SessionLocal()
    try:
        ctx_del = db.query(Context).delete()
        msg_del = db.query(Message).delete()
        sess_del = db.query(Session).delete()
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    finally:
        db.close()

    # Remove model cache
    cache_dir = Path(__file__).parent.parent / ".model_cache"
    cache_deleted = False
    if cache_dir.exists():
        import shutil

        shutil.rmtree(cache_dir, ignore_errors=True)
        cache_deleted = True

    # Clear sent markers cache
    from pathlib import Path as PPath

    sent_cache_file = PPath.home() / ".cacheproxy_sent_markers.json"
    sent_cache_cleared = False
    if sent_cache_file.exists():
        sent_cache_file.unlink(missing_ok=True)
        sent_cache_cleared = True

    return DbResetResult(
        sessions_deleted=sess_del,
        messages_deleted=msg_del,
        contexts_deleted=ctx_del,
        cache_deleted=cache_deleted,
        sent_cache_cleared=sent_cache_cleared,
    )
