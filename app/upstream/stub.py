# shortcut: no real upstream; replace with NanoGPT client after smoke contract
"""Stub-генерация на границе будущего upstream (app/upstream/nanogpt.py)."""

import asyncio
import time
import uuid
from typing import Any, AsyncIterator

STUB_TEMPLATE = "[local-stub] model={model} ok. Last user: {last_user}"


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else "(non-text)"
    return "(none)"


def _stub_text(public_model_id: str, messages: list[dict[str, Any]]) -> str:
    last_user = _last_user_text(messages)[:200]
    return STUB_TEMPLATE.format(model=public_model_id, last_user=last_user)


def _usage(messages: list[dict[str, Any]], stub_text: str) -> dict[str, int]:
    # фейковая оценка, не биллинг (см. SPEC_002 §4.8)
    prompt_tokens = max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4)
    completion_tokens = max(1, len(stub_text) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _fragments(stub_text: str) -> list[str]:
    words = stub_text.split(" ")
    fragments = []
    for i, word in enumerate(words):
        piece = word + (" " if i < len(words) - 1 else "")
        if piece:
            fragments.append(piece)
    return fragments


async def _stream_chunks(
    response_id: str,
    created: int,
    public_model_id: str,
    stub_text: str,
    usage: dict[str, int],
    include_usage: bool,
) -> AsyncIterator[dict[str, Any]]:
    base = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": public_model_id,
    }
    first = True
    for fragment in _fragments(stub_text):
        delta = {"role": "assistant", "content": fragment} if first else {"content": fragment}
        first = False
        yield {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        await asyncio.sleep(0)
    yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if include_usage:
        yield {**base, "choices": [], "usage": usage}


async def run_chat(
    public_model_id: str,
    messages: list[dict[str, Any]],
    stream: bool,
    include_usage: bool,
    extra: dict[str, Any],
) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
    """Sync → dict chat.completion; stream → async-итератор chunk'ов.

    extra — только метаданные (имена pass-through полей), на генерацию не влияет.
    """
    stub_text = _stub_text(public_model_id, messages)
    usage = _usage(messages, stub_text)
    response_id = f"chatcmpl-local-{uuid.uuid4().hex}"
    created = int(time.time())
    if not stream:
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": public_model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": stub_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
    return _stream_chunks(response_id, created, public_model_id, stub_text, usage, include_usage)
