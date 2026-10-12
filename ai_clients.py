"""Small OpenAI-compatible clients for optional paqu model judging."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_local_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def model_status() -> dict[str, Any]:
    load_local_env()
    return {
        "deepseek": {"configured": bool(os.getenv("DEEPSEEK_API_KEY")), "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")},
        "qwen": {"configured": bool(os.getenv("QWEN_API_KEY")), "model": os.getenv("QWEN_VISION_MODEL", "qwen3-vl-flash")},
    }


def chat_json(base_url: str, api_key: str, model: str, messages: list[dict[str, Any]], timeout: float = 45) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": messages, "response_format": {"type": "json_object"}, "temperature": 0}).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"模型 API HTTP {exc.code}: {detail}") from exc
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content)


def deepseek_metadata_judge(topic: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    load_local_env()
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return None
    prompt = {
        "topic": topic,
        "title": metadata.get("title", ""),
        "description": str(metadata.get("description", ""))[:5000],
        "tags": metadata.get("tags") or [],
        "channel": metadata.get("channel") or metadata.get("uploader") or "",
    }
    return chat_json(
        os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), key,
        os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        [{"role": "system", "content": "你是工业安全视频分类器。只输出JSON：relevant布尔值、confidence为0到1、level为高相关/待审核/不相关、matched_terms数组、excluded_terms数组、reason中文字符串。不要把标题党当作画面事实。"},
         {"role": "user", "content": "请根据以下公开视频文字元数据判断是否符合主题。输出json。\n" + json.dumps(prompt, ensure_ascii=False)}],
    )


def qwen_thumbnail_judge(topic: str, thumbnail_url: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    load_local_env()
    key = os.getenv("QWEN_API_KEY")
    if not key or not thumbnail_url:
        return None
    return chat_json(
        os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"), key,
        os.getenv("QWEN_VISION_MODEL", "qwen3-vl-flash"),
        [{"role": "system", "content": "你是工业安全视觉复审员。只输出JSON：relevant布尔值、confidence为0到1、level为高相关/待审核/不相关、visual_evidence数组、reason中文字符串。单张缩略图无法证明瞬时事故时必须标为待审核，不可臆测。"},
         {"role": "user", "content": [
             {"type": "text", "text": f"主题：{topic}\n标题：{metadata.get('title','')}\n请检查缩略图是否提供相关视觉证据并输出json。"},
             {"type": "image_url", "image_url": {"url": thumbnail_url}},
         ]}],
    )
