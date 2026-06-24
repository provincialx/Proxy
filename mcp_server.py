#!/usr/bin/env python3
"""
MCP-сервер для интеграции CacheProxy с AI-ассистентами (Zed).

Предоставляет инструменты для семантического поиска контекста,
получения сессий и сообщений через Model Context Protocol (MCP).

Подключается напрямую к PostgreSQL (через app.database) и переиспользует
существующую логику эмбеддингов и гибридного поиска.

Настройка в Zed (settings.json) — обязательно указать полный путь к python из .venv:
    "mcp": {
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
from uuid import UUID

# Add project root to path so we can import app.*
PROJECT_ROOT = str(Path(__file__).parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from app.database import SessionLocal
from app.models.context import Context
from app.models.message import Message
from app.models.session import Session
from app.routes import context as context_routes

SNIPPET_LEN = 250

server = Server("cacheproxy")


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
        f"",
        f"--- Content ---",
        f"",
    ]
    lines.append(ctx.content or "(empty)")
    return "\n".join(lines)


def _format_session(
    session: Session, messages: list[Message], snippet_len: int = 200
) -> str:
    """Format a session with its messages as snippets."""
    lines = [
        f"Session: {session.id}",
        f"Title: {session.title or 'N/A'}",
        f"Project: {session.project or 'N/A'}",
        f"Status: {session.status}",
        f"Created: {session.created_at.isoformat()}",
        f"Updated: {session.updated_at.isoformat()}",
        f"",
        f"--- Messages ({len(messages)}) ---",
        "",
    ]
    for m in messages:
        if m.role == 'tool':
            continue  # Skip tool messages — break API providers
        snippet = _snippet(m.content, snippet_len)
        lines.append(f"[{m.role}] {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Tool definitions ─────────────────────────────────────────


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_context",
            description=(
                "Semantic search across stored conversation contexts. "
                "Returns previews (snippet + metadata). "
                "Use get_context_detail for full content. "
                "Гибридный поиск: keyword ILIKE для коротких запросов (1-3 слова), "
                "semantic search (fastembed + cosine similarity) для длинных. "
                "Поддерживает iRacing-терминологию (сетап, подвеска, шины и т.д.)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Filter by project name, e.g. iRacing-Analyzer, CacheProxy"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5, max 20)",
                        "default": 5,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_context_detail",
            description=(
                "Get full context detail by ID, including complete content. "
                "Use after search_context to read a relevant entry in full."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "context_id": {
                        "type": "string",
                        "description": "Context UUID (from search_context results)",
                    },
                },
                "required": ["context_id"],
            },
        ),
        types.Tool(
            name="get_session",
            description=(
                "Get session details with message snippets (200 chars each). "
                "Use get_session_message for full message content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        types.Tool(
            name="list_sessions",
            description="List chat sessions with optional filters (project, status). Lightweight — no message content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Filter by project name",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status: active, archived",
                        "enum": ["active", "archived"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 100)",
                        "default": 20,
                        "maximum": 100,
                    },
                },
            },
        ),
        types.Tool(
            name="get_recent_contexts",
            description=(
                "Get most recent context entries without a search query. "
                "Returns previews (snippet + metadata). "
                "Use get_context_detail for full content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Filter by project name",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5, max 20)",
                        "default": 5,
                        "maximum": 20,
                    },
                },
            },
        ),
    ]


# ── Tool call handler ────────────────────────────────────────


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    db = SessionLocal()
    try:
        if name == "search_context":
            return _handle_search(db, arguments)
        elif name == "get_context_detail":
            return _handle_context_detail(db, arguments)
        elif name == "get_session":
            return _handle_get_session(db, arguments)
        elif name == "list_sessions":
            return _handle_list_sessions(db, arguments)
        elif name == "get_recent_contexts":
            return _handle_recent_contexts(db, arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
    finally:
        db.close()


# ── Tool implementations ─────────────────────────────────────


def _handle_search(db: SessionLocal, args: dict) -> list[types.TextContent]:
    query = args["query"]
    project = args.get("project")
    limit = min(args.get("limit", 5), 20)

    results = context_routes._hybrid_search(query, db, project=project, limit=limit)

    if not results:
        return [types.TextContent(type="text", text="Nothing found.")]

    formatted = [_format_context_preview(ctx, score) for ctx, score in results]
    return [
        types.TextContent(
            type="text",
            text=f"Search results for '{query}':\n\n" + "\n\n---\n\n".join(formatted),
        )
    ]


def _handle_context_detail(db: SessionLocal, args: dict) -> list[types.TextContent]:
    context_id = UUID(args["context_id"])
    ctx = db.get(Context, context_id)
    if not ctx:
        return [types.TextContent(type="text", text="Context not found.")]

    return [types.TextContent(type="text", text=_format_context_detail(ctx))]


def _handle_get_session(db: SessionLocal, args: dict) -> list[types.TextContent]:
    session_id = UUID(args["session_id"])
    session = db.get(Session, session_id)
    if not session:
        return [types.TextContent(type="text", text="Session not found.")]

    messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return [types.TextContent(type="text", text=_format_session(session, messages))]


def _handle_list_sessions(db: SessionLocal, args: dict) -> list[types.TextContent]:
    q = db.query(Session)
    if args.get("project"):
        q = q.filter(Session.project == args["project"])
    if args.get("status"):
        q = q.filter(Session.status == args["status"])
    limit = min(args.get("limit", 20), 100)
    sessions = q.order_by(Session.updated_at.desc()).limit(limit).all()

    if not sessions:
        return [types.TextContent(type="text", text="No sessions found.")]

    lines = [f"Sessions ({len(sessions)}):", ""]
    for s in sessions:
        msg_count = db.query(Message).filter(Message.session_id == s.id).count()
        ctx_count = db.query(Context).filter(Context.session_id == s.id).count()
        lines.append(
            f"- {s.id} | {s.title or 'Untitled'} | {s.project or '-'} | "
            f"{s.status} | {msg_count} msgs, {ctx_count} ctx | "
            f"{s.updated_at.strftime('%Y-%m-%d %H:%M')}"
        )
    return [types.TextContent(type="text", text="\n".join(lines))]


def _handle_recent_contexts(db: SessionLocal, args: dict) -> list[types.TextContent]:
    project = args.get("project")
    limit = min(args.get("limit", 5), 20)

    q = db.query(Context).filter(Context.embedding.isnot(None))
    if project:
        q = q.join(Context.session).filter(Session.project == project)
    contexts = q.order_by(Context.created_at.desc()).limit(limit).all()

    if not contexts:
        return [types.TextContent(type="text", text="No contexts found.")]

    formatted = [_format_context_preview(ctx) for ctx in contexts]
    return [
        types.TextContent(
            type="text",
            text="Recent contexts:\n\n" + "\n\n---\n\n".join(formatted),
        )
    ]


# ── Entry point ──────────────────────────────────────────────


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="cacheproxy",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
