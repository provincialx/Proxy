"""Context routes — store & search conversation context with embeddings."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session as SASession

from app.database import get_db
from app.models.context import Context
from app.models.session import Session
from app.schemas import ContextCreate, ContextOut, ContextSearchResult
from app.services import EmbeddingService

router = APIRouter(prefix="/context", tags=["context"])


@router.post("", response_model=ContextOut, status_code=status.HTTP_201_CREATED)
def create_context(body: ContextCreate, db: SASession = Depends(get_db)):
    """Store a context snippet from a conversation, auto-generates embedding."""
    session = db.get(Session, body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Generate embedding from content + keywords for better semantic matching
    embed_text = body.content
    if body.keywords:
        embed_text = f"{body.content} Keywords: {body.keywords}"
    embedding = EmbeddingService.embed(embed_text)

    ctx = Context(
        session_id=body.session_id,
        summary=body.summary,
        content=body.content,
        keywords=body.keywords,
        embedding=embedding,
        token_count=body.token_count,
    )
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ctx


@router.get("/search", response_model=ContextSearchResult)
def search_context(
    query: str,
    project: str | None = None,
    limit: int = 10,
    db: SASession = Depends(get_db),
):
    """Semantic search across context entries using vector embeddings.
    If query is empty, returns the most recent contexts (no embedding needed).
    """
    if not query.strip():
        # Empty query → show recent contexts (newest first)
        q = db.query(Context).filter(Context.embedding.isnot(None))
        if project:
            q = q.join(Context.session).filter(Session.project == project)
        recent = q.order_by(Context.created_at.desc()).limit(limit).all()
        return ContextSearchResult(
            query=query,
            results=[
                ContextOut(
                    id=ctx.id,
                    session_id=ctx.session_id,
                    summary=ctx.summary,
                    content=ctx.content,
                    keywords=ctx.keywords,
                    token_count=ctx.token_count,
                    created_at=ctx.created_at,
                )
                for ctx in recent
            ],
        )

    results = EmbeddingService.search(query, db, limit=limit, project=project)
    return ContextSearchResult(
        query=query,
        results=[
            ContextOut(
                id=ctx.id,
                session_id=ctx.session_id,
                summary=ctx.summary,
                content=ctx.content,
                keywords=ctx.keywords,
                token_count=ctx.token_count,
                created_at=ctx.created_at,
                score=round(score, 4),
            )
            for ctx, score in results
        ],
    )


@router.delete("/{context_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_context(context_id: UUID, db: SASession = Depends(get_db)):
    """Delete a specific context entry."""
    ctx = db.get(Context, context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    db.delete(ctx)
    db.commit()
