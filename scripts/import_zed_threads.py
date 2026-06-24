"""Import all Zed assistant threads into CacheProxy.

Reads threads from Zed's local SQLite database, decompresses zstd data,
and imports conversations into CacheProxy as:
  - Sessions (via API)
  - Messages (via direct DB insert — avoids per-message auto-context)
  - Contexts (exchange pairs user+assistant, combined for rich embeddings)

Usage: python scripts/import_zed_threads.py
"""

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import zstandard as zs

CACHEPROXY_BASE = "http://192.168.101.211:8100"
ZED_THREADS_DB = str(Path.home() / ".local/share/zed/threads/threads.db")
ZED_MAIN_DB = str(Path.home() / ".local/share/zed/db/0-stable/db.sqlite")

# Short responses that add no topical value — skip standalone contexts
GENERIC_RESPONSES = {
    "понял",
    "готово",
    "ок",
    "ok",
    "хорошо",
    "ладно",
    "понял.",
    "отлично!",
    "чисто.",
    "продолжай",
    "супер",
    "ага",
}


def binary_uuid_to_str(b: bytes) -> str:
    return str(uuid.UUID(bytes_le=b))


def extract_text(content_blocks: list) -> str:
    """Extract user-visible text, skipping internal ToolUse blocks."""
    texts = []
    for block in content_blocks:
        if "Text" in block:
            texts.append(block["Text"])
    return "\n".join(texts)


def get_project_name(folder_paths: str | None) -> str | None:
    if not folder_paths or not folder_paths.strip():
        return None
    paths = folder_paths.replace("\\t", "\t").split("\t")
    for p in paths:
        p = p.strip()
        if p:
            name = Path(p).name
            if name:
                return name
    return None


def is_generic(text: str | None) -> bool:
    """Check if text is a short generic response with no topical value."""
    if not text:
        return False
    t = text.strip().lower().rstrip(".!").strip()
    return t in GENERIC_RESPONSES


def group_into_exchanges(messages: list[dict]) -> list[dict]:
    """Group flat message list into user+assistant exchange pairs.

    Returns list of {user, assistant} dicts.
    Unpaired messages (e.g. last user without response) are included as {user}.
    Consecutive assistant messages (tool calls) are merged with preceding user.
    """
    exchanges = []
    current = {}

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            # Save previous exchange if exists
            if current.get("user") is not None:
                exchanges.append(current)
            current = {"user": content, "assistant": None}
        elif role == "assistant":
            if current.get("user") is not None:
                if current["assistant"] is None:
                    current["assistant"] = content
                else:
                    # Consecutive assistant — append
                    current["assistant"] += "\n" + content

    # Flush last exchange
    if current.get("user") is not None:
        exchanges.append(current)

    return exchanges


def context_text(title: str | None, exchange: dict) -> str:
    """Build context text from exchange pair + thread title."""
    parts = []
    if title and title.strip() and title != "Untitled":
        parts.append(f"[{title}]")
    if exchange.get("user"):
        parts.append(f"[user] {exchange['user']}")
    if exchange.get("assistant"):
        parts.append(f"[assistant] {exchange['assistant']}")
    return "\n".join(parts)


def should_skip_exchange(title: str | None, exchange: dict) -> bool:
    """Skip exchanges with no topical value."""
    combined = ""
    if exchange.get("user"):
        combined += exchange["user"]
    if exchange.get("assistant"):
        combined += " " + exchange["assistant"]
    combined = combined.strip()

    # Skip empty
    if not combined:
        return True

    # Skip very short generic — but only if no meaningful title
    if len(combined) < 30 and title in (None, "", "Untitled"):
        return True

    # Skip if both user and assistant are generic
    user_gen = is_generic(exchange.get("user", ""))
    asst_gen = is_generic(exchange.get("assistant", ""))
    if user_gen and asst_gen and len(combined) < 60:
        return True

    return False


