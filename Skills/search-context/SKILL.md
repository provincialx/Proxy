---
name: search-context
description: Semantic search across CacheProxy conversation history
---

Search through stored conversation contexts in CacheProxy.

1. Use MCP tool `search_context` with the user's query
2. Optionally filter by `project` if user mentions a specific project
3. Return relevant results with scores and snippets

Examples:
- `/search-context настройки DB reset` — поиск обсуждений DB reset
- `/search-context suspension setup project:iRacing-Analyzer` — поиск в конкретном проекте
