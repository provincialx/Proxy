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
    search_type: str = "auto",
    db: SASession = Depends(get_db),
):
    """Semantic search across context entries using vector embeddings.

    Parameters:
    - search_type: "sessions" — search only session titles (best for finding topics)
                   "messages" — search individual messages (granular)
                   "auto" — try sessions first, fall back to messages if low scores
    If query is empty, returns the most recent contexts (no embedding needed).
    """
    if not query.strip():
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

    TRANSLATIONS = {
        "сетап": ["setup"],
        "настройка": ["setup"],
        "настройки": ["setup"],
        "подвеска": ["suspension"],
        "lambo": ["lamborghini"],
        "lamborghini": ["lambo"],
        "setup": ["сетап", "настройка", "настройки"],
    }

    def _get_keyword_ids(base_q) -> set:
        """Find context IDs matching query terms via ILIKE."""
        from sqlalchemy import or_

        terms = set()
        for w in query.lower().split():
            if len(w) > 2:
                terms.add(w)
        for w in list(terms):
            if w in TRANSLATIONS:
                terms.update(TRANSLATIONS[w])

        if not terms:
            return set()

        q = base_q.filter(Context.embedding.isnot(None))
        if project:
            q = q.join(Context.session).filter(Session.project == project)

        filters = [Context.content.ilike(f"%{w}%") for w in terms]
        return {ctx.id for ctx in q.filter(or_(*filters)).all()}

    def _build_results(ctx_score_list) -> list[ContextOut]:
        return [
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
            for ctx, score in ctx_score_list
        ]

    # Build base query for scoping by project
    base_query = db.query(Context).filter(Context.embedding.isnot(None))

    if search_type in ("sessions", "auto"):
        # Search only session-title contexts (summary='session title')
        title_q = base_query.filter(Context.summary == "session title")
        if project:
            title_q = title_q.join(Context.session).filter(Session.project == project)

        # Semantic on titles
        title_results = EmbeddingService.search(
            query,
            db,
            limit=limit * 3,
            project=project,
            base_query=title_q,
        )

        # Hybrid: keyword boost
        kw_ids = _get_keyword_ids(
            db.query(Context).filter(Context.summary == "session title")
        )
        seen = set()
        merged = []
        for ctx, score in title_results:
            seen.add(ctx.id)
            if ctx.id in kw_ids:
                merged.append((ctx, max(score, 0.65)))
            else:
                merged.append((ctx, score))
        for cid in kw_ids:
            if cid not in seen:
                ctx = db.get(Context, cid)
                if ctx:
                    merged.append((ctx, 0.65))

        merged.sort(key=lambda x: x[1], reverse=True)
        title_results = merged[:limit]

        if search_type == "sessions":
            return ContextSearchResult(
                query=query, results=_build_results(title_results)
            )

        # "auto": if best title score >= 0.5, return title results
        if title_results and title_results[0][1] >= 0.5:
            return ContextSearchResult(
                query=query, results=_build_results(title_results)
            )

    # Fallback: search all message-level contexts (hybrid)
    results = EmbeddingService.search(query, db, limit=limit * 2, project=project)
    kw_ids = _get_keyword_ids(base_query)
    if kw_ids:
        seen = set()
        merged = []
        for ctx, score in results:
            seen.add(ctx.id)
            if ctx.id in kw_ids:
                merged.append((ctx, max(score, 0.55)))
            else:
                merged.append((ctx, score))
        for cid in kw_ids:
            if cid not in seen:
                ctx = db.get(Context, cid)
                if ctx:
                    merged.append((ctx, 0.55))
        merged.sort(key=lambda x: x[1], reverse=True)
        results = merged[:limit]

    return ContextSearchResult(query=query, results=_build_results(results))


@router.delete("/{context_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_context(context_id: UUID, db: SASession = Depends(get_db)):
    """Delete a specific context entry."""
    ctx = db.get(Context, context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    db.delete(ctx)
    db.commit()