def main():
    print("=" * 60)
    print("Importing Zed assistant threads into CacheProxy")
    print("=" * 60)

    # Setup DB connection for direct inserts
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from sqlalchemy import text

    from app.database import SessionLocal, engine
    from app.models.context import Context as ContextModel
    from app.models.message import Message as MessageModel
    from app.services import EmbeddingService

    # Connect to Zed databases
    threads_conn = sqlite3.connect(ZED_THREADS_DB)
    main_conn = sqlite3.connect(ZED_MAIN_DB)

    # Build thread map from sidebar_threads
    print("\nBuilding thread map...")
    thread_map = {}
    rows = main_conn.execute(
        "SELECT thread_id, title, folder_paths FROM sidebar_threads"
    ).fetchall()
    for row in rows:
        tid = binary_uuid_to_str(row[0])
        thread_map[tid] = {"title": row[1], "folder_paths": row[2]}
    print(f"  {len(thread_map)} threads in sidebar_threads")

    # Get all threads from threads.db
    all_threads = threads_conn.execute(
        "SELECT id, summary, data, folder_paths FROM threads ORDER BY created_at ASC"
    ).fetchall()
    print(f"  {len(all_threads)} threads in threads.db")

    # Check CacheProxy health
    client = httpx.Client(base_url=CACHEPROXY_BASE, timeout=30.0)
    try:
        r = client.get("/health")
        r.raise_for_status()
        print(f"  CacheProxy: {r.json()}")
    except Exception as e:
        print(f"  ERROR: CacheProxy not reachable: {e}")
        return

    # Pre-load embedding model once
    print("\nLoading embedding model...")
    try:
        EmbeddingService._get_model()
        print("  ✓ Model loaded")
    except Exception as e:
        print(f"  ⚠ Model load failed: {e}")

    stats = {"sessions": 0, "messages": 0, "contexts": 0, "errors": 0, "skipped": 0}

    for i, (tid, summary, data_blob, folder_paths) in enumerate(all_threads):
        print(f"\n[{i + 1}/{len(all_threads)}] Thread {tid[:16]}...")

        if len(data_blob) < 100:
            stats["skipped"] += 1
            continue

        # Decompress
        try:
            dctx = zs.ZstdDecompressor()
            reader = dctx.stream_reader(data_blob)
            thread_data = json.loads(reader.read())
        except Exception as e:
            print(f"  ERROR decompress: {e}")
            stats["errors"] += 1
            continue

        # Metadata
        info = thread_map.get(tid, {})
        title = (
            info.get("title")
            or (thread_data.get("title") or "").strip()
            or summary
            or "Untitled"
        )
        project = get_project_name(info.get("folder_paths") or folder_paths)

        # Parse messages
        raw_messages = thread_data.get("messages", [])
        parsed = []
        for msg in raw_messages:
            if isinstance(msg, str):
                if msg.strip():
                    parsed.append({"role": "user", "content": msg.strip()})
                continue
            role = list(msg.keys())[0]
            blocks = msg[role].get("content", [])
            text = extract_text(blocks)
            if text.strip():
                parsed.append(
                    {
                        "role": "user" if role == "User" else "assistant",
                        "content": text.strip(),
                    }
                )

        if not parsed:
            stats["skipped"] += 1
            continue

        # Group into exchanges
        exchanges = group_into_exchanges(parsed)
        exchanges = [ex for ex in exchanges if not should_skip_exchange(title, ex)]

        if not exchanges:
            print(f"  SKIP: no substantive exchanges after filtering")
            stats["skipped"] += 1
            continue

        print(f"  Title: {title[:60]}")
        print(f"  Project: {project}")
        print(f"  Messages: {len(parsed)}, Exchanges: {len(exchanges)}")

        # Create session via API
        try:
            r = client.post(
                "/sessions",
                json={
                    "title": title[:255],
                    "project": project[:255] if project else None,
                },
            )
            r.raise_for_status()
            session_id = r.json()["id"]
            stats["sessions"] += 1
        except Exception as e:
            print(f"  ERROR creating session: {e}")
            stats["errors"] += 1
            continue

        # Insert messages directly into DB (bypass API to avoid auto-context)
        db = SessionLocal()
        try:
            for pm in parsed:
                msg = MessageModel(
                    id=uuid.uuid4(),
                    session_id=uuid.UUID(session_id),
                    role=pm["role"],
                    content=pm["content"],
                    created_at=datetime.now(timezone.utc),
                )
                db.add(msg)
                stats["messages"] += 1

            # Create context entries from exchanges (with embeddings)
            for ex in exchanges:
                ctx_text = context_text(title, ex)
                ctx = ContextModel(
                    id=uuid.uuid4(),
                    session_id=uuid.UUID(session_id),
                    content=ctx_text,
                    keywords=project or "",
                    token_count=None,
                    created_at=datetime.now(timezone.utc),
                )
                # Generate embedding
                try:
                    ctx.embedding = EmbeddingService.embed(ctx_text)
                except Exception as embed_err:
                    print(f"    ⚠ Embedding failed: {embed_err}")
                db.add(ctx)
                stats["contexts"] += 1

            db.commit()
            print(f"  → {len(parsed)} msgs, {len(exchanges)} contexts")
        except Exception as e:
            db.rollback()
            print(f"  ERROR DB insert: {e}")
            stats["errors"] += 1
        finally:
            db.close()

    threads_conn.close()
    main_conn.close()

    print("\n" + "=" * 60)
    print("IMPORT COMPLETE")
    print("=" * 60)
    print(f"  Sessions:  {stats['sessions']}")
    print(f"  Messages:  {stats['messages']}")
    print(f"  Contexts:  {stats['contexts']}")
    print(f"  Skipped:   {stats['skipped']}")
    print(f"  Errors:    {stats['errors']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
