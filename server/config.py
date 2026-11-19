"""BFF / Agent 配置。

所有敏感信息只从环境变量读取，不写入前端构建产物。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # 模型代理：地址只写到 /anthropic，代码里统一追加 /v1/messages
    llm_base_url: str = field(
        default_factory=lambda: _env(
            "LLM_BASE_URL", "http://127.0.0.1:18786/llmproxy/anthropic"
        ).rstrip("/")
    )
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-flash"))
    llm_timeout_seconds: int = field(
        default_factory=lambda: _env_int("LLM_TIMEOUT_SECONDS", 90)
    )

    # 前端访问 BFF 的令牌；生产环境应换成 SSO / 网关鉴权
    bff_token: str = field(default_factory=lambda: _env("BFF_TOKEN", "energy-demo-token"))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.strip()
            for item in _env(
                "CORS_ORIGINS",
                "http://localhost:8090,http://127.0.0.1:8090",
            ).split(",")
            if item.strip()
        )
    )

    # archery | mock
    # 默认走 Archery 只读技能；本实现不提供 Doris 直连。
    data_provider: str = field(default_factory=lambda: _env("DATA_PROVIDER", "archery").lower())

    # 是否允许用户按问题里的租户名跨库查询；按需求默认开启。
    # 生产仍建议由网关/SSO 先做租户身份校验，该开关本身不是鉴权。
    allow_tenant_query: bool = field(
        default_factory=lambda: _env_bool("ALLOW_TENANT_QUERY", True)
    )

    # 自主调查：固定模板覆盖不了时，让模型自己选择工具取证。
    # AGENT_TOOL_LOOP=false 可退回“只走固定模板”的保守模式。
    agent_tool_loop: bool = field(
        default_factory=lambda: _env_bool("AGENT_TOOL_LOOP", True)
    )
    # 单次调查最多调用多少次工具（含查表结构/知识/代码/指标/SQL）。
    agent_max_steps: int = field(
        default_factory=lambda: _env_int("AGENT_MAX_STEPS", 12)
    )

    # 库表结构快照：启动时后台预热当前租户，减少第一次字段检索的等待。
    schema_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("SCHEMA_CACHE_TTL_SECONDS", 6 * 3600)
    )
    schema_prewarm: bool = field(
        default_factory=lambda: _env_bool("SCHEMA_PREWARM", True)
    )
    # 逗号分隔的租户 ID；为空时只预热当前租户（DORIS_TENANT_ID）。
    schema_prewarm_tenants: str = field(
        default_factory=lambda: _env("SCHEMA_PREWARM_TENANTS", "")
    )

    # 只读 SQL 结果短期缓存：同一 SQL 在 TTL 内复用，减少 Archery 往返。
    sql_cache_enabled: bool = field(
        default_factory=lambda: _env_bool("SQL_CACHE_ENABLED", True)
    )
    sql_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("SQL_CACHE_TTL_SECONDS", 120)
    )
    sql_cache_size: int = field(
        default_factory=lambda: _env_int("SQL_CACHE_SIZE", 200)
    )

    # 回答逐行输出的目标总时长（秒）。0 表示一次性输出；默认 8 秒，
    # 让长回答像打字一样逐步出现，避免整段刷出后滚动条瞬间到底。
    answer_stream_seconds: float = field(
        default_factory=lambda: max(
            0.0, min(_env_float("ANSWER_STREAM_SECONDS", 8.0), 60.0)
        )
    )

    # 逻辑 Doris 库名（实际查询通过 Archery 执行，不建立直连）
    doris_db: str = field(default_factory=lambda: _env("DORIS_DB", "nwdy_dm"))
    doris_tenant_id: int = field(default_factory=lambda: _env_int("DORIS_TENANT_ID", 1026))
    doris_project_id: int = field(default_factory=lambda: _env_int("DORIS_PROJECT_ID", 1))
    doris_energy_type: str = field(
        default_factory=lambda: _env("DORIS_ENERGY_TYPE", "electric")
    )

    # 线上 MySQL 业务库：与 Doris 同一个 Archery 实例，仅 db_name 不同。
    # 每个租户一个库，库名=租户 code；不建立 MySQL 直连。
    mysql_enabled: bool = field(default_factory=lambda: _env_bool("MYSQL_ENABLED", True))
    mysql_archery_instance: str = field(
        default_factory=lambda: _env("MYSQL_ARCHERY_INSTANCE", "数智能效saas现网")
    )
    mysql_db: str = field(
        default_factory=lambda: (
            _env("MYSQL_DB")
            or _env("DORIS_DB", "nwdy_dm").removesuffix("_dm")
            or "nwdy"
        )
    )

    # 演示用日期；为空时使用系统当天
    demo_today: str = field(default_factory=lambda: _env("DEMO_TODAY"))

    # /archery 技能：实例名和助手脚本目录（不保存数据库密码）
    archery_instance: str = field(
        default_factory=lambda: _env("ARCHERY_INSTANCE", "数智能效saas现网-doris")
    )
    archery_helper_dir: str = field(
        default_factory=lambda: _env(
            "ARCHERY_HELPER_DIR", r"C:\Users\2025\.dsh\skills\archery"
        )
    )


settings = Settings()
