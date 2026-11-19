"""与前端 Anthropic Messages 协议对齐的最小请求模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]]


class ChatRequest(BaseModel):
    """前端 `CreateMessageRequest` 的子集。

    `system`、`tools`、`safety_check` 由 BFF 忽略或覆盖；
    `thinking` 决定是否输出分析阶段；`title` 目前始终生成。
    """

    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-flash"
    system: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    thinking: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    safety_check: dict[str, Any] | None = None
    title: dict[str, Any] | None = None
    stream: bool = True


def content_to_text(content: str | list[dict[str, Any]]) -> str:
    """把 Anthropic 的 string / content blocks 统一成纯文本。"""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def latest_user_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            text = content_to_text(message.content)
            if text:
                return text
    return ""
