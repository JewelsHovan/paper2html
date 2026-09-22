"""Unit tests for the lenient JSON extractor (pipeline/_json.py).

These pin the synthesis-failure bug observed on a real arXiv run: Claude wrapped
its synthesis JSON in a ```json code fence, and the bare ``json.loads`` raised
"Expecting value: line 1 column 1 (char 0)", silently emptying overall_summary,
key_contributions, and terminology.
"""

from __future__ import annotations

import json

import pytest

from paper2html.pipeline._json import loads_lenient


def test_plain_json_object() -> None:
    assert loads_lenient('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_json_code_fence() -> None:
    content = '```json\n{"overall_summary": "ok", "key_contributions": []}\n```'
    assert loads_lenient(content) == {
        "overall_summary": "ok",
        "key_contributions": [],
    }


def test_bare_code_fence() -> None:
    content = '```\n{"x": true}\n```'
    assert loads_lenient(content) == {"x": True}


def test_preamble_then_object() -> None:
    content = 'Here is the synthesis:\n{"overall_summary": "done"}'
    assert loads_lenient(content) == {"overall_summary": "done"}


def test_object_then_trailing_prose() -> None:
    content = '{"a": 1}\n\nLet me know if you need anything else.'
    assert loads_lenient(content) == {"a": 1}


def test_braces_inside_string_do_not_unbalance() -> None:
    content = 'preamble {"note": "use {curly} braces", "n": 1} trailing'
    assert loads_lenient(content) == {"note": "use {curly} braces", "n": 1}


def test_nested_object() -> None:
    content = '```json\n{"terminology": {"MHA": "multi-head attention"}}\n```'
    assert loads_lenient(content) == {"terminology": {"MHA": "multi-head attention"}}


def test_unrecoverable_raises_json_decode_error() -> None:
    """No object at all → genuine JSONDecodeError so callers' handlers fire."""
    with pytest.raises(json.JSONDecodeError):
        loads_lenient("this response contains no json whatsoever")


def test_empty_string_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        loads_lenient("")
