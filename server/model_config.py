"""模型运行配置：设置页可改，落盘到 server/model_config.json。

优先级：model_config.json > 环境变量（.env）。
API Key 只在服务端保存与使用，接口只返回“是否已配置”，不回传明文。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import ROOT, settings

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("MODEL_CONFIG_PATH") or (ROOT / "model_config.json"))
PROTOCOLS = ("anthropic", "openai")
DEFAULT_PROTOCOL = "anthropic"
_FIELDS = ("base_url", "protocol", "model", "api_key", "timeout")

_lock = threading.Lock()


def _defaults() -> dict[str, Any]:
    return {
        "base_url": settings.llm_base_url,
        "protocol": (os.getenv("LLM_PROTOCOL") or DEFAULT_PROTOCOL).strip().lower(),
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
        "timeout": settings.llm_timeout_seconds,
    }


def _clean_base_url(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("API 地址需要是 http(s):// 开头的完整地址")
    return text


def normalize(config: dict[str, Any]) -> dict[str, Any]:
    """补齐字段并做边界收敛，保证 LLMClient 拿到的配置总是可用的。"""
    data = _defaults()
    for key in _FIELDS:
        value = config.get(key)
        if key == "timeout":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                data[key] = max(5, min(int(value), 300))
            continue
        if isinstance(value, str) and value.strip():
            data[key] = value.strip()

    data["base_url"] = _clean_base_url(str(data["base_url"]))
    protocol = str(data.get("protocol") or "").lower()
    data["protocol"] = protocol if protocol in PROTOCOLS else DEFAULT_PROTOCOL
    data["model"] = str(data.get("model") or "").strip() or settings.llm_model
    data["api_key"] = str(data.get("api_key") or "").strip()
    data["timeout"] = max(5, min(int(data.get("timeout") or 90), 300))
    return data


def load() -> dict[str, Any]:
    """读取当前生效配置；文件损坏时回落到 .env，不影响服务启动。"""
    raw: dict[str, Any] = {}
    source = "env"
    if CONFIG_PATH.exists():
        try:
            parsed = json.loads(CONFIG_PATH.read_text("utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
                source = "file"
        except (OSError, ValueError) as exc:
            logger.warning("模型配置文件不可读，回落到环境变量: %s", exc)
    try:
        config = normalize(raw)
    except ValueError as exc:
        logger.warning("模型配置内容无效，回落到环境变量: %s", exc)
        config = normalize({})
        source = "env"
    config["source"] = source
    return config


def public() -> dict[str, Any]:
    """给前端的脱敏视图：不包含 API Key 明文。"""
    config = load()
    return {
        "base_url": config["base_url"],
        "protocol": config["protocol"],
        "model": config["model"],
        "timeout": config["timeout"],
        "has_api_key": bool(config["api_key"]),
        "source": config["source"],
    }


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入并原子替换；api_key 为空表示保持原值。"""
    if not isinstance(patch, dict):
        raise ValueError("请求体需要是 JSON 对象")
    with _lock:
        current = load()
        merged = {key: current[key] for key in _FIELDS}
        for key in _FIELDS:
            if key not in patch:
                continue
            value = patch[key]
            if key == "api_key":
                if isinstance(value, str) and value.strip():
                    merged[key] = value.strip()
                continue
            merged[key] = value
        config = normalize(merged)
        payload = {key: config[key] for key in _FIELDS}
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
        config["source"] = "file"
        return config
