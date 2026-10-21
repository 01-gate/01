#!/usr/bin/env python3
"""Compare one streamed tool-call request directly with NanoGPT and via the gateway.

This intentionally makes up to TWO paid requests, one per route. It never retries,
never executes returned tools, and never prints message/tool/reasoning contents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

UPSTREAM_MODEL = "xiaomi/mimo-v2.6-flash"
DEFAULT_NANOGPT_BASE = "https://api.nano-gpt.com/v1"
DEFAULT_GATEWAY_BASE = "http://127.0.0.1:8000/v1"
MAX_BODY_BYTES = 128 * 1024
MAX_OUTPUT_TOKENS = 1024


def load_project_env() -> None:
    """Read missing values from the repository .env without printing any values."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(("NANOGPT_", "GATEWAY_")):
            os.environ.setdefault(key, value.strip().strip("\"'"))


def prepare_payload(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError(f"Input JSON exceeds {MAX_BODY_BYTES} bytes.")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Input JSON must be an object.")
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise ValueError("Input JSON must contain a non-empty messages array.")
    if not isinstance(payload.get("tools"), list) or not payload["tools"]:
        raise ValueError("This diagnostic requires a non-empty tools array.")
    if "reasoning_effort" in payload and not isinstance(payload["reasoning_effort"], str):
        raise ValueError("reasoning_effort must be a string when provided.")

    for field in ("max_completion_tokens", "max_tokens"):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
            or value < 1 or value > MAX_OUTPUT_TOKENS
        ):
            raise ValueError(f"{field} must be an integer from 1 to {MAX_OUTPUT_TOKENS}.")

    payload["stream"] = True
    stream_options = payload.get("stream_options")
    if stream_options is None:
        stream_options = {}
    if not isinstance(stream_options, dict):
        raise ValueError("stream_options must be an object.")
    stream_options["include_usage"] = True
    payload["stream_options"] = stream_options

    # Bound output cost when caller omitted an explicit limit.
    if "max_completion_tokens" not in payload and "max_tokens" not in payload:
        payload["max_completion_tokens"] = 512
    payload["model"] = "01"
    return payload


def summarize_delta(chunk: dict[str, Any], result: dict[str, Any]) -> None:
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                safe_reasons = {"stop", "length", "tool_calls", "function_call", "content_filter"}
                result["finish_reasons"][reason if reason in safe_reasons else "other"] += 1
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            for key in delta:
                if isinstance(key, str):
                    result["delta_keys"][key if key in {"content", "reasoning", "reasoning_content", "role", "tool_calls", "function_call"} else "other"] += 1
            content = delta.get("content")
            if isinstance(content, str) and content and result["first_text_ms"] is None:
                result["first_text_ms"] = elapsed_ms(result["_started"])
            for key in ("reasoning", "reasoning_content"):
                value = delta.get(key)
                if isinstance(value, str) and value and result["first_reasoning_ms"] is None:
                    result["first_reasoning_ms"] = elapsed_ms(result["_started"])
            if delta.get("tool_calls") and result["first_tool_call_ms"] is None:
                result["first_tool_call_ms"] = elapsed_ms(result["_started"])

    usage = chunk.get("usage")
    if isinstance(usage, dict):
        result["usage_seen"] = True
        result["usage"] = {
            key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage.get(key), (int, float))
        }
    if isinstance(chunk.get("id"), str) and result["response_id"] is None:
        result["response_id"] = chunk["id"]


def elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


