"""Lenient JSON extraction for LLM responses.

Models frequently wrap their JSON in markdown code fences (```json … ```) or
emit a short preamble before the object, even when instructed not to. A bare
``json.loads`` raises ``Expecting value: line 1 column 1 (char 0)`` on such
responses. ``loads_lenient`` recovers the JSON object in those common cases
while still raising ``json.JSONDecodeError`` when no object can be found, so
callers keep their existing error handling.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ```json\n{...}\n```  or  ```\n{...}\n```  — capture the fenced body.
_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?```",
    re.DOTALL,
)


def loads_lenient(content: str) -> Any:
    """Parse JSON from *content*, tolerating code fences and preamble.

    Resolution order:
      1. Direct ``json.loads`` (the fast, strict path).
      2. The body of the first markdown code fence, if any.
      3. The first balanced ``{...}`` object found by brace scanning.

    Raises:
        json.JSONDecodeError: If no valid JSON object can be extracted.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    fence = _FENCE_RE.search(content)
    if fence:
        try:
            return json.loads(fence.group("body").strip())
        except json.JSONDecodeError:
            pass

    obj = _extract_first_object(content)
    if obj is not None:
        return json.loads(obj)

    # Nothing recoverable — re-raise a strict failure for the original content
    # so callers see a genuine JSONDecodeError.
    return json.loads(content)


def _extract_first_object(content: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or None.

    Scans for the first ``{`` and tracks brace depth while skipping over
    string literals (so braces inside strings don't unbalance the count).
    """
    start = content.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(content)):
        ch = content[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[start : i + 1]
    return None
