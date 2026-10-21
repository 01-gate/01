import json
import time
from collections import Counter
from pathlib import Path

import pytest

from scripts.compare_direct_gateway import (
    DEFAULT_NANOGPT_BASE,
    prepare_payload,
    summarize_delta,
    validate_tool_call_stream,
)


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


def test_sample_request_does_not_send_unsupported_reasoning_effort():
    sample_path = Path(__file__).resolve().parents[1] / "examples" / "tool_call_ab_request.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))

    # NanoGPT rejected this field for xiaomi/mimo-v2.6-flash in a live request.
    # Keep the diagnostic focused on tool-call SSE; model reasoning stays at its default.
    assert "reasoning_effort" not in sample
    assert sample["tool_choice"] == "required"


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
        "saw_tool_calls": False,
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


def test_default_nanogpt_base_matches_documented_api_origin():
    assert DEFAULT_NANOGPT_BASE == "https://nano-gpt.com/api/v1"



def _stream_result():
    return {
        "protocol_errors": [],
        "malformed_frames": 0,
        "done_seen": True,
        "saw_tool_calls": True,
        "finish_reasons": {"tool_calls": 1},
        "usage_seen": True,
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        "error_type": None,
    }


def test_validate_tool_call_stream_accepts_complete_tool_call():
    result = _stream_result()

    validate_tool_call_stream(result)

    assert result["error_type"] is None
    assert result["protocol_errors"] == []


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ({"done_seen": False}, "missing_done"),
        ({"saw_tool_calls": False}, "missing_tool_call_delta"),
        ({"finish_reasons": {"stop": 1}}, "missing_tool_calls_finish_reason"),
        ({"usage_seen": False}, "missing_or_incomplete_usage"),
        ({"malformed_frames": 1}, "malformed_json_frame"),
    ],
)
def test_validate_tool_call_stream_rejects_incomplete_or_wrong_stream(change, expected_error):
    result = {**_stream_result(), **change}

    validate_tool_call_stream(result)

    assert result["error_type"] == "invalid_sse_protocol"
    assert expected_error in result["protocol_errors"]


def test_validate_tool_call_stream_rejects_incomplete_usage_counts():
    result = _stream_result()
    result["usage"]["total_tokens"] = None

    validate_tool_call_stream(result)

    assert result["error_type"] == "invalid_sse_protocol"
    assert "missing_or_incomplete_usage" in result["protocol_errors"]
