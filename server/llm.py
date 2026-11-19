"""模型代理客户端：同时支持 Anthropic Messages 与 OpenAI Chat Completions。

默认地址是本机模型代理（见 .env）；运行配置由 model_config.py 提供，
设置页保存后 app.py 会重建客户端并重新绑定到 Agent。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from model_config import load as load_model_config

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _join(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/{suffix}"
    return f"{base}/v1/{suffix}"


class LLMClient:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or load_model_config()
        self.base_url = str(cfg.get("base_url") or "")
        self.protocol = str(cfg.get("protocol") or "anthropic")
        self.api_key = str(cfg.get("api_key") or "")
        self.model = str(cfg.get("model") or "")
        self.timeout = int(cfg.get("timeout") or 90)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and self.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _models_url(self) -> str:
        return _join(self.base_url, "models")

    def _chat_url(self) -> str:
        return _join(
            self.base_url, "messages" if self.protocol == "anthropic" else "chat/completions"
        )

    async def list_models(self) -> list[str]:
        if not self.configured:
            raise LLMError("模型未配置完整：需要 API 地址、API Key 和模型名")
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get(self._models_url(), headers=self._headers())
        except httpx.HTTPError as exc:
            raise LLMError(f"获取模型列表失败: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"模型服务返回 HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
        except ValueError:
            raise LLMError("模型列表不是 JSON")
        return _parse_models(data)

    async def ping(self) -> bool:
        try:
            await self.list_models()
        except LLMError as exc:
            logger.warning("llm ping failed: %s", exc)
            return False
        return True

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ) -> str:
        if not self.configured:
            raise LLMError("模型未配置完整：需要 API 地址、API Key 和模型名")
        if self.protocol == "anthropic":
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        else:
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10),
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._chat_url(), headers=self._headers(), json=payload
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"模型代理请求失败: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500]
            raise LLMError(f"模型代理返回 HTTP {response.status_code}: {detail}")

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("模型代理返回的不是 JSON") from exc

        if isinstance(data.get("error"), dict):
            raise LLMError(str(data["error"].get("message") or data["error"]))

        if self.protocol == "anthropic":
            text = _anthropic_text(data)
        else:
            text = _openai_text(data)
        if not text:
            raise LLMError("模型没有返回 text 内容")
        return text


def _anthropic_text(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    return "\n".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts).strip()
    return ""


def _parse_models(data: Any) -> list[str]:
    items: Any = data
    if isinstance(data, dict):
        for key in ("data", "models", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = []
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
        else:
            name = None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names[:200]
