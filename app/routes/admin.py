"""Admin routes — sync, daemon, DB reset, raw Zed threads."""

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
from app.services.summarizer import Summarizer

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


class ReSummarizeResult(BaseModel):
    processed: int
    summarized: int
    errors: list[str]


class DbResetResult(BaseModel):
    sessions_deleted: int
    messages_deleted: int
    contexts_deleted: int
    cache_deleted: bool
    sent_cache_cleared: bool = False


class ZedThreadOut(BaseModel):
    id: str
    title: str | None
    project: str
    archived: bool
    message_count: int
    compressed_bytes: int
    decompressed_bytes: int | None
    created_at: str | None
    updated_at: str | None
    error: str | None = None


class ZedThreadsResult(BaseModel):
    threads: list[ZedThreadOut]
    total: int
    total_messages: int
    archived_count: int
    active_count: int
    total_compressed_mb: float
    total_decompressed_mb: float | None


# ── Sync ────────────────────────────────────────────────────


@router.get("/projects", response_model=ProjectsResult)
def admin_projects():
    """List available projects.

    Tries Zed SQLite first (sync_agent), falls back to PostgreSQL sessions.
    """
    try:
        from sync_agent import get_available_projects

        projects = get_available_projects()
        if projects:
            return ProjectsResult(projects=projects)
    except Exception as e:
        print(f"⚠ admin/projects: sync_agent failed ({e}), fallback to DB")

    # Fallback: projects from PostgreSQL sessions
    db = SessionLocal()
    try:
        rows = (
            db.query(Session.project)
            .filter(Session.project.isnot(None), Session.project != "")
            .distinct()
            .order_by(Session.project)
            .all()
        )
        return ProjectsResult(projects=[r[0] for r in rows])
    finally:
        db.close()


@router.post("/sync", response_model=SyncResult)
def admin_sync(projects: str | None = None):
    """Run one sync pass — send archived Zed threads to CacheProxy.

    Query param: ?projects=proj1,proj2  (comma-separated, optional)
    """
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


@router.post("/resummarize", response_model=ReSummarizeResult)
def admin_resummarize(session_id: str | None = None):
    """Re-summarize archived sessions using LLM.

    Without session_id — processes ALL archived sessions.
    With session_id — processes only that session.
    Replaces per-message contexts with a single LLM summary.
    """
    db = SessionLocal()
    try:
        q = db.query(Session).filter(Session.status == "archived")
        if session_id:
            from uuid import UUID

            q = q.filter(Session.id == UUID(session_id))
        sessions = q.all()

        summarizer = Summarizer()
        processed = 0
        summarized = 0
        errors = []

        for s in sessions:
            processed += 1
            try:
                messages = (
                    db.query(Message)
                    .filter(Message.session_id == s.id)
                    .order_by(Message.created_at.asc())
                    .all()
                )
                if not messages:
                    continue

                msg_tuples = [(m.role, m.content) for m in messages]
                result = summarizer.summarize(
                    msg_tuples,
                    session_title=s.title,
                    project=s.project,
                )
                if not result.get("summary"):
                    continue

                # Delete existing contexts for this session
                db.query(Context).filter(Context.session_id == s.id).delete()

                ctx = Context(
                    session_id=s.id,
                    summary=result.get("title")
                    or f"Archived session: {s.title or 'Untitled'}",
                    content=result["summary"],
                    keywords=result.get("keywords") or (s.project or ""),
                    token_count=sum(m.tokens_used or 0 for m in messages),
                )
                try:
                    from app.services import EmbeddingService

                    ctx.embedding = EmbeddingService.embed(ctx.content)
                except Exception as e:
                    print(f"⚠ Resummarize embedding failed: {e}")
                db.add(ctx)
                summarized += 1
                print(f"  ✓ Resummarized {s.id} ({s.title})")
            except Exception as e:
                errors.append(str(e))
                print(f"⚠ Resummarize error session {s.id}: {e}")

        db.commit()
        return ReSummarizeResult(
            processed=processed,
            summarized=summarized,
            errors=errors,
        )
    finally:
        db.close()


