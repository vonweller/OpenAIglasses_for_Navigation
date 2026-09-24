# omni_client.py
# -*- coding: utf-8 -*-
import asyncio
import os
import threading
from typing import Any, AsyncGenerator, Dict, List, Optional

from openai import OpenAI

API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN_MODEL = "qwen-omni-turbo"
DEFAULT_SYSTEM_PROMPT = (
    "你是 AI 眼镜语音助手。使用简体中文直接回答，默认只说 1 到 2 句、50 字以内。"
    "不要复述问题，不要寒暄；只有安全提醒、导航或用户明确要求详细说明时，才补充必要步骤。"
)
CONCISE_USER_REMINDER = "回答要求：请用简体中文，直接回答，通常不超过两句或50字。"

oai_client = OpenAI(
    api_key=API_KEY or "missing-key",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)


def set_api_key(api_key: str):
    global API_KEY, oai_client
    API_KEY = (api_key or "").strip()
    os.environ["DASHSCOPE_API_KEY"] = API_KEY
    oai_client = OpenAI(
        api_key=API_KEY or "missing-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )


class OmniStreamPiece:
    def __init__(self, text_delta: Optional[str] = None, audio_b64: Optional[str] = None):
        self.text_delta = text_delta
        self.audio_b64 = audio_b64


_STREAM_DONE = object()


def _parse_stream_chunk(chunk) -> Optional[OmniStreamPiece]:
    text_delta: Optional[str] = None
    audio_b64: Optional[str] = None

    if getattr(chunk, "choices", None):
        c0 = chunk.choices[0]
        delta = getattr(c0, "delta", None)
        if delta and getattr(delta, "content", None):
            piece = delta.content
            if piece:
                text_delta = piece
        if delta and getattr(delta, "audio", None):
            aud = delta.audio
            audio_b64 = aud.get("data") if isinstance(aud, dict) else getattr(aud, "data", None)
        if audio_b64 is None:
            msg = getattr(c0, "message", None)
            if msg and getattr(msg, "audio", None):
                ma = msg.audio
                audio_b64 = ma.get("data") if isinstance(ma, dict) else getattr(ma, "data", None)

    if text_delta is not None or audio_b64 is not None:
        return OmniStreamPiece(text_delta=text_delta, audio_b64=audio_b64)
    return None


async def stream_chat(
    content_list: List[Dict[str, Any]],
    voice: str = "Cherry",
    audio_format: str = "wav",
    system_prompt: Optional[str] = None,
) -> AsyncGenerator[OmniStreamPiece, None]:
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[object]" = asyncio.Queue()
    stop_event = threading.Event()

    def _worker():
        try:
            user_content = list(content_list)
            user_content.append({"type": "text", "text": CONCISE_USER_REMINDER})
            completion = oai_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        or os.getenv("AIGLASS_OMNI_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
                    },
                    {"role": "user", "content": user_content},
                ],
                modalities=["text", "audio"],
                audio={"voice": voice, "format": audio_format},
                max_tokens=max(32, int(os.getenv("AIGLASS_OMNI_MAX_TOKENS", "96"))),
                stream=True,
                stream_options={"include_usage": True},
            )
            for chunk in completion:
                if stop_event.is_set():
                    break
                piece = _parse_stream_chunk(chunk)
                if piece is not None:
                    loop.call_soon_threadsafe(q.put_nowait, piece)
        except Exception as exc:
            loop.call_soon_threadsafe(q.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, _STREAM_DONE)

    threading.Thread(target=_worker, name="omni-stream", daemon=True).start()

    try:
        while True:
            item = await q.get()
            if item is _STREAM_DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop_event.set()
