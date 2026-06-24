#!/usr/bin/env python3
"""
Sync Agent — локальный демон, который читает архивированные треды из SQLite БД Zed
и отправляет их в CacheProxy.

Использование:
    python sync_agent.py discover                        # показать схему БД
    python sync_agent.py sync                            # один проход
    python sync_agent.py daemon                          # фоновый режим (каждые N сек)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Add app dir to path for utils
sys.path.insert(0, str(Path(__file__).parent))
from app.utils import strip_thinking

# ── Конфигурация ────────────────────────────────────────────

DEFAULT_CACHEPROXY_URL = "http://127.0.0.1:8100"
DEFAULT_POLL_INTERVAL = 60
SENT_MARKERS_FILE = Path.home() / ".cacheproxy_sent_markers.json"


def _zed_data_dir() -> Path | None:
    """Returns path to Local/Zed or None if not found."""
    for var in ("LOCALAPPDATA", "HOME"):
        base = os.environ.get(var)
        if not base:
            continue
        if var == "LOCALAPPDATA":
            p = Path(base) / "Zed"
        else:
            p = Path(base) / ".local/share/zed"
        if p.exists():
            return p
    return None


def _db_paths() -> tuple[Path | None, Path | None]:
    """
    Returns (sidebar_db, threads_db) paths.
    sidebar_db = 0-stable/db.sqlite (has archived flag)
    threads_db = threads/threads.db (has actual message content, zstd)
    """
    base = _zed_data_dir()
    if not base:
        return None, None
    sidebar = base / "db" / "0-stable" / "db.sqlite"
    threads = base / "threads" / "threads.db"
    return (
        sidebar if sidebar.exists() else None,
        threads if threads.exists() else None,
    )


def _load_sent_cache() -> set[str]:
    if SENT_MARKERS_FILE.exists():
        try:
            data = json.loads(SENT_MARKERS_FILE.read_text())
            return set(data.get("sent", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def _save_sent_cache(sent: set[str]) -> None:
    SENT_MARKERS_FILE.write_text(
        json.dumps({"sent": list(sent)}, ensure_ascii=False, indent=2)
    )


def _discover_schema(db_path: Path) -> None:
    """Print schema of a database."""
    if not db_path.exists():
        print(f"  File not found: {db_path}")
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r["name"] for r in cur.fetchall()]
    print(f"\n  📁 {db_path}")
    for t in tables:
        cur.execute(f'PRAGMA table_info("{t}")')
        cols = cur.fetchall()
        print(f"\n    📋 {t}")
        for c in cols:
            print(f"      {c['name']}: {c['type']}")
    conn.close()


# ── Core: extract archived threads ────────────────────────────


def _decompress_thread_data(data: bytes) -> dict[str, Any] | None:
    """Decompress zstd data from threads.db and parse JSON.

    Uses a streaming reader to handle multi-frame zstd content
    (some threads produce ~88MB decompressed from 6.6MB compressed).
    """
    try:
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        reader = dctx.stream_reader(data)
        decompressed = reader.read()
        return json.loads(decompressed.decode("utf-8"))
    except Exception as e:
        print(f"    ⚠ Failed to decompress/parse thread data: {e}")
        return None


def _parse_messages(thread_data: dict[str, Any]) -> list[dict[str, str]]:
    """
    Parse thread JSON into list of {role, content}.
    Each message preserves its original role (user/assistant/system).
    """
    raw_messages = thread_data.get("messages", [])
    result = []
    for msg in raw_messages:
        if "User" in msg:
            role = "user"
            parts = msg["User"].get("content", [])
        elif "Agent" in msg:
            role = "assistant"
            parts = msg["Agent"].get("content", [])
        elif "System" in msg:
            role = "system"
            parts = msg["System"].get("content", [])
        else:
            continue

        text_parts = []
        for part in parts:
            if isinstance(part, dict):
                if "Text" in part:
                    text_parts.append(part["Text"])
                elif "ToolUse" in part:
                    # Skip tool calls — noise for search
                    continue
                elif "ToolResult" in part or "Tool" in part:
                    # Skip tool results — break API providers (no tool_calls parent)
                    continue
                elif "Thinking" in part:
                    # Skip model thinking blocks — noise for search
                    continue
                elif "Mention" in part:
                    # Skip file/mention blocks — noise for search
                    continue
                elif "Image" in part:
                    # Skip image blocks — noise for search
                    continue
                else:
                    text_parts.append(json.dumps(part, ensure_ascii=False))
            elif isinstance(part, str):
                text_parts.append(part)
            else:
                text_parts.append(str(part))

        text = " ".join(text_parts).strip()
        if text:
            result.append({"role": role, "content": text})
    return result


def get_available_projects() -> list[str]:
    """Scan archived threads and return distinct project names."""
    sidebar_db, _ = _db_paths()
    if not sidebar_db:
        return []

    s_conn = sqlite3.connect(f"file:{sidebar_db}?mode=ro", uri=True)
    s_conn.row_factory = sqlite3.Row
    s_cur = s_conn.cursor()
    s_cur.execute("SELECT folder_paths FROM sidebar_threads WHERE archived = 1")
    projects: set[str] = set()
    for r in s_cur.fetchall():
        folder_paths = (r["folder_paths"] or "").strip()
        if folder_paths:
            project = Path(folder_paths.split("\n")[0].strip()).name or "zed"
        else:
            project = "zed"
        projects.add(project)
    s_conn.close()
    return sorted(projects)


def _get_archived_threads(projects: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Read archived threads from Zed's SQLite databases.

    Returns list of dicts with:
      - id: thread UUID (from sidebar_threads.session_id)
      - title: thread title
      - project: project name (derived from folder_paths)
      - content: formatted conversation text
      - created_at, updated_at: timestamps
    """
    sidebar_db, threads_db = _db_paths()
    if not sidebar_db or not threads_db:
        print("  ⚠ Zed databases not found")
        return []

    # ── 1. Get archived threads from sidebar ──
    s_conn = sqlite3.connect(f"file:{sidebar_db}?mode=ro", uri=True)
    s_conn.row_factory = sqlite3.Row
    s_cur = s_conn.cursor()
    s_cur.execute(
        "SELECT session_id, title, folder_paths, folder_paths_order, archived, "
        "created_at, updated_at, interacted_at "
        "FROM sidebar_threads WHERE archived = 1"
    )
    sidebar_rows = [dict(r) for r in s_cur.fetchall()]
    s_conn.close()
    print(f"  Archived threads in sidebar: {len(sidebar_rows)}")

    if not sidebar_rows:
        return []

    # ── 2. Get thread content from threads.db ──
    t_conn = sqlite3.connect(f"file:{threads_db}?mode=ro", uri=True)
    t_conn.row_factory = sqlite3.Row
    t_cur = t_conn.cursor()

    # Build lookup: id -> (summary, data)
    t_cur.execute("SELECT id, summary, data FROM threads")
    thread_data_map: dict[str, dict[str, Any]] = {}
    for r in t_cur.fetchall():
        d = dict(r)
        thread_data_map[d["id"]] = d
    t_conn.close()
    print(f"  Thread content entries: {len(thread_data_map)}")

    # ── 3. Join and format ──
    results = []
    for row in sidebar_rows:
        sid = row["session_id"]
        if not sid:
            continue

        thread_entry = thread_data_map.get(sid)
        if not thread_entry:
            print(f"    ⚠ No content for thread {sid} ({row['title']})")
            continue

        # Decompress
        raw_data = thread_entry["data"]
        if not raw_data:
            continue
        thread_data = _decompress_thread_data(raw_data)
        if not thread_data:
            continue

        # Parse messages — each with original role
        messages = _parse_messages(thread_data)

        # Determine project from folder_paths
        folder_paths = (row.get("folder_paths") or "").strip()
        project = "zed"
        if folder_paths:
            project = Path(folder_paths.split("\n")[0].strip()).name or "zed"

        results.append(
            {
                "id": sid,
                "title": row.get("title") or thread_data.get("title") or "Untitled",
                "project": project,
                "messages": messages,
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
            }
        )

    # Filter by projects if specified
    if projects:
        results = [r for r in results if r["project"] in projects]

    return results


