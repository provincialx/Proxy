---
name: recent-contexts
description: Show most recent context entries
---

Get the most recent context entries from CacheProxy.

1. Use MCP tool `get_recent_contexts`
2. Optionally filter by `project`
3. Return formatted list with metadata and snippets

Examples:
- `/recent-contexts` — последние контексты по всем проектам
- `/recent-contexts project:CacheProxy` — только CacheProxy
