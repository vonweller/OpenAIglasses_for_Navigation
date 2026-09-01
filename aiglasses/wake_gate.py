# -*- coding: utf-8 -*-
"""语音助手唤醒门：唤醒词 -> 激活窗口 -> 超时退出。

- 休眠：ASR 识别照常。闲聊 / 看图问答需先说唤醒词；找物、导航、过马路、红绿灯等功能指令仍立即执行。
- 激活：ACTIVE_WINDOW_SEC 秒内的正常语音照旧走 LLM/指令；每次有效交互刷新窗口。
- 超时：窗口到期自动回到休眠。
"""
import os
import re
import threading
import time
from typing import List, Optional

# 唤醒词（标准化后匹配，标点/空格忽略）。可用环境变量 AIGLASS_WAKE_WORDS 覆盖，逗号分隔。
DEFAULT_WAKE_WORDS = "你好智能助手"
ACTIVE_WINDOW_SEC = max(5.0, float(os.getenv("AIGLASS_WAKE_WINDOW_SEC", "15")))

# 休眠时仍立即执行的功能指令。闲聊 / 看图问答仍需先唤醒，避免误触发模型。
ALWAYS_ON_KEYWORDS = (
    "开始导航",
    "盲道导航",
    "帮我导航",
    "停止导航",
    "结束导航",
    "开始过马路",
    "帮我过马路",
    "过马路结束",
    "结束过马路",
    "检测红绿灯",
    "看红绿灯",
    "停止检测",
    "停止红绿灯",
    "立即通过",
    "现在通过",
    "找到了",
    "拿到了",
    "收到",
    "停止找物",
    "结束找物",
)
_FIND_RE = re.compile(
    r"(?:帮我|请|麻烦)?\s*(?:找一下|找一找|找一个|找找|寻找|搜索|识别一下|检测一下|找)\s*\S+"
    r"|.+(?:在哪里|在哪儿|在哪|哪里|的位置)"
)

_PUNCT_RE = re.compile(r"[\s，。！？!?,.、·~～:：;；'\"“”‘’()（）\[\]【】]+")
_PUNCT_SET = "，。！？!?,. 、·~～:：;；'\"“”‘’()（）[]【】"

_lock = threading.Lock()
_active_until = 0.0
_last_wake_at = 0.0


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or "")).lower()


def _wake_words() -> List[str]:
    raw = os.getenv("AIGLASS_WAKE_WORDS", DEFAULT_WAKE_WORDS)
    words = [_normalize(x) for x in raw.split(",") if _normalize(x)]
    return words or [_normalize(DEFAULT_WAKE_WORDS)]


def is_wake_phrase(text: str) -> bool:
    norm = _normalize(text)
    if not norm:
        return False
    return any(w in norm for w in _wake_words())


def is_always_on_command(text: str) -> bool:
    """功能指令在休眠时也应直接执行，不要求先说唤醒词。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if any(k in raw for k in ALWAYS_ON_KEYWORDS):
        return True
    return bool(_FIND_RE.search(raw))


def _wake_span(text: str, keyword: str) -> Optional[tuple]:
    """在原文中定位唤醒词（允许中间夹标点），返回 (start, end)；找不到返回 None。"""
    ki = 0
    start = -1
    for i, ch in enumerate(text):
        if ch in _PUNCT_SET:
            continue
        if _normalize(ch) == keyword[ki]:
            if ki == 0:
                start = i
            ki += 1
            if ki == len(keyword):
                return (start, i + 1)
        else:
            ki = 1 if _normalize(ch) == keyword[0] else 0
            start = i if ki == 1 else -1
    return None


def extract_command_after_wake(text: str) -> str:
    """返回唤醒词之后的指令文本；唤醒词在句尾（无后续指令）时返回空串。"""
    text = str(text or "")
    for w in _wake_words():
        span = _wake_span(text, w)
        if span is None:
            continue
        rest = text[span[1]:].lstrip(_PUNCT_SET).strip()
        if rest:
            return rest
        pre = text[: span[0]].rstrip(_PUNCT_SET).strip()
        if pre:
            return pre
    return ""


def is_active() -> bool:
    with _lock:
        return time.time() < _active_until


def remaining_sec() -> float:
    with _lock:
        return max(0.0, _active_until - time.time())


def activate(window_sec: float = ACTIVE_WINDOW_SEC) -> None:
    global _active_until, _last_wake_at
    with _lock:
        now = time.time()
        _active_until = now + window_sec
        _last_wake_at = now


def touch(window_sec: float = ACTIVE_WINDOW_SEC) -> None:
    """一次有效交互后刷新窗口；仅在仍处于激活状态时刷新。"""
    global _active_until
    with _lock:
        if time.time() < _active_until:
            _active_until = time.time() + window_sec


def deactivate() -> None:
    global _active_until
    with _lock:
        _active_until = 0.0


def snapshot() -> dict:
    with _lock:
        return {
            "active": time.time() < _active_until,
            "remaining_sec": round(max(0.0, _active_until - time.time()), 1),
            "window_sec": ACTIVE_WINDOW_SEC,
            "wake_words": [w for w in os.getenv("AIGLASS_WAKE_WORDS", DEFAULT_WAKE_WORDS).split(",") if w],
            "last_wake_at": _last_wake_at,
        }