# ── Send to CacheProxy ────────────────────────────────────────


def _send_to_cacheproxy(
    thread: dict[str, Any],
    url: str,
) -> bool:
    """Create session in CacheProxy, send content, archive."""
    try:
        with httpx.Client(base_url=url, timeout=120) as client:
            # 1. Create session
            r = client.post(
                "/sessions",
                json={
                    "title": thread["title"],
                    "project": thread["project"],
                },
            )
            if r.status_code not in (200, 201):
                print(f"    ☠ Create session failed: {r.status_code}")
                return False
            sid = r.json()["id"]

            # 2. Send each message individually with its original role
            for msg in thread["messages"]:
                # Cut thinking blocks — save space, reduce noise in search
                content = strip_thinking(msg["content"])
                if not content:
                    continue
                # Truncate extremely long messages (tool results etc)
                if len(content) > 40000:
                    content = content[:40000] + "\n\n... (truncated)"
                r = client.post(
                    "/messages",
                    json={
                        "session_id": sid,
                        "role": msg["role"],
                        "content": content,
                    },
                )
                if r.status_code not in (200, 201):
                    print(f"    ☠ Send message failed: {r.status_code}")
                    return False
                # Small delay to not hammer the API
                time.sleep(0.05)

            # 3. Archive session (consolidates into context for search)
            r = client.post(f"/sessions/{sid}/archive")
            if r.status_code not in (200, 201):
                print(f"    ☠ Archive failed: {r.status_code}")
                return False

            print(
                f"    ✓ {thread['title'][:60]} ({len(thread['messages'])} msg, {thread['project']})"
            )
            return True

    except httpx.ConnectError:
        print(f"    ☠ CacheProxy not reachable at {url}")
        return False
    except Exception as e:
        print(f"    ☠ Error: {e}")
        return False


