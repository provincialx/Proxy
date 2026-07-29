---
name: list-sessions
description: List all chat sessions in CacheProxy
---

List all chat sessions stored in CacheProxy.

1. Use MCP tool `list_sessions`
2. Optionally filter by `project` or `status` (active/archived)
3. Return formatted list with titles, projects, message counts and dates

Examples:
- `/list-sessions` — все сессии
- `/list-sessions project:CacheProxy` — только CacheProxy
- `/list-sessions status:active` — только активные
