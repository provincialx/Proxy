"""Context routes — store & search conversation context with embeddings."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session as SASession

from app.database import get_db
from app.models.context import Context
from app.models.session import Session
from app.schemas import ContextCreate, ContextOut, ContextSearchResult
from app.services import EmbeddingService
from app.utils import strip_thinking

router = APIRouter(prefix="/context", tags=["context"])

# iRacing-специфичная терминология для расширения запроса
TRANSLATIONS: dict[str, list[str]] = {
    "сетап": ["setup", "настройка", "настройки"],
    "настройка": ["setup", "настройки", "сетап"],
    "настройки": ["setup", "настройка", "сетап"],
    "подвеска": ["suspension", "подвески"],
    "подвески": ["suspension", "подвеска"],
    "lambo": ["lamborghini"],
    "lamborghini": ["lambo"],
    "setup": ["сетап", "настройка", "настройки"],
    "шин": ["tire", "tyre", "резин"],
    "резин": ["tire", "tyre"],
    "двигател": ["engine", "мотор", "движок"],
    "мотор": ["engine", "двигател", "движок"],
    "аэродинамик": ["aero", "крыло", "антикрыло", "прижим"],
    "прижим": ["aero", "downforce", "крыло", "антикрыло"],
}


def _expand_query(query: str) -> set[str]:
    """Expand query terms using TRANSLATIONS dict. Returns all search terms."""
    terms: set[str] = set()
    for w in query.lower().split():
        if len(w) > 2:
            terms.add(w)
            if w in TRANSLATIONS:
                terms.update(TRANSLATIONS[w])
    # Также ищем частичные совпадения ключей
    for key, vals in TRANSLATIONS.items():
        if any(key in t for t in terms):
            terms.update(vals)
        if any(v in terms for v in vals):
            terms.add(key)
    return terms


def _keyword_search(
    db: SASession,
    terms: set[str],
    project: str | None = None,
    limit: int = 10,
) -> list[tuple[Context, float]]:
    """Search contexts by keyword ILIKE with relevance scoring.

    Score = weighted by match density (more matches = higher).
    Exact word match > partial substring match.
    """
    if not terms:
        return []

    q = db.query(Context).filter(Context.content.isnot(None))
    if project:
        q = q.join(Context.session).filter(Session.project == project)

    # Build OR filters for all terms
    filters = []
    for w in terms:
        filters.append(Context.content.ilike(f"%{w}%"))
        filters.append(Context.keywords.ilike(f"%{w}%"))
        filters.append(Context.summary.ilike(f"%{w}%"))

    candidates: list[Context] = q.filter(or_(*filters)).all()

    scored: list[tuple[Context, float]] = []
    for ctx in candidates:
        content_lower = (ctx.content or "").lower()
        score = 0.0
        for w in terms:
            wl = w.lower()
            # Exact word match — highest weight
            if f" {wl} " in f" {content_lower} ":
                score += 0.3
            # Substring match — lower weight
            elif wl in content_lower:
                score += 0.15
            # Match in keywords — bonus
            if ctx.keywords and wl in ctx.keywords.lower():
                score += 0.1
            # Match in summary — bonus
            if ctx.summary and wl in ctx.summary.lower():
                score += 0.1
        if score > 0:
            scored.append((ctx, min(score, 1.0)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


@router.post("", response_model=ContextOut, status_code=status.HTTP_201_CREATED)
def create_context(body: ContextCreate, db: SASession = Depends(get_db)):
    """Store a context snippet from a conversation, auto-generates embedding."""
    session = db.get(Session, body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    clean_content = strip_thinking(body.content)

    # Generate embedding from content + keywords for better semantic matching
    embed_text = clean_content
    if body.keywords:
        embed_text = f"{clean_content} Keywords: {body.keywords}"
    embedding = EmbeddingService.embed(embed_text)

    ctx = Context(
        session_id=body.session_id,
        summary=body.summary,
        content=clean_content,
        keywords=body.keywords,
        embedding=embedding,
        token_count=body.token_count,
    )
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ctx


def _is_short_query(query: str) -> bool:
    """Short queries (1-3 words) — keyword search works better."""
    words = [w for w in query.strip().split() if len(w) > 2]
    return len(words) <= 3


def _build_results(ctx_score_list: list[tuple[Context, float]]) -> list[ContextOut]:
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


def _hybrid_search(
    query: str,
    db: SASession,
    project: str | None = None,
    limit: int = 10,
    summary_filter: str | None = None,
) -> list[tuple[Context, float]]:
    """Hybrid search: keyword boost on top of semantic results.

    Для коротких запросов (<=3 слов) — чисто keyword search.
    Для длинных — semantic search с keyword-бустом для точных совпадений.
    """
    terms = _expand_query(query)

    # Короткие запросы — только keyword
    if _is_short_query(query):
        kw_results = _keyword_search(db, terms, project=project, limit=limit)
        if kw_results:
            return kw_results
        # Fallback на semantic если keyword ничего не дал

    # Semantic search
    base_q = db.query(Context).filter(Context.embedding.isnot(None))
    if summary_filter:
        base_q = base_q.filter(Context.summary == summary_filter)

    semantic_results = EmbeddingService.search(
        query,
        db,
        limit=limit * 2,
        project=project,
        base_query=base_q,
    )

    # Keyword boost: точные совпадения получают буст
    kw_ids = {
        ctx.id for ctx, _ in _keyword_search(db, terms, project=project, limit=50)
    }
    if not terms or not kw_ids:
        return semantic_results[:limit]

    seen: set[UUID] = set()
    merged: list[tuple[Context, float]] = []
    for ctx, score in semantic_results:
        seen.add(ctx.id)
        if ctx.id in kw_ids:
            # Буст: +0.15 если семантический результат совпадает с keyword
            merged.append((ctx, score + 0.15))
        else:
            # Штраф: -0.1 если нет точного совпадения (для длинных запросов с бустом)
            merged.append((ctx, score - 0.1 if not _is_short_query(query) else score))

    # Добавляем keyword-результаты, которые не нашлись семантикой
    for ctx_id in kw_ids:
        if ctx_id not in seen:
            ctx = db.get(Context, ctx_id)
            if ctx:
                merged.append((ctx, 0.5))

    merged.sort(key=lambda x: x[1], reverse=True)
    return merged[:limit]


@router.get("/search", response_model=ContextSearchResult)
def search_context(
    query: str,
    project: str | None = None,
    limit: int = 10,
    search_type: str = "auto",
    db: SASession = Depends(get_db),
):
    """Search across context entries.

    - Короткие запросы (1-3 слова): keyword search с транслитерацией
    - Длинные запросы: semantic search + keyword boost
    - search_type="sessions": только session title контексты
    - search_type="messages": только сообщения
    - Если query пустой: последние контексты
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

    if search_type == "sessions":
        results = _hybrid_search(
            query, db, project=project, limit=limit, summary_filter="session title"
        )
    elif search_type == "messages":
        results = _hybrid_search(query, db, project=project, limit=limit)
    else:  # auto
        results = _hybrid_search(query, db, project=project, limit=limit)

    return ContextSearchResult(query=query, results=_build_results(results))


@router.delete("/{context_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_context(context_id: UUID, db: SASession = Depends(get_db)):
    """Delete a specific context entry."""
    ctx = db.get(Context, context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    db.delete(ctx)
    db.commit()