# ── Commands ──────────────────────────────────────────────────


def cmd_discover(args: argparse.Namespace) -> None:
    """Show schemas of both Zed databases."""
    sidebar_db, threads_db = _db_paths()
    if not sidebar_db and not threads_db:
        print("❌ Zed data directory not found")
        base = _zed_data_dir()
        print(f"  Looked in: {base if base else 'LOCALAPPDATA/Zed'}")
        print("  Use --db-path to specify manually")
        return

    if sidebar_db:
        _discover_schema(sidebar_db)
    if threads_db:
        _discover_schema(threads_db)


def run_sync(
    url: str = DEFAULT_CACHEPROXY_URL,
    projects: list[str] | None = None,
) -> dict:
    """Run one sync pass. Returns {synced: int, total: int, errors: list}."""
    result: dict = {"synced": 0, "total": 0, "errors": []}

    try:
        r = httpx.get(f"{url}/health", timeout=5)
        if r.status_code != 200:
            result["errors"].append("CacheProxy not healthy")
            return result
    except Exception as e:
        result["errors"].append(f"CacheProxy not reachable: {e}")
        return result

    sent = _load_sent_cache()
    threads = _get_archived_threads(projects)
    result["total"] = len(threads)

    for t in threads:
        if t["id"] in sent:
            continue
        if _send_to_cacheproxy(t, url):
            sent.add(t["id"])
            _save_sent_cache(sent)
            result["synced"] += 1
        else:
            result["errors"].append(f"Failed: {t['title'][:60]}")

    return result


def cmd_sync(args: argparse.Namespace) -> None:
    """One sync pass (CLI wrapper)."""
    url = args.url or DEFAULT_CACHEPROXY_URL
    res = run_sync(url)
    print(f"  Synced {res['synced']} new thread(s) (total: {res['total']})")
    for e in res["errors"]:
        print(f"  ☠ {e}")


def cmd_daemon(args: argparse.Namespace) -> None:
    """Background mode."""
    interval = args.interval or DEFAULT_POLL_INTERVAL
    print(f"🔄 Sync daemon started (interval: {interval}s)")
    print(f"  CacheProxy: {args.url or DEFAULT_CACHEPROXY_URL}")
    print("  Ctrl+C to stop\n")

    while True:
        try:

            class OnceArgs:
                url = args.url

            cmd_sync(OnceArgs())
        except KeyboardInterrupt:
            print("\n🛑 Stopped")
            break
        except Exception as e:
            print(f"⚠ Daemon error: {e}")
        time.sleep(interval)


# ── CLI ──────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Agent — выгрузка архивных тредов из Zed в CacheProxy"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_CACHEPROXY_URL,
        help=f"CacheProxy URL (default: {DEFAULT_CACHEPROXY_URL})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="Show Zed database schemas")

    sync_parser = sub.add_parser("sync", help="One sync pass")
    sync_parser.add_argument("--once", action="store_true", help="(alias)")

    daemon_parser = sub.add_parser("daemon", help="Background mode")
    daemon_parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help="Poll interval in seconds (default: 60)",
    )

    args = parser.parse_args()

    if args.command == "discover":
        cmd_discover(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "daemon":
        cmd_daemon(args)


if __name__ == "__main__":
    main()