# ── Raw Zed threads (Junk view) ────────────────────────────


def _zed_db_paths() -> tuple[Path | None, Path | None]:
    """Return (sidebar_db, threads_db) paths."""
    import os

    zed_dir = None
    for var in ("LOCALAPPDATA", "HOME"):
        base = os.environ.get(var)
        if not base:
            continue
        if var == "LOCALAPPDATA":
            p = Path(base) / "Zed"
        else:
            p = Path(base) / ".local/share/zed"
        if p.exists():
            zed_dir = p
            break
    if not zed_dir:
        return None, None
    sidebar = zed_dir / "db" / "0-stable" / "db.sqlite"
    threads = zed_dir / "threads" / "threads.db"
    return (
        sidebar if sidebar.exists() else None,
        threads if threads.exists() else None,
    )


@router.get("/zed-threads", response_model=ZedThreadsResult)
def admin_zed_threads():
    """Read ALL threads directly from Zed SQLite databases (raw, unfiltered).

    Includes archived and non-archived, error titles, untitled, everything.
    Useful for comparing what's in CacheProxy vs what's actually in Zed.
    """
    import json as json_lib
    import sqlite3

    import zstandard

    sidebar_db, threads_db = _zed_db_paths()
    if not sidebar_db or not threads_db:
        raise HTTPException(status_code=503, detail="Zed databases not found")

    # Read sidebar threads
    s_conn = sqlite3.connect(f"file:{sidebar_db}?mode=ro", uri=True)
    s_conn.row_factory = sqlite3.Row
    s_cur = s_conn.cursor()
    s_cur.execute(
        "SELECT session_id, title, folder_paths, archived, "
        "created_at, updated_at, interacted_at "
        "FROM sidebar_threads ORDER BY interacted_at DESC"
    )
    sidebar_rows = [dict(r) for r in s_cur.fetchall()]
    s_conn.close()

    # Read thread content
    t_conn = sqlite3.connect(f"file:{threads_db}?mode=ro", uri=True)
    t_conn.row_factory = sqlite3.Row
    t_cur = t_conn.cursor()
    t_cur.execute("SELECT id, summary, data FROM threads")
    thread_map: dict[str, dict] = {}
    for r in t_cur.fetchall():
        d = dict(r)
        thread_map[d["id"]] = d
    t_conn.close()

    threads: list[ZedThreadOut] = []
    total_messages = 0
    archived_count = 0
    active_count = 0
    total_compressed = 0
    total_decompressed = 0
    decompressed_ok = True

    for row in sidebar_rows:
        sid = row["session_id"]
        if not sid:
            continue

        folder_paths = (row.get("folder_paths") or "").strip()
        project = "zed"
        if folder_paths:
            project = Path(folder_paths.split("\n")[0].strip()).name or "zed"

        thread_entry = thread_map.get(sid)
        compressed_bytes = 0
        msg_count = 0
        decompressed_bytes = None
        error = None

        if thread_entry and thread_entry.get("data"):
            raw_data = thread_entry["data"]
            compressed_bytes = len(raw_data)
            total_compressed += compressed_bytes
            try:
                dctx = zstandard.ZstdDecompressor()
                reader = dctx.stream_reader(raw_data)
                decompressed = reader.read()
                decompressed_bytes = len(decompressed)
                total_decompressed += decompressed_bytes
                thread_data = json_lib.loads(decompressed.decode("utf-8"))
                raw_msgs = thread_data.get("messages", [])
                for msg in raw_msgs:
                    if isinstance(msg, dict):
                        msg_count += 1
                    elif isinstance(msg, str) and msg.strip():
                        msg_count += 1
            except Exception as e:
                error = str(e)
                decompressed_ok = False
        else:
            error = "No content in threads.db"

        title = row.get("title") or (
            thread_entry.get("summary") if thread_entry else None
        )
        if not title:
            title = None

        total_messages += msg_count
        if row.get("archived"):
            archived_count += 1
        else:
            active_count += 1

        threads.append(
            ZedThreadOut(
                id=sid,
                title=title,
                project=project,
                archived=bool(row.get("archived")),
                message_count=msg_count,
                compressed_bytes=compressed_bytes,
                decompressed_bytes=decompressed_bytes,
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                error=error,
            )
        )

    return ZedThreadsResult(
        threads=threads,
        total=len(threads),
        total_messages=total_messages,
        archived_count=archived_count,
        active_count=active_count,
        total_compressed_mb=round(total_compressed / 1024 / 1024, 1),
        total_decompressed_mb=round(total_decompressed / 1024 / 1024, 1)
        if decompressed_ok
        else None,
    )