async def run_one(
    label: str,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request_payload = dict(payload)
    request_payload["model"] = UPSTREAM_MODEL if label == "direct" else "01"
    result: dict[str, Any] = {
        "route": label,
        "status": None,
        "response_open_ms": None,
        "first_event_ms": None,
        "first_text_ms": None,
        "first_reasoning_ms": None,
        "first_tool_call_ms": None,
        "events": 0,
        "max_event_gap_ms": 0,
        "finish_reasons": Counter(),
        "delta_keys": Counter(),
        "usage_seen": False,
        "usage": {},
        "response_id": None,
        "request_id": None,
        "done_seen": False,
        "error_type": None,
        "_started": time.monotonic(),
    }
    started = result["_started"]
    previous_event_ms: int | None = None
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json=request_payload,
            ) as response:
                result["status"] = response.status_code
                result["response_open_ms"] = elapsed_ms(started)
                result["request_id"] = response.headers.get("x-request-id")
                if response.status_code < 200 or response.status_code >= 300:
                    result["error_type"] = "http_error"
                    return _clean_result(result)

                data_lines: list[str] = []

                async def process_frame() -> None:
                    nonlocal previous_event_ms
                    if not data_lines:
                        return
                    data = "\n".join(data_lines)
                    if data.strip() == "[DONE]":
                        result["done_seen"] = True
                        data_lines.clear()
                        return
                    try:
                        chunk = json.loads(data)
                    except (ValueError, TypeError):
                        data_lines.clear()
                        return
                    now_ms = elapsed_ms(started)
                    result["events"] += 1
                    if result["first_event_ms"] is None:
                        result["first_event_ms"] = now_ms
                    if previous_event_ms is not None:
                        result["max_event_gap_ms"] = max(
                            result["max_event_gap_ms"], now_ms - previous_event_ms
                        )
                    previous_event_ms = now_ms
                    if isinstance(chunk, dict):
                        summarize_delta(chunk, result)
                    data_lines.clear()

                async for line in response.aiter_lines():
                    if line == "":
                        await process_frame()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                await process_frame()
                if not result["done_seen"]:
                    result["error_type"] = "stream_incomplete_unknown_outcome"
    except httpx.TimeoutException:
        result["error_type"] = "timeout_unknown_outcome"
    except httpx.HTTPError as exc:
        result["error_type"] = type(exc).__name__

    return _clean_result(result)


def _clean_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    result.pop("_started", None)
    for key in ("finish_reasons", "delta_keys"):
        result[key] = dict(sorted(result[key].items()))
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_json", type=Path, help="Small JSON request with messages and tools")
    parser.add_argument(
        "--confirm-paid", action="store_true",
        help="Required acknowledgement: sends one request to each route (up to two paid calls)",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    if not args.confirm_paid:
        parser.error("refusing to call paid endpoints without --confirm-paid")
    if args.timeout <= 0 or args.timeout > 180:
        parser.error("--timeout must be between 0 and 180 seconds")

    load_project_env()
    nano_key = os.environ.get("NANOGPT_API_KEY", "")
    gateway_key = os.environ.get("GATEWAY_TEST_API_KEY", "")
    if not nano_key or nano_key.startswith("replace_with_"):
        parser.error("NANOGPT_API_KEY is not configured")
    if not gateway_key or gateway_key.startswith("replace_with_"):
        parser.error("GATEWAY_TEST_API_KEY is not configured")

    try:
        payload = prepare_payload(args.request_json)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid request file: {exc}")

    direct_base = os.environ.get("NANOGPT_INFERENCE_BASE_URL", DEFAULT_NANOGPT_BASE)
    gateway_base = os.environ.get("GATEWAY_BASE_URL", DEFAULT_GATEWAY_BASE)

    print("A/B comparison will send up to TWO paid streaming requests.")
    print("Only timing, protocol metadata, request IDs and usage counts are printed; content is suppressed.")
    print(f"Input: {args.request_json}; tools={len(payload['tools'])}; messages={len(payload['messages'])}; output_cap={payload.get('max_completion_tokens', payload.get('max_tokens'))}")
    print("Route order: direct NanoGPT first, then gateway. No retries; tool calls are not executed.")
    direct = await run_one("direct", direct_base, nano_key, payload, args.timeout)
    print(json.dumps(direct, ensure_ascii=False, sort_keys=True))
    if direct.get("status") is None or not 200 <= direct["status"] < 300 or direct.get("error_type"):
        print("Stopping after direct request failed or had unknown outcome; gateway request NOT sent.", file=sys.stderr)
        return 2

    gateway = await run_one("gateway", gateway_base, gateway_key, payload, args.timeout)
    print(json.dumps(gateway, ensure_ascii=False, sort_keys=True))
    if gateway.get("error_type") in {"timeout_unknown_outcome", "stream_incomplete_unknown_outcome"}:
        print("Gateway outcome is unknown or incomplete. Do not retry blindly; inspect gateway logs/request IDs.", file=sys.stderr)
        return 3
    return 0 if gateway.get("status") is not None and 200 <= gateway["status"] < 300 and not gateway.get("error_type") else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
