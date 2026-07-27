"""Embedding service — generates and searches vector embeddings."""

from functools import lru_cache
from typing import Sequence

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session as SASession

from app.config import settings


class EmbeddingService:
    """Singleton embedding service using fastembed."""

    _model = None

    _cache_dir = None

    @classmethod
    def _get_cache_dir(cls) -> str:
        """Return a stable cache directory for the embedding model."""
        if cls._cache_dir is None:
            from pathlib import Path

            cls._cache_dir = str(Path(__file__).parent.parent / ".model_cache")
        return cls._cache_dir

    @classmethod
    def _get_model(cls):
        """Lazy-load the embedding model (loaded once)."""
        if cls._model is None:
            from fastembed import TextEmbedding

            cls._model = TextEmbedding(
                model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                cache_dir=cls._get_cache_dir(),
                providers=["CPUExecutionProvider"],
            )
        return cls._model

    @classmethod
    def embed(cls, text: str) -> list[float]:
        """Generate embedding vector for a single text string."""
        model = cls._get_model()
        vectors = list(model.embed([text]))
        return vectors[0].tolist()

    @classmethod
    def embed_batch(cls, texts: Sequence[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts at once."""
        model = cls._get_model()
        return [v.tolist() for v in model.embed(texts)]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors."""
        aa = np.array(a, dtype=np.float64)
        bb = np.array(b, dtype=np.float64)
        return float(np.dot(aa, bb) / (np.linalg.norm(aa) * np.linalg.norm(bb) + 1e-10))

    @classmethod
    def search(
        cls,
        query: str,
        db: SASession,
        limit: int = 10,
        project: str | None = None,
        base_query=None,
    ) -> list[tuple]:
        """
        Search contexts by semantic similarity.

        Args:
            base_query: Optional pre-built SQLAlchemy query to use instead
                        of the default Context query. Useful for filtering
                        by context type (e.g. session titles only).

        Returns list of (Context, score) tuples ordered by relevance.
        Uses brute-force cosine similarity over all stored embeddings.
        """
        from app.models.context import Context

        query_vec = cls.embed(query)

        # Use provided base_query or build default
        from app.models.session import Session as SessionModel

        if base_query is not None:
            q = base_query
            if project:
                q = q.join(Context.session).filter(SessionModel.project == project)
        contexts: list[Context] = q.all()

        scored = []
        for ctx in contexts:
            # Parse PostgreSQL float array
            db_vec = cls._parse_pg_array(ctx.embedding)
            if db_vec is None:
                continue
            score = cls.cosine_similarity(query_vec, db_vec)
            scored.append((ctx, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    @staticmethod
    def _parse_pg_array(val) -> list[float] | None:
        """Parse a float array returned by PostgreSQL into a Python list."""
        if val is None:
            return None
        # pgvector/psycopg returns list directly
        if isinstance(val, (list, tuple)):
            return [float(v) for v in val]
        # If it comes as a string (edge case), parse it
        if isinstance(val, str):
            import json

            cleaned = val.replace("{", "[").replace("}", "]")
            return json.loads(cleaned)
        return None
