"""Utility functions for text processing."""

from __future__ import annotations

import re
from typing import Optional


def _strip_json_block(text: str, key: str) -> str:
    """Remove JSON blocks with a given key like ``{key: {...}}``.

    Uses brace counting instead of regex to handle any nesting depth.
    """
    pattern = f'"{key}"'
    result = text
    while True:
        idx = result.find(pattern)
        if idx == -1:
            break
        # Find opening { before the key
        start = result.rfind("{", 0, idx)
        if start == -1:
            break
        # Walk forward counting braces to find matching close
        depth = 0
        end = start
        for i in range(start, len(result)):
            if result[i] == "{":
                depth += 1
            elif result[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            result = result[:start] + result[end:]
        else:
            break
    return result


def strip_thinking(content: Optional[str]) -> str:
    """Remove model thinking/reasoning blocks from content.

    Strips:
      - ``<thinking>...</thinking>`` (Claude)
      - ``[thinking]...[/thinking]`` (generic bracket style)
      - ``{"Thinking": {...}}`` (JSON-structured thinking, any nesting)
      - ``{"Mention": {...}}`` (JSON file/mention blocks)
      - ``{"Image": {...}}`` (JSON image blocks)
      - ``[Tool: name] {...}`` (tool call blocks)

    Returns cleaned text with excess whitespace collapsed.
    """
    if not content:
        return content or ""

    result = content

    # <thinking>...</thinking> (Claude) — non-greedy, multiline
    result = re.sub(
        r"<thinking>.*?</thinking>", "", result, flags=re.DOTALL | re.IGNORECASE
    )

    # [thinking]...[/thinking] (generic bracket style)
    result = re.sub(
        r"\[/?thinking\].*?\[/?thinking\]", "", result, flags=re.DOTALL | re.IGNORECASE
    )

    # {"Thinking": {...}} (JSON-structured, any nesting depth)
    result = _strip_json_block(result, "Thinking")

    # {"Mention": {...}} (JSON file/mention blocks)
    result = _strip_json_block(result, "Mention")

    # {"Image": {...}} (JSON image blocks)
    result = _strip_json_block(result, "Image")

    # [Tool: name] {...} — remove entire line(s) with tool calls
    result = re.sub(
        r"\[Tool:.*?(\n|$)",
        "",
        result,
    )

    # Collapse excessive blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
