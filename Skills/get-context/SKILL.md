---
name: get-context
description: Get full context entry by ID
---

Get complete context content from CacheProxy by its ID.

1. User must provide a `context_id` (UUID)
2. Use MCP tool `get_context_detail` with the provided context_id
3. Return full content with metadata

Example:
- `/get-context 7c43201a-f0b0-43e9-a49d-d87cb7cac2b5`
