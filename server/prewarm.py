"""启动预热：在后台为指定租户构建库表结构快照。

默认只预热当前租户（DORIS_TENANT_ID），可通过 SCHEMA_PREWARM_TENANTS 指定多个；
每个租户并行构建 Doris / MySQL 两个源的快照。预热失败只记日志，不影响 BFF 启动。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from caches import SCHEMA_CACHE
from config import settings
from investigator import DataInvestigator
from providers import get_mysql_provider, get_tenant_provider
from tenants import TENANT_CATALOG

logger = logging.getLogger("energy-prewarm")


@dataclass
class PrewarmStatus:
    state: str = "idle"  # idle | running | done | skipped | failed
    tenants: list[int] = field(default_factory=list)
    tables: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.finished_at and self.started_at:
            payload["duration_seconds"] = round(
                self.finished_at - self.started_at, 2
            )
        return payload


STATUS = PrewarmStatus()


def target_tenant_ids() -> list[int]:
    raw = settings.schema_prewarm_tenants
    ids = [int(part) for part in re.split(r"[,;\s]+", raw) if part.isdigit()]
    if ids:
        return ids
    return [settings.doris_tenant_id]


async def prewarm_schemas() -> None:
    STATUS.state = "running"
    STATUS.started_at = time.time()
    STATUS.finished_at = 0.0
    STATUS.tenants = []
    STATUS.tables = {}
    STATUS.errors = []
    if settings.data_provider != "archery":
        STATUS.state = "skipped"
        STATUS.finished_at = time.time()
        logger.info("schema prewarm skipped: provider=%s", settings.data_provider)
        return
    STATUS.tenants = target_tenant_ids()

    investigator = DataInvestigator(llm=None, config=settings)  # type: ignore[arg-type]
    for tenant_id in STATUS.tenants:
        tenant = next(
            (item for item in TENANT_CATALOG if item.tenant_id == tenant_id), None
        )
        if tenant is None:
            STATUS.errors.append(f"unknown tenant {tenant_id}")
            continue
        sources = []
        doris = get_tenant_provider(tenant)
        if doris is not None:
            sources.append(investigator.doris_source(doris))
        mysql = get_mysql_provider(tenant)
        if mysql is not None:
            sources.append(investigator.mysql_source(mysql))
        if not sources:
            STATUS.errors.append(f"tenant {tenant_id}: no source")
            continue
        results = await asyncio.gather(
            *(SCHEMA_CACHE.get(source) for source in sources),
            return_exceptions=True,
        )
        for source, result in zip(sources, results):
            label = f"{tenant.code}:{source.key}({source.database})"
            if isinstance(result, BaseException):
                STATUS.errors.append(f"{label}: {result}")
                logger.warning("schema prewarm failed for %s: %s", label, result)
            elif result is None:
                STATUS.errors.append(f"{label}: empty")
            else:
                STATUS.tables[label] = len(result.tables)
                logger.info(
                    "schema prewarm ok: %s (%d tables)", label, len(result.tables)
                )
    STATUS.state = "done" if STATUS.tables else "failed"
    STATUS.finished_at = time.time()
    logger.info(
        "schema prewarm %s in %.1fs, tables=%s, errors=%s",
        STATUS.state,
        STATUS.finished_at - STATUS.started_at,
        STATUS.tables,
        STATUS.errors[:3],
    )
