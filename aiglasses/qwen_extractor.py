# qwen_extractor.py
# -*- coding: utf-8 -*-
from typing import List, Tuple
import os
import re
from openai import OpenAI

# —— 本地优先映射（可随时扩充/改名）——
LOCAL_CN2EN = {
    "红牛": "Red_Bull",
    "ad钙奶": "AD_milk",
    "ad 钙奶": "AD_milk",
    "ad": "AD_milk",
    "钙奶": "AD_milk",
    "矿泉水": "bottle",
    "水瓶": "bottle",
    "可乐": "coke",
    "雪碧": "sprite",
    "鼠标": "mouse",
    "鼠标垫": "mouse pad",
    "键盘": "keyboard",
    "手机": "cell phone",
    "手晶": "cell phone",
    "手经": "cell phone",
    "手机壳": "cell phone",
    "phone": "cell phone",
    "mobile": "cell phone",
    "mobile phone": "cell phone",
    "cellphone": "cell phone",
    "cell phone": "cell phone",
    "杯子": "cup",
    "水杯": "cup",
    "电脑": "laptop",
    "笔记本电脑": "laptop",
    "平板": "tablet",
    "平板电脑": "tablet",
    "显示器": "computer monitor",
    "屏幕": "computer monitor",
    "遥控器": "remote control",
    "充电器": "charger",
    "数据线": "cable",
    "耳机": "earphones",
    "头戴耳机": "headphones",
    "眼镜": "glasses",
    "钥匙": "key",
    "钱包": "wallet",
    "书": "book",
    "书本": "book",
    "笔记本": "notebook",
    "书包": "backpack",
    "背包": "backpack",
    "包": "handbag",
    "手表": "watch",
    "智能手表": "smart watch",
    "椅子": "chair",
    "桌子": "table",
    "沙发": "sofa",
    "床": "bed",
    "柜子": "cabinet",
    "门": "door",
    "苹果": "apple",
    "香蕉": "banana",
    "橙子": "orange",
    "葡萄": "grape",
    "梨": "pear",
    "西瓜": "watermelon",
    "汽车": "car",
    "车": "car",
    "公交车": "bus",
    "自行车": "bicycle",
    "摩托车": "motorcycle",
    "卡车": "truck",
}

QUERY_NOISE_RE = re.compile(
    r"(帮我|请|麻烦|找一下|找一找|找一个|找找|寻找|搜索|识别|检测|看一下|看看|在哪里|在哪儿|在哪|哪里|什么位置|的位置|一下|一个|一只|这个|那个|请问|吗|呢|吧|呀|啊)",
    re.IGNORECASE,
)


def normalize_object_query(query_cn: str) -> str:
    q = (query_cn or "").strip().lower()
    q = re.sub(r"[，。！？、,.!?：:；;（）()\[\]【】\"'“”‘’]", " ", q)
    q = QUERY_NOISE_RE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q

def _make_client() -> OpenAI:
    # 复用你百炼兼容端点；支持从环境变量读取
    base_url = os.getenv("DASHSCOPE_COMPAT_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    api_key  = os.getenv("DASHSCOPE_API_KEY", "")
    return OpenAI(api_key=api_key, base_url=base_url)

PROMPT_SYS = (
    "You are a label normalizer. Convert the given Chinese object "
    "description into a short, lowercase English YOLO/vision class name "
    "(1~3 words). If multiple are given, return the single most likely one. "
    "Output ONLY the label, no punctuation."
)

LABEL_ALIASES = {
    "phone": "cell phone",
    "mobile": "cell phone",
    "mobile phone": "cell phone",
    "cellphone": "cell phone",
    "smartphone": "cell phone",
}

def extract_english_label(query_cn: str) -> Tuple[str, str]:
    """
    返回 (label_en, source)；source ∈ {'local', 'qwen', 'fallback'}
    """
    q = normalize_object_query(query_cn)
    if q in LOCAL_CN2EN:
        return LOCAL_CN2EN[q], "local"

    # 简单规则：去掉前缀修饰词
    for k, v in LOCAL_CN2EN.items():
        if k in q or k in (query_cn or "").strip().lower():
            return v, "local"

    # 调用 Qwen Turbo（兼容 Chat Completions）
    try:
        client = _make_client()
        msgs = [
            {"role": "system", "content": PROMPT_SYS},
            {"role": "user",   "content": q or query_cn.strip()},
        ]
        rsp = client.chat.completions.create(
            model=os.getenv("QWEN_MODEL", "qwen-turbo"),
            messages=msgs,
            stream=False
        )
        label = (rsp.choices[0].message.content or "").strip()
        # 清洗一下
        label = label.replace(".", "").replace(",", "").replace("  ", " ").strip().lower()
        label = LABEL_ALIASES.get(label, label)
        # 兜底：空就回 'bottle'
        return (label or "bottle"), "qwen"
    except Exception:
        return "bottle", "fallback"
