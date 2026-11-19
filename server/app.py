"""FastAPI BFF：对前端暴露 Anthropic Messages 兼容接口。

启动：
    python -m uvicorn app:app --host 127.0.0.1 --port 8088 --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import prewarm
from agent import EnergyAgent, detect_intent
from caches import SCHEMA_CACHE, SQL_CACHE
from config import settings
from knowledge import knowledge_payload
from llm import LLMClient, LLMError
from model_config import load as load_model_config
from model_config import normalize as normalize_model_config
from model_config import public as public_model_config
from model_config import save as save_model_config
from models import ChatRequest, content_to_text, latest_user_text
from providers import get_provider
from semantic import plan_question
from tenants import tenant_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("energy-bff")

provider = get_provider()
llm = LLMClient()
agent = EnergyAgent(provider, llm)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    task = None
    if settings.schema_prewarm:
        # 后台预热，不阻塞端口就绪；失败只写日志。
        task = asyncio.create_task(prewarm.prewarm_schemas())
    try:
        yield
    finally:
        if task is not None and not task.done():
            task.cancel()


app = FastAPI(title="能效 Agent BFF", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _apply_model_config(config: dict[str, Any]) -> None:
    """设置页保存后重建客户端，并让 Agent / 调查循环立即使用新模型。"""
    global llm
    client = LLMClient(config)
    llm = client
    agent.llm = client
    investigator = getattr(agent, "investigator", None)
    if investigator is not None:
        investigator.llm = client


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _read_token(request: Request) -> str:
    token = request.headers.get("x-api-key", "").strip()
    if token:
        return token
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _require_auth(request: Request) -> None:
    if not settings.bff_token:
        return
    if _read_token(request) != settings.bff_token:
        raise HTTPException(status_code=401, detail="访问令牌无效")


def _line_chunks(text: str) -> list[str]:
    """按行切分并保留换行符，让前端逐行渲染而不是整段刷出。"""
    if not text:
        return [""]
    return text.splitlines(keepends=True)


def _line_delay(line_count: int) -> float:
    """把总输出时长摊到每一行：短回答不拖沓，长回答也不会太慢。"""
    if line_count <= 0:
        return 0.0
    target = settings.answer_stream_seconds
    if target <= 0:
        return 0.0
    # 单行最少 25ms、最多 180ms；总时长受 ANSWER_STREAM_SECONDS 控制。
    return min(0.18, max(0.025, target / line_count))


def _stages(question: str) -> list[str]:
    plan = plan_question(question)
    if plan.key == "cold_station_electricity":
        return [
            "识别冷站用电指标",
            "读取冷站与设备映射",
            "检查电表是否可用",
            "按冷站聚合用电",
            "组织结论与边界",
        ]
    if plan.key == "cold_station_cooling":
        return ["识别冷站冷量指标", "读取冷站配置", "检查冷量口径", "说明边界"]
    if plan.key == "energy_anomaly":
        return [
            "识别用电异常问题",
            "读取当前区间用电",
            "读取前 7 天基线",
            "计算偏差并判断",
            "组织结论与边界",
        ]
    if plan.key in ("device_count", "meter_count", "asset_counts"):
        return ["识别实体数量", "读取 MySQL 台账", "过滤租户与删除标记", "返回数量"]
    intent = detect_intent(question)
    if intent == "capability":
        return ["读取能力清单", "整理数据口径与边界"]
    if intent == "room_temp":
        return ["解析设备名称", "解析设备 ID", "读取最近点位", "校验在线状态"]
    if intent == "control":
        return ["解析时间范围", "选择控制任务模板", "聚合成功/失败", "核对状态码口径"]
    if intent == "alarm":
        return ["解析时间范围", "探测告警表", "按级别/状态聚合", "判断是否可用"]
    if intent == "knowledge":
        return ["识别指标与问题类型", "匹配代码路由", "核对库表字段", "整理口径与边界"]
    if intent == "memory":
        return ["读取会话历史", "定位上一轮问题", "组织承接回答"]
    if intent == "data_quality":
        return ["解析数据粒度", "读取小时表", "按整点聚合", "校验表计覆盖", "组织结论"]
    if intent == "mysql":
        return [
            "选择 MySQL 业务库",
            "读取表结构",
            "生成只读 SQL",
            "执行并观察结果",
            "必要时修正查询",
            "组织结论",
        ]
    if intent == "cold_station":
        return ["解析租户与冷站意图", "读取冷站配置表", "校验配置记录", "整理冷站清单"]
    return [
        "解析问题与时间范围",
        "选择数据源与指标模板",
        "通过 Archery 查询（只读）",
        "校验数据完整性",
        "组织结论",
    ]


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "能效 Agent BFF",
        "endpoints": [
            "GET /models",
            "GET /tenants",
            "GET /knowledge",
            "POST /anthropic/v1/messages",
            "GET /health",
            "GET/PUT /model/config",
            "POST /model/probe",
        ],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    current_tenant = next((row for row in tenant_rows() if row["current"]), None)
    model_view = public_model_config()
    return {
        "status": "ok",
        "data_provider": provider.mode,
        "dataset": provider.dataset,
        "llm_model": llm.model,
        "llm_base_url": llm.base_url,
        "llm_protocol": llm.protocol,
        "llm_configured": llm.configured,
        "model_config": model_view,
        "auth_required": bool(settings.bff_token),
        "current_tenant_id": settings.doris_tenant_id,
        "current_tenant_name": current_tenant["name"] if current_tenant else None,
        "current_tenant_db": settings.doris_db,
        "current_project_id": settings.doris_project_id,
        "allow_tenant_query": settings.allow_tenant_query,
        "conversation_memory": "messages_history",
        "metric_semantic_layer": "semantic.py",
        "hourly_completeness": "dm_eo_e_meter_hi",
        "mysql_data_source": (
            f"{settings.mysql_db}（{settings.mysql_archery_instance} / Archery 只读）"
            if settings.mysql_enabled
            else None
        ),
        "sql_investigation": "llm_tool_loop",
        "agent_tool_loop": settings.agent_tool_loop,
        "agent_max_steps": settings.agent_max_steps,
        "answer_stream_seconds": settings.answer_stream_seconds,
        "agent_tools": [
            "metric",
            "run_sql",
            "find_tables",
            "search_columns",
            "describe_table",
            "table_profile",
            "knowledge",
            "read_code",
            "tenants",
        ],
        "schema_cache": SCHEMA_CACHE.stats(),
        "schema_prewarm": prewarm.STATUS.as_dict(),
        "sql_cache": SQL_CACHE.stats(),
    }


@app.get("/health/llm")
async def health_llm() -> dict[str, Any]:
    return {"ok": await llm.ping(), "model": llm.model, "protocol": llm.protocol}


@app.get("/model/config")
async def model_config_get(request: Request) -> dict[str, Any]:
    """当前生效的模型配置（API Key 只返回是否已配置）。"""
    _require_auth(request)
    return public_model_config()


@app.put("/model/config")
async def model_config_put(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        patch = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体需要是 JSON 对象")
    try:
        config = save_model_config(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _apply_model_config(config)
    logger.info(
        "model config updated: protocol=%s base=%s model=%s",
        config["protocol"],
        config["base_url"],
        config["model"],
    )
    return public_model_config()


@app.post("/model/probe")
async def model_probe(request: Request) -> dict[str, Any]:
    """用当前或草稿配置探测连通性并拉取模型列表；不落盘。"""
    _require_auth(request)
    body: dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        body = {}
    current = load_model_config()
    merged = {key: current[key] for key in ("base_url", "protocol", "model", "api_key", "timeout")}
    for key in ("base_url", "protocol", "model", "api_key", "timeout"):
        if key in body:
            merged[key] = body[key]
    try:
        config = normalize_model_config(merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client = LLMClient(config)
    started = time.perf_counter()
    try:
        models = await client.list_models()
    except LLMError as exc:
        return {
            "ok": False,
            "models": [],
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
    return {
        "ok": True,
        "models": models,
        "error": None,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


@app.get("/models")
async def models(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {
        "data": [
            {
                "type": "model",
                "id": settings.llm_model,
                "display_name": settings.llm_model,
                "created_at": "2025-01-01T00:00:00Z",
            }
        ],
        "has_more": False,
        "first_id": settings.llm_model,
        "last_id": settings.llm_model,
    }


@app.get("/tenants")
async def tenants(request: Request) -> dict[str, Any]:
    _require_auth(request)
    rows = tenant_rows()
    return {
        "data": rows,
        "current_tenant_id": settings.doris_tenant_id,
        "current_tenant_db": settings.doris_db,
        "current_project_id": settings.doris_project_id,
        "allow_tenant_query": settings.allow_tenant_query,
    }


@app.get("/knowledge")
async def knowledge(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return knowledge_payload()


@app.post("/anthropic/v1/files")
async def upload_file(request: Request) -> JSONResponse:
    _require_auth(request)
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "type": "not_supported",
                "message": "首期不启用附件上传；请直接描述要查询的指标。",
            }
        },
    )


@app.delete("/anthropic/v1/files/{file_id}")
async def delete_file(file_id: str, request: Request) -> JSONResponse:
    _require_auth(request)
    return JSONResponse(
        status_code=501,
        content={"error": {"type": "not_supported", "message": f"附件功能未启用：{file_id}"}},
    )


@app.post("/anthropic/v1/messages")
async def messages(request: Request):
    _require_auth(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc

    try:
        body = ChatRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 返回前端可读的错误
        raise HTTPException(status_code=400, detail=f"请求体不符合协议: {exc}") from exc

    question = latest_user_text(body.messages)
    if not question:
        raise HTTPException(status_code=400, detail="没有找到用户问题")
    history = [
        {"role": message.role, "content": content_to_text(message.content)}
        for message in body.messages
    ]

    thinking_enabled = bool(
        isinstance(body.thinking, dict) and body.thinking.get("type") == "enabled"
    )
    request_id = uuid.uuid4().hex
    started = time.perf_counter()

    if not body.stream:
        result = await agent.answer(question, thinking_enabled, history=history)
        logger.info(
            "chat intent=%s duration=%.2fs provider=%s",
            result.intent,
            time.perf_counter() - started,
            result.facts.get("mode", provider.mode),
        )
        return JSONResponse(
            content={
                "id": request_id,
                "type": "message",
                "role": "assistant",
                "model": body.model,
                "content": [{"type": "text", "text": result.answer}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
            }
        )

    async def event_stream() -> AsyncIterator[str]:
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {"id": request_id, "model": body.model},
            },
        )

        # 真实进度：agent 每执行一步（解析问题/查表结构/读知识/跑指标/跑 SQL）
        # 都会推一条 stage，前端因此看到的是实际调查过程，而不是预置文案。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        outcome: dict[str, Any] = {"result": None, "error": None}

        async def progress(text: str) -> None:
            await queue.put(("stage", text))

        async def run() -> None:
            try:
                outcome["result"] = await agent.answer(
                    question,
                    thinking_enabled,
                    history=history,
                    progress=progress,
                )
            except Exception as exc:  # noqa: BLE001 - 保留可读错误
                logger.exception("agent failed")
                outcome["error"] = exc
            finally:
                await queue.put(("done", None))

        answer_task = asyncio.create_task(run())
        block_index = 0
        thinking_index: int | None = None
        first_stage = True

        if thinking_enabled:
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
            thinking_index = block_index
            block_index += 1

        while True:
            kind, payload = await queue.get()
            if kind == "done":
                break
            if thinking_index is None:
                continue
            prefix = "" if first_stage else "\n\n"
            first_stage = False
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": thinking_index,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": prefix + str(payload),
                    },
                },
            )

        if thinking_index is not None:
            yield _sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": thinking_index},
            )

        result = outcome["result"]
        error_text = (
            f"查询失败：{outcome['error']}" if outcome["error"] is not None else ""
        )
        if result is None and not error_text:
            error_text = "查询没有返回结果，请稍后重试。"

        # 保留任务引用，避免未 await 的任务被垃圾回收。
        _ = answer_task

        text_index = block_index
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": text_index,
                "content_block": {"type": "text", "text": ""},
            },
        )

        answer_text = result.answer if result else error_text
        # 逐行输出：Markdown 表格/列表按整行出现，阅读节奏接近“打字机”。
        lines = _line_chunks(answer_text)
        delay = _line_delay(len(lines))
        for line in lines:
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": line},
                },
            )
            if delay:
                await asyncio.sleep(delay)

        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": text_index},
        )
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "title": result.title if result else "查询失败",
                },
                "usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})
        logger.info(
            "chat intent=%s duration=%.2fs provider=%s",
            result.intent if result else "error",
            time.perf_counter() - started,
            provider.mode,
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8088, reload=False)
