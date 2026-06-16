"""
LLM-суммаризатор диалогов для архивации сессий.

Вызывает OpenAI-compatible API и возвращает структурированное саммари:
  {title, summary, keywords}

Graceful degradation: если LLM недоступен → пустой dict → fallback на сырые сообщения.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings
from app.utils import strip_thinking

SYSTEM_PROMPT = """Ты — технический суммаризатор диалогов. Ниже лог чата между пользователем и AI-ассистентом.
Верни ТОЛЬКО JSON без лишнего текста:

{
  "title": "Заголовок (макс 8 слов, отражает суть)",
  "summary": "Техническое саммари (2-4 абзаца): что обсуждали, какие проблемы решали, какие решения приняли, какие файлы/код меняли, результат",
  "keywords": ["слово1", "слово2", "слово3", ...]
}

Правила:
- Не используй имена участников, только технические детали
- Если в диалоге есть код/команды — упомяни ключевые
- keywords: 5-10 релевантных терминов"""


class Summarizer:
    """LLM-based conversation summarizer. Thread-safe (sync httpx)."""

    def __init__(self):
        self.api_key = settings.llm_api_key
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.enabled = settings.llm_enabled and bool(self.api_key)
        self.max_tokens = settings.llm_max_tokens

    def _format_conversation(self, messages: list[tuple[str, str]]) -> str:
        """Format (role, content) pairs into a prompt block."""
        parts = []
        for role, content in messages:
            text = strip_thinking(content or "").strip()
            if len(text) < 15:
                continue
            # Truncate individual messages to avoid prompt blowup
            if len(text) > 4000:
                text = text[:4000] + " ...[truncated]"
            parts.append(f"[{role}] {text}")
        return "\n\n".join(parts)

    def summarize(self, messages: list[tuple[str, str]]) -> dict[str, Any]:
        """Summarize a conversation.

        Args:
            messages: list of (role, content) tuples.

        Returns:
            {"title": str, "summary": str, "keywords": str}
            or {} if summarization fails / disabled.
        """
        if not self.enabled:
            return {}

        conversation = self._format_conversation(messages)
        if not conversation:
            return {}

        # Truncate conversation to avoid token limit (max ~100K chars)
        if len(conversation) > 100000:
            conversation = conversation[:100000] + "\n\n...[truncated]"

        try:
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
                            {"role": "system", "content": SYSTEM_PROMPT},
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
            print(f"⚠ LLM summarization failed: {e}")
            return {}
