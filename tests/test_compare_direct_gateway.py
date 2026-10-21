import json
import time
from collections import Counter

import pytest

from scripts.compare_direct_gateway import prepare_payload, summarize_delta


def test_prepare_payload_forces_stream_and_bounds_default_output(tmp_path):
    source = {
        "model": "01",
        "messages": [{"role": "user", "content": "call the tool"}],
        "tools": [{"type": "function", "function": {"name": "lookup_record", "parameters": {"type": "object"}}}],
        "reasoning_effort": "low",
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    payload = prepare_payload(path)

    assert payload["stream"] is True
    assert payload["stream_options"]["include_usage"] is True
    assert payload["max_completion_tokens"] == 512
    assert payload["model"] == "01"


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_prepare_payload_rejects_output_cap_above_limit(tmp_path, field):
    source = {
        "messages": [{"role": "user", "content": "call the tool"}],
        "tools": [{"type": "function", "function": {"name": "lookup_record", "parameters": {"type": "object"}}}],
        field: 1025,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        prepare_payload(path)


def test_summarize_delta_records_only_safe_metadata():
    secret = "DO_NOT_LOG_THIS_TOOL_ARGUMENT"
    started = time.monotonic()
    result = {
        "_started": started,
        "first_text_ms": None,
        "first_reasoning_ms": None,
        "first_tool_call_ms": None,
        "finish_reasons": Counter(),
        "delta_keys": Counter(),
        "usage_seen": False,
        "usage": {},
        "response_id": None,
    }

    summarize_delta(
        {
            "id": "chatcmpl-test",
            "choices": [{
                "finish_reason": "tool_calls",
                "delta": {
                    "reasoning": "private reasoning",
                    "tool_calls": [{"index": 0, "function": {"arguments": secret}}],
                },
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        },
        result,
    )

    assert result["finish_reasons"] == {"tool_calls": 1}
    assert result["delta_keys"] == {"reasoning": 1, "tool_calls": 1}
    assert result["first_reasoning_ms"] is not None
    assert result["first_tool_call_ms"] is not None
    assert result["usage_seen"] is True
    assert result["usage"]["total_tokens"] == 17
    assert secret not in repr(result)
    assert "private reasoning" not in repr(result)
