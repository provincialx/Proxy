#!/usr/bin/env python3
"""
MCP-сервер для интеграции CacheProxy с AI-ассистентами (Zed).

Предоставляет инструменты для семантического поиска контекста,
получения сессий и сообщений через Model Context Protocol (MCP).

Подключается напрямую к PostgreSQL (через app.database) и переиспользует
существующую логику эмбеддингов и гибридного поиска.

Настройка в Zed (settings.json):
    "context_servers": {
        "cacheproxy": {
            "command": "D:\\Projects\\CacheProxy\\.venv\\Scripts\\python.exe",
            "args": ["D:\\Projects\\CacheProxy\\mcp_server.py"]
        }
    }
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal
from uuid import UUID

# Add project root to path so we can import app.*
PROJECT_ROOT = str(Path(__file__).parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mcp.server import MCPServer

from app.database import SessionLocal
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session
from app.routes import context as context_routes

SNIPPET_LEN = 250

server = MCPServer("cacheproxy")


# ── Formatting helpers ───────────────────────────────────────


def _snippet(text: str | None, max_len: int = SNIPPET_LEN) -> str:
    """Return first N chars of text, with ellipsis if truncated."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + " ..."


def _format_context_preview(ctx: Context, score: float | None = None) -> str:
    """Short preview — no full content. Use get_context_detail for full text."""
    lines = [
        f"ID: {ctx.id}",
        f"Session: {ctx.session_id}",
        f"Summary: {ctx.summary or 'N/A'}",
        f"Keywords: {ctx.keywords or 'N/A'}",
        f"Tokens: {ctx.token_count or '?'}",
        f"Created: {ctx.created_at.isoformat()}",
    ]
    if score is not None:
        lines.append(f"Score: {score:.4f}")
    lines.append("Snippet: " + _snippet(ctx.content))
    return "\n".join(lines)


def _format_context_detail(ctx: Context) -> str:
    """Full context detail with complete content."""
    lines = [
        f"ID: {ctx.id}",
        f"Session: {ctx.session_id}",
        f"Summary: {ctx.summary or 'N/A'}",
        f"Keywords: {ctx.keywords or 'N/A'}",
        f"Tokens: {ctx.token_count or '?'}",
        f"Created: {ctx.created_at.isoformat()}",
        "",
        "--- Content ---",
        "",
    ]
    lines.append(ctx.content or "(empty)")
    return "\n".join(lines)


def _format_session(
    session: Session, messages: list[Message], snippet_len: int = 200
) -> str:
    """Format a session with its messages as snippets."""
    updated = (
        session.updated_at.strftime("%Y-%m-%d %H:%M")
        if session.updated_at
        else "N/A"
    )
    lines = [
        f"Session: {session.id}",
        f"Title: {session.title or 'N/A'}",
        f"Project: {session.project or 'N/A'}",
        f"Status: {session.status}",
        f"Created: {session.created_at.isoformat()}",
        f"Updated: {updated}",
        "",
        f"--- Messages ({len(messages)}) ---",
        "",
    ]
    for m in messages:
        if m.role == 'tool':
            continue
        snippet = _snippet(m.content, snippet_len)
        lines.append(f"[{m.role}] {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Tool implementations (registered via @server.tool()) ────


@server.tool()
async def search_context(query: str, project: str | None = None, limit: int = 5) -> str:
    """Semantic search across stored conversation contexts.
    Returns previews (snippet + metadata).
    Use get_context_detail for full content.

    Гибридный поиск: keyword ILIKE для коротких запросов (1-3 слова),
    semantic search (fastembed + cosine similarity) для длинных.
    """
    db = SessionLocal()
    try:
        limit = min(limit, 20)
        results = context_routes._hybrid_search(
            query, db, project=project, limit=limit
        )
        if not results:
            return "Nothing found."

        formatted = [
            _format_context_preview(ctx, score) for ctx, score in results
        ]
        return (
            f"Search results for '{query}':\n\n"
            + "\n\n---\n\n".join(formatted)
        )
    finally:
        db.close()


@server.tool()
async def get_context_detail(context_id: str) -> str:
    """Get full context detail by ID, including complete content.
    Use after search_context to read a relevant entry in full.
    """
    db = SessionLocal()
    try:
        ctx = db.get(Context, UUID(context_id))
        if not ctx:
            return "Context not found."
        return _format_context_detail(ctx)
    finally:
        db.close()


@server.tool()
async def get_session(session_id: str) -> str:
    """Get session details with message snippets (200 chars each)."""
    db = SessionLocal()
    try:
        sid = UUID(session_id)
        session = db.get(Session, sid)
        if not session:
            return "Session not found."

        messages = (
            db.query(Message)
            .filter(Message.session_id == sid)
            .order_by(Message.created_at.asc())
            .all()
        )
        return _format_session(session, messages)
    finally:
        db.close()


@server.tool()
async def list_sessions(
    project: str | None = None,
    status: Literal["active", "archived"] | None = None,
    limit: int = 20,
) -> str:
    """List chat sessions with optional filters (project, status).
    Lightweight — no message content.
    """
    db = SessionLocal()
    try:
        q = db.query(Session)
        if project:
            q = q.filter(Session.project == project)
        if status:
            q = q.filter(Session.status == status)
        limit = min(limit, 100)
        sessions = q.order_by(Session.updated_at.desc()).limit(limit).all()

        if not sessions:
            return "No sessions found."

        lines = [f"Sessions ({len(sessions)}):", ""]
        for s in sessions:
            msg_count = (
                db.query(Message)
                .filter(Message.session_id == s.id)
                .count()
            )
            ctx_count = (
                db.query(Context)
                .filter(Context.session_id == s.id)
                .count()
            )
            updated = (
                s.updated_at.strftime("%Y-%m-%d %H:%M")
                if s.updated_at
                else "N/A"
            )
            lines.append(
                f"- {s.id} | {s.title or 'Untitled'} | {s.project or '-'} | "
                f"{s.status} | {msg_count} msgs, {ctx_count} ctx | {updated}"
            )
        return "\n".join(lines)
    finally:
        db.close()


@server.tool()
async def get_recent_contexts(project: str | None = None, limit: int = 5) -> str:
    """Get most recent context entries without a search query.
    Returns previews (snippet + metadata).
    Use get_context_detail for full content.
    """
    db = SessionLocal()
    try:
        limit = min(limit, 20)
        q = db.query(Context).filter(Context.embedding.isnot(None))
        if project:
            q = q.join(Context.session).filter(Session.project == project)
        contexts = q.order_by(Context.created_at.desc()).limit(limit).all()

        if not contexts:
            return "No contexts found."

        formatted = [_format_context_preview(ctx) for ctx in contexts]
        return "Recent contexts:\n\n" + "\n\n---\n\n".join(formatted)
    finally:
        db.close()


# ── Entry point ──────────────────────────────────────────────


async def main() -> None:
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