@router.get("/zed-threads/{thread_id}/messages")
def admin_zed_thread_messages(thread_id: str):
    """Get RAW messages for a thread directly from Zed (unfiltered).

    Includes all block types: Text, ToolUse, Thinking, Mention, Image.
    Returns messages with their original structure preserved.
    """
    import json as json_lib
    import sqlite3

    import zstandard

    sidebar_db, threads_db = _zed_db_paths()
    if not sidebar_db or not threads_db:
        raise HTTPException(status_code=503, detail="Zed databases not found")

    # Read thread content
    t_conn = sqlite3.connect(f"file:{threads_db}?mode=ro", uri=True)
    t_cur = t_conn.cursor()
    t_cur.execute("SELECT data FROM threads WHERE id = ?", (thread_id,))
    t_row = t_cur.fetchone()
    t_conn.close()

    if not t_row or not t_row[0]:
        raise HTTPException(status_code=404, detail="Thread content not found")

    # Decompress
    try:
        dctx = zstandard.ZstdDecompressor()
        reader = dctx.stream_reader(t_row[0])
        thread_data = json_lib.loads(reader.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to decompress: {e}")

    # Parse ALL messages with ALL block types preserved
    result_messages = []
    for msg in thread_data.get("messages", []):
        if isinstance(msg, str):
            if msg.strip():
                result_messages.append(
                    {
                        "role": "user",
                        "content": msg.strip(),
                        "blocks": ["text"],
                        "has_thinking": False,
                    }
                )
            continue

        role_key = list(msg.keys())[0]
        role_map = {"User": "user", "Agent": "assistant", "System": "system"}
        role = role_map.get(role_key, "user")
        blocks = msg[role_key].get("content", [])

        texts = []
        block_types = set()
        has_thinking = False
        for b in blocks:
            if isinstance(b, dict):
                key = list(b.keys())[0]
                block_types.add(key)
                if key == "Text":
                    texts.append(b[key])
                elif key == "Thinking":
                    has_thinking = True
                    texts.append(f"[Thinking] {b[key]}")
                elif key == "ToolUse":
                    tool_name = b.get("ToolUse", {}).get("name", "")
                    tool_input = b.get("ToolUse", {}).get("input", "")
                    inp_str = (
                        json_lib.dumps(tool_input, ensure_ascii=False)
                        if tool_input
                        else ""
                    )
                    texts.append(f"[ToolUse: {tool_name}] {inp_str[:200]}")
                elif key == "Mention":
                    texts.append(f"[Mention: {b[key]}]")
                elif key == "Image":
                    texts.append("[Image]")
                else:
                    val = str(b[key])
                    texts.append(f"[{key}] {val[:200]}")
            elif isinstance(b, str):
                texts.append(b)
                block_types.add("text")

        text = "\n".join(texts).strip()
        result_messages.append(
            {
                "role": role,
                "content": text,
                "blocks": sorted(block_types),
                "has_thinking": has_thinking,
            }
        )

    return {"messages": result_messages}


@router.post("/zed-threads/{thread_id}/sync")
def admin_sync_zed_thread(thread_id: str):
    """Sync a single raw Zed thread into CacheProxy.

    Reads the thread directly from Zed SQLite databases and creates
    a session in CacheProxy with all messages.
    """
    import json as json_lib
    import sqlite3
    import uuid as uuid_lib
    from datetime import datetime, timezone

    import httpx
    import zstandard

    sidebar_db, threads_db = _zed_db_paths()
    if not sidebar_db or not threads_db:
        raise HTTPException(status_code=503, detail="Zed databases not found")

    # Read from sidebar
    s_conn = sqlite3.connect(f"file:{sidebar_db}?mode=ro", uri=True)
    s_conn.row_factory = sqlite3.Row
    s_cur = s_conn.cursor()
    s_cur.execute(
        "SELECT session_id, title, folder_paths, archived "
        "FROM sidebar_threads WHERE session_id = ?",
        (thread_id,),
    )
    s_row = s_cur.fetchone()
    s_conn.close()

    if not s_row:
        raise HTTPException(status_code=404, detail="Thread not found in Zed sidebar")
    # Convert to dict for .get() access
    s_row = dict(s_row)

    # Read thread content
    t_conn = sqlite3.connect(f"file:{threads_db}?mode=ro", uri=True)
    t_cur = t_conn.cursor()
    t_cur.execute("SELECT data, summary FROM threads WHERE id = ?", (thread_id,))
    t_row = t_cur.fetchone()
    t_conn.close()

    if not t_row or not t_row[0]:
        raise HTTPException(
            status_code=404, detail="Thread content not found in Zed threads.db"
        )

    thread_summary = t_row[1]

    # Decompress
    try:
        dctx = zstandard.ZstdDecompressor()
        reader = dctx.stream_reader(t_row[0])
        thread_data = json_lib.loads(reader.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to decompress thread: {e}")

    # Parse messages
    messages = []
    for msg in thread_data.get("messages", []):
        if isinstance(msg, str):
            if msg.strip():
                messages.append({"role": "user", "content": msg.strip()})
            continue
        role_key = list(msg.keys())[0]
        role_map = {"User": "user", "Agent": "assistant", "System": "system"}
        role = role_map.get(role_key, "user")
        blocks = msg[role_key].get("content", [])
        texts = []
        for b in blocks:
            if isinstance(b, dict) and "Text" in b:
                texts.append(b["Text"])
        text = " ".join(texts).strip()
        if text:
            messages.append({"role": role, "content": text})

    if not messages:
        raise HTTPException(status_code=400, detail="No parseable messages in thread")

    # Determine project
    folder_paths = (s_row.get("folder_paths") or "").strip()
    project = "zed"
    if folder_paths:
        project = Path(folder_paths.split("\n")[0].strip()).name or "zed"

    title = s_row.get("title") or thread_summary or "Untitled"

    # Send to CacheProxy via internal HTTP
    base = "http://127.0.0.1:8100"
    try:
        with httpx.Client(base_url=base, timeout=120.0) as client:
            # Create session
            r = client.post(
                "/sessions",
                json={
                    "title": title[:255],
                    "project": project[:255] if project else None,
                },
            )
            if r.status_code not in (200, 201):
                raise HTTPException(
                    status_code=502, detail=f"Failed to create session: {r.status_code}"
                )
            sid = r.json()["id"]

            # Send messages
            for msg in messages:
                content = msg["content"]
                if len(content) > 40000:
                    content = content[:40000] + "\n\n... (truncated)"
                r = client.post(
                    "/messages",
                    json={"session_id": sid, "role": msg["role"], "content": content},
                )
                if r.status_code not in (200, 201):
                    print(f"  ⚠ Message send failed: {r.status_code}")

            # Archive if original was archived
            if s_row.get("archived"):
                r = client.post(f"/sessions/{sid}/archive")
                if r.status_code not in (200, 201):
                    print(f"  ⚠ Archive failed: {r.status_code}")

        return {
            "synced": True,
            "session_id": sid,
            "title": title,
            "project": project,
            "messages": len(messages),
            "archived": bool(s_row.get("archived")),
        }
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="CacheProxy not reachable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")
