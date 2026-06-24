"""
Суммаризатор контекста для архивации сессий.

Режимы:
1. Concatenation (по умолч.) — склеивает все сообщения сессии в 1 контекст.
   Никаких ключей не нужно.
2. LLM (опционально) — если задан LLM_API_KEY, вместо склейки делает
   интеллектуальное саммари через OpenAI-compatible API.

Allгда возвращает {title, summary, keywords}. Без ошибок.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.utils import strip_thinking


def _clean_text(text: str | None) -> str:
    """Strip thinking tags and collapse whitespace."""
    return strip_thinking(text or "").strip()


def _concat_summary(messages: list[tuple[str, str]]) -> str:
    """Concatenate all messages into one text block (no LLM needed)."""
    parts = []
    for role, content in messages:
        # Skip tool messages — they break API providers (no tool_calls parent)
        if role == 'tool':
            continue
        text = _clean_text(content)
        if len(text) < 15:
            continue
        parts.append(text)
    return "\n\n".join(parts)


class Summarizer:
    """Conversation summarizer with optional LLM enhancement.

    Always returns a result — no API key required.
    LLM is used only if LLM_API_KEY is set in .env.
    """

    def __init__(self):
        self.api_key = settings.llm_api_key
        self.llm_enabled = settings.llm_enabled and bool(self.api_key)
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.max_tokens = settings.llm_max_tokens

    def summarize(
        self,
        messages: list[tuple[str, str]],
        session_title: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Summarize a conversation.

        Args:
            messages: list of (role, content) tuples.
            session_title: original session title (used as fallback).
            project: project name (used as keywords).

        Returns:
            {"title": str, "summary": str, "keywords": str}
            Всегда возвращает результат, даже пустой.
        """
        if not messages:
            return {
                "title": session_title or "",
                "summary": "",
                "keywords": project or "",
            }

        # Build plain-text conversation (always needed for concat fallback)
        all_texts = []
        for role, content in messages:
            # Skip tool messages — they break API providers (no tool_calls parent)
            if role == 'tool':
                continue
            text = _clean_text(content)
            if len(text) < 15:
                continue
            all_texts.append(text)

        if not all_texts:
            return {
                "title": session_title or "",
                "summary": "",
                "keywords": project or "",
            }

        # Try LLM if enabled
        if self.llm_enabled:
            llm_result = self._llm_summarize(messages, all_texts)
            if llm_result:
                return llm_result

        # Fallback: concatenate all messages into one context
        consolidated = "\n\n".join(all_texts)
        # Truncate to avoid bloating the DB
        if len(consolidated) > 50000:
            consolidated = consolidated[:50000] + "\n\n...[truncated]"

        return {
            "title": session_title or "Archived session",
            "summary": consolidated,
            "keywords": project or "",
        }

    def _llm_summarize(
        self,
        raw_messages: list[tuple[str, str]],
        cleaned_texts: list[str],
    ) -> dict[str, Any] | None:
        """Call LLM API for smart summarization. Returns None on failure."""
        conversation = self._format_for_llm(raw_messages)
        if not conversation:
            return None

        try:
            import httpx

            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": _LLM_PROMPT},
                            {"role": "user", "content": conversation},
                        ],
                        "temperature": 0.3,
                        "max_tokens": self.max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                result = json.loads(content)

                title = (result.get("title") or "").strip()
                summary = (result.get("summary") or "").strip()
                keywords_raw = result.get("keywords", [])

                if isinstance(keywords_raw, list):
                    keywords = ", ".join(k.strip() for k in keywords_raw if k.strip())
                else:
                    keywords = str(keywords_raw).strip()

                return {
                    "title": title,
                    "summary": summary,
                    "keywords": keywords,
                }
        except Exception as e:
            print(f"⚠ LLM summarization failed, using concat fallback: {e}")
            return None

    def _format_for_llm(self, messages: list[tuple[str, str]]) -> str:
        """Format messages for LLM prompt (with role labels)."""
        parts = []
        for role, content in messages:
            # Skip tool messages — they break API providers (no tool_calls parent)
            if role == 'tool':
                continue
            text = _clean_text(content)
            if len(text) < 15:
                continue
            if len(text) > 4000:
                text = text[:4000] + " ...[truncated]"
            parts.append(f"[{role}] {text}")
        result = "\n\n".join(parts)
        if len(result) > 100000:
            result = result[:100000] + "\n\n...[truncated]"
        return result


_LLM_PROMPT = """Ты — технический суммаризатор диалогов. Ниже лог чата между пользователем и AI-ассистентом.
Верни ТОЛЬКО JSON:

{
  "title": "Заголовок (макс 8 слов, отражает суть)",
  "summary": "Техническое саммари (2-4 абзаца): что обсуждали, какие проблемы решали, какие решения приняли, какие файлы/код меняли, результат",
  "keywords": ["слово1", "слово2", "слово3", ...]
}

Правила:
- Не используй имена участников, только технические детали
- Если в диалоге есть код/команды — упомяни ключевые
- keywords: 5-10 релевантных терминов"""
