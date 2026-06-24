"""Add a 'session title' context entry for every session with a meaningful title.

For each session where title is non-empty and not "Untitled", creates one
context entry with:
  - session_id: the session's ID
  - content: just the session title
  - keywords: the session's project name
  - embedding: generated via EmbeddingService.embed(title)
  - summary: "session title"

Skips sessions where a context with summary='session title' already exists.

Usage: python scripts/add_summary_contexts.py
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models.context import Context
from app.models.session import Session
from app.services import EmbeddingService


def main():
    print("=" * 60)
    print("Add session-summary context entries")
    print("=" * 60)

    # Pre-load embedding model
    print("\nLoading embedding model...")
    try:
        EmbeddingService._get_model()
        print("  ✓ Model loaded")
    except Exception as e:
        print(f"  ✗ Model load failed: {e}")
        return

    db = SessionLocal()
    try:
        # Find all sessions with meaningful titles
        sessions = (
            db.query(Session)
            .filter(
                Session.title.isnot(None),
                Session.title != "",
                Session.title != "Untitled",
            )
            .all()
        )
        print(f"\nFound {len(sessions)} sessions with meaningful titles.")

        # Collect session IDs that already have a 'session title' summary context
        existing_ids = set(
            row[0]
            for row in db.query(Context.session_id).filter(
                Context.summary == "session title"
            )
        )
        print(f"  {len(existing_ids)} already have a 'session title' context entry.")

        stats = {"created": 0, "skipped": 0, "errors": 0}

        for session in sessions:
            if session.id in existing_ids:
                stats["skipped"] += 1
                continue

            title = session.title.strip()
            if not title:
                stats["skipped"] += 1
                continue

            # Generate embedding from title
            try:
                embedding = EmbeddingService.embed(title)
            except Exception as e:
                print(f"  ✗ Embedding failed for session {session.id}: {e}")
                stats["errors"] += 1
                continue

            ctx = Context(
                session_id=session.id,
                content=title,
                keywords=session.project or "",
                embedding=embedding,
                summary="session title",
            )
            db.add(ctx)
            stats["created"] += 1

            if stats["created"] % 50 == 0:
                db.flush()
                print(f"  ... {stats['created']} created so far")

        db.commit()
        print(f"\nDone. {stats['created']} contexts created.")
        print(f"  Created: {stats['created']}")
        print(f"  Skipped (already exists): {stats['skipped']}")
        print(f"  Errors: {stats['errors']}")
    except Exception as e:
        db.rollback()
        print(f"\n  ✗ Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
