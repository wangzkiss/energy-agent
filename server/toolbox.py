"""Agent 工具箱：把“查表结构 / 查口径知识 / 读项目代码 / 跑固定指标 / 跑只读 SQL”
封装成模型可以自己选择的工具。

设计目标：
- 模型先分析问题，再决定调哪个工具，而不是被关键词硬编码到某个固定模板；
- 表结构不靠猜：可以 find_tables -> describe_table，也可以直接读 knowledge；
- 口径不确定时可以读项目代码（只允许白名单目录），把“代码即文档”变成可查证据；
- 所有数据库访问仍然走 sqlguard 的只读校验，工具不能绕过租户/时间/限行约束。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from concepts import CONCEPTS
from knowledge import MYSQL_TABLES, TABLES
from semantic import METRICS
from sqlguard import (
    QuerySource,
    ROW_LIMIT,
    SQLGuardError,
    TABLE_NAME_PATTERN,
    validate_readonly_sql,
)
from tenants import TENANT_CATALOG

logger = logging.getLogger("energy-toolbox")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CODE_ROOTS = ("server", "docs")
CODE_EXTS = (".py", ".md")
MAX_CODE_LINES = 90
MAX_SCHEMA_ROWS = 80
TABLE_CACHE_SECONDS = 600

EXTRA_METRICS: dict[str, dict[str, str]] = {
    "floor_energy": {
        "name": "楼层用电",
        "description": "从表计名称解析楼层号并聚合用电，返回覆盖率、未识别电量，用于楼层排名和对比。",
        "aliases": "楼层 哪层 哪个楼层 楼层排名 楼层对比 每层",
        "args": '"metric":"floor_energy","start":"YYYY-MM-DD","end":"YYYY-MM-DD"',
        "caveats": "楼层号来自 meter_name 正则解析（19层xx / A2栋36-8），并按栋拆分；不是结构化字段，结论必须写覆盖率。",
    },
    "space_energy": {
        "name": "空间/楼层用电",
        "description": "按 space_name/space_code 聚合表计日用电，并返回空间覆盖数，用于楼层/空间排名和对比。",
        "aliases": "楼层 楼栋 区域 空间 分组 排名 对比 哪层 哪个楼层",
        "args": '"metric":"space_energy","start":"YYYY-MM-DD","end":"YYYY-MM-DD"',
        "caveats": "space_name 只有 A1座 等少量取值时，说明楼层维度未拆分，不能拿表计排名冒充楼层排名。",
    },
    "device_readings": {
        "name": "设备点位统计",
        "description": "按设备名称/空间匹配设备，汇总最近窗口内每个点位的均值/极值/样本数/活跃小时数。",
        "aliases": "室温 温度 压力 点位 上报 设备数据 开关机 运行状态",
        "args": '"metric":"device_readings","keyword":"36层","point_codes":"RoomTemp,RoomTempSp","hours":24',
        "caveats": "点位表没有 tenant_id，必须由工具内部按设备台账筛租户；0 值可能是采集异常。",
    },
    "cold_station_devices": {
        "name": "冷站设备分类",
        "description": (
            "按冷站统计设备分类数量（冷机/冷冻水泵/冷却水泵/冷却塔等），"
            "来自 eo_energy_saving_cooling_device_configs 关联 eo_devices。"
        ),
        "aliases": "冷站设备 设备类型 设备分类 有哪些设备 设备清单",
        "args": '"metric":"cold_station_devices"',
        "caveats": (
            "分类名来自 eo_devices.device_category_name；综合冷机是汇总站，"
            "设备数会和其它站重复，不能相加。"
        ),
    },
    "cold_station_scop": {
        "name": "冷站 SCOP",
        "description": (
            "按冷站配置、冷量计（energy_type=cold）和电表名称归集，计算每个冷站的"
            "冷量 kWh、用电 kWh、SCOP=冷量/用电，并标记无冷量/电量存疑。"
        ),
        "aliases": "scop 冷站能效 系统能效 冷站效率 冷站COP 冷量比",
        "args": '"metric":"cold_station_scop","start":"YYYY-MM-DD","end":"YYYY-MM-DD"',
        "caveats": (
            "冷量计和电表按名称归到冷站，属于名称归集不是结构化映射；"
            "工艺/空调分站名不清晰时必须写明归集假设；电量≈0 或 SCOP 异常高要标记为不可用。"
        ),
    },
    "running_devices": {
        "name": "持续运行设备",
        "description": "按 FanStartSwitchSp/ModeSp/ActivePowerSum 的活跃小时占比，找出窗口内持续运行的设备。",
        "aliases": "一直运行 持续运行 哪些设备在运行 常开 长期开机 运行设备",
        "args": '"metric":"running_devices","hours":168,"min_ratio":0.8',
        "caveats": "活跃占比是统计判据，不等于设备一定在制冷；要写明窗口和阈值。",
    },
}

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass
class ToolOutcome:
    tool: str
    ok: bool
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def as_observation(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "result": self.summary,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def compact_value(value: Any, limit: int = 240) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def compact_rows(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if isinstance(row, dict):
            compact.append({str(key): compact_value(value) for key, value in row.items()})
        else:
            compact.append({"value": compact_value(row)})
    return compact


def _rows_to_text(rows: list[dict[str, Any]], limit: int = 20) -> str:
    if not rows:
        return "（0 行）"
    payload = compact_rows(rows, limit)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(rows) > limit:
        text += f"\n（共 {len(rows)} 行，只展示前 {limit} 行）"
    return text


def _parse_day(value: Any, default: date) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return default


def _keywords(text: str) -> list[str]:
    """把中文问题切成 2 字片段，英文/字段名整体保留；用于本地知识检索。"""
    lowered = (text or "").lower()
    tokens = re.findall(r"[a-z_][a-z0-9_]{1,}", lowered)
    for run in re.findall(r"[\u4e00-\u9fa5]{2,}", lowered):
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    seen: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.append(token)
    return seen


def _score(haystack: str, tokens: list[str]) -> int:
    lowered = haystack.lower()
    return sum(1 for token in tokens if token in lowered)


class ToolboxError(RuntimeError):
    pass


class AgentToolbox:
    """一次提问内的工具集合；不跨请求复用可变状态。"""

    TOOL_NAMES = (
        "metric",
        "run_sql",
        "find_tables",
        "search_columns",
        "describe_table",
        "table_profile",
        "knowledge",
        "read_code",
        "tenants",
    )

    def __init__(
        self,
        *,
        sources: list[QuerySource],
        today: date,
        row_limit: int = ROW_LIMIT,
        progress: ProgressCallback | None = None,
        schema_cache: Any | None = None,
        sql_cache: Any | None = None,
    ) -> None:
        self.sources = {source.key: source for source in sources}
        self.today = today
        self.row_limit = row_limit
        self.progress = progress
        self.schema_cache = schema_cache
        self.sql_cache = sql_cache
        self._schema_tasks: dict[str, asyncio.Task[Any]] = {}
        self._table_cache: dict[str, tuple[float, list[str]]] = {}
        self._describe_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._profile_cache: dict[tuple[str, str], ToolOutcome] = {}
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def source(self, key: str | None) -> QuerySource:
        if not self.sources:
            raise ToolboxError("当前没有可用数据源")
        resolved = (key or next(iter(self.sources))).strip().lower()
        source = self.sources.get(resolved)
        if source is None:
            raise ToolboxError(
                "未知数据源 " + repr(resolved) + "；可用：" + " / ".join(self.sources)
            )
        return source

    async def emit(self, text: str) -> None:
        if self.progress is None:
            return
        try:
            await self.progress(text)
        except Exception:  # noqa: BLE001 - 进度推送失败不能影响查询
            logger.debug("progress callback failed", exc_info=True)

    def catalog_text(self) -> str:
        sources = "、".join(
            f"{source.key}={source.database}" for source in self.sources.values()
        )
        return f"""可用工具（只读）：
1. metric —— 已注册指标的固定口径查询，优先使用。
   参数：{{"metric":"energy_total|energy_trend|energy_ranking|control_success|alarm_stats|room_temperature|device_count|meter_count|cold_station_config|data_quality|space_energy|floor_energy|device_readings|running_devices","start":"YYYY-MM-DD","end":"YYYY-MM-DD","keyword":"可选"}}
   - space_energy：按 space_name/space_code 聚合用电，返回空间覆盖数。
   - floor_energy：按 meter_name 解析楼层并聚合用电，返回覆盖率；楼层排名/对比优先用它。
   - device_readings：按设备名称/空间查点位统计（室温、压力、开关机等）；
     参数额外支持 "point_codes":"RoomTemp,RoomTempSp" 与 "hours":24。
     点位表没有 tenant_id，直接 run_sql 会被校验拒绝，请用这个工具。
   - running_devices：窗口内持续运行的设备（按 FanStartSwitchSp/ModeSp/ActivePowerSum
     的活跃小时占比）；参数支持 "hours":168、"min_ratio":0.8。
2. run_sql —— 执行一条只读 SQL（会强制校验）。
   参数：{{"source":"doris|mysql","sql":"SELECT ..."}}
3. find_tables —— 按关键词找表，返回表名和已知用途。
   参数：{{"keyword":"meter|楼层|告警","source":"可选"}}
4. search_columns —— 在真实 information_schema 里按字段名/字段注释搜表和字段；
   找不到表、或不知道某个业务词落在哪张表时先用它（如 SCOP、冷量、需量）。
   默认读本地 schema 快照（6 小时 TTL），怀疑快照过期时加 "refresh":true。
   参数：{{"keyword":"SCOP 能效","source":"可选，缺省搜全部数据源"}}
5. describe_table —— 看真实表结构（字段/类型/注释）。
   参数：{{"table":"dm_eo_e_meter_di","source":"可选"}}
6. table_profile —— 判断一张表“有没有数据、覆盖什么时间、是不是空表”。
   参数：{{"table":"algo_overview_kpi","source":"可选"}}
   返回：行数、时间字段与范围、租户数；0 行会明确标“空表”，不能当 0 值。
7. knowledge —— 检索本地库表知识/指标口径/边界（不查库）。
   参数：{{"query":"楼层 用电 口径"}}
8. read_code —— 读项目代码/文档，确认口径和实现（白名单目录）。
   参数：{{"query":"energy_total","path":"server/providers.py 可选"}}
9. tenants —— 租户与 MySQL/Doris 库映射。
   参数：{{}}

当前数据源：{sources}"""

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------
    async def execute(self, name: str, args: dict[str, Any] | None) -> ToolOutcome:
        tool = (name or "").strip()
        payload = args if isinstance(args, dict) else {}
        started = time.perf_counter()
        await self.emit(self._progress_text(tool, payload))
        try:
            if tool == "metric":
                outcome = await self._metric(payload)
            elif tool == "run_sql":
                outcome = await self._run_sql(payload)
            elif tool == "find_tables":
                outcome = await self._find_tables(payload)
            elif tool == "search_columns":
                outcome = await self._search_columns(payload)
            elif tool == "describe_table":
                outcome = await self._describe_table(payload)
            elif tool == "table_profile":
                outcome = await self._table_profile(payload)
            elif tool == "knowledge":
                outcome = self._knowledge(payload)
            elif tool == "read_code":
                outcome = self._read_code(payload)
            elif tool == "tenants":
                outcome = self._tenants(payload)
            else:
                raise ToolboxError(
                    f"未知工具 {tool!r}；可用工具：{', '.join(self.TOOL_NAMES)}"
                )
        except (ToolboxError, SQLGuardError) as exc:
            outcome = ToolOutcome(tool=tool, ok=False, summary=str(exc), error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 把失败反馈给模型修正
            logger.warning("tool %s failed: %s", tool, exc)
            outcome = ToolOutcome(
                tool=tool,
                ok=False,
                summary=f"工具执行失败：{exc}",
                error=str(exc),
            )
        outcome.facts.setdefault("tool", tool)
        outcome.facts.setdefault("args", payload)
        outcome.facts.setdefault("duration_ms", int((time.perf_counter() - started) * 1000))
        self.calls.append(
            {
                "tool": tool,
                "args": payload,
                "ok": outcome.ok,
                "error": outcome.error,
                "duration_ms": outcome.facts["duration_ms"],
            }
        )
        return outcome

    @staticmethod
    def _progress_text(tool: str, args: dict[str, Any]) -> str:
        if tool == "run_sql":
            sql = " ".join(str(args.get("sql") or "").split())
            return f"执行只读 SQL：{sql[:120]}"
        if tool == "metric":
            return f"查询指标：{args.get('metric') or '?'}"
        if tool == "find_tables":
            return f"查找表：{args.get('keyword') or '全部'}"
        if tool == "search_columns":
            return f"搜索字段：{args.get('keyword') or '?'}"
        if tool == "describe_table":
            return f"读取表结构：{args.get('table') or '?'}"
        if tool == "table_profile":
            return f"分析表数据：{args.get('table') or '?'}"
        if tool == "knowledge":
            return f"检索知识库：{args.get('query') or ''}"[:80]
        if tool == "read_code":
            return f"阅读代码：{args.get('path') or args.get('query') or ''}"[:80]
        if tool == "tenants":
            return "读取租户与数据库映射"
        return f"调用工具：{tool}"

    # ------------------------------------------------------------------
    # 工具实现
    # ------------------------------------------------------------------
    async def _run_sql(self, args: dict[str, Any]) -> ToolOutcome:
        source = self.source(args.get("source"))
        raw_sql = args.get("sql") or args.get("query")
        if not isinstance(raw_sql, str) or not raw_sql.strip():
            raise ToolboxError("run_sql 需要 sql 参数")
        safe_sql = validate_readonly_sql(
            raw_sql,
            database=source.database,
            tenant_id=source.tenant_id,
            fact_tables=source.fact_tables,
            require_tenant=source.require_tenant,
            forbidden_databases=source.forbidden_databases,
        )
        cache_key = None
        if self.sql_cache is not None:
            cache_key = self.sql_cache.key(
                source.key, source.database, safe_sql, self.row_limit
            )
            hit = self.sql_cache.get(cache_key)
            if hit is not None:
                rows, age = hit
                return ToolOutcome(
                    tool="run_sql",
                    ok=True,
                    summary=(
                        f"{len(rows)} 行（SQL 缓存命中，{age:.0f}s 前的结果）\n"
                        + _rows_to_text(rows, limit=20)
                    ),
                    facts={
                        "source": source.key,
                        "database": source.database,
                        "sql": safe_sql,
                        "row_count": len(rows),
                        "cached": True,
                        "cache_age_seconds": round(age, 1),
                    },
                    rows=rows,
                )
        rows = await asyncio.wait_for(
            source.provider.ad_hoc_query(safe_sql, limit=self.row_limit),
            timeout=60,
        )
        if cache_key is not None:
            self.sql_cache.put(cache_key, rows)
        summary = f"{len(rows)} 行\n" + _rows_to_text(rows, limit=20)
        return ToolOutcome(
            tool="run_sql",
            ok=True,
            summary=summary,
            facts={
                "source": source.key,
                "database": source.database,
                "sql": safe_sql,
                "row_count": len(rows),
                "cached": False,
            },
            rows=rows,
        )

    async def _table_names(self, source: QuerySource) -> list[str]:
        cached = self._table_cache.get(source.key)
        now = time.time()
        if cached and now - cached[0] < TABLE_CACHE_SECONDS:
            return cached[1]
        names: list[str] = []
        try:
            rows = await asyncio.wait_for(
                source.provider.ad_hoc_query("SHOW TABLES", limit=500), timeout=60
            )
            for row in rows:
                for value in row.values():
                    if isinstance(value, str):
                        names.append(value)
                        break
        except Exception as exc:  # noqa: BLE001 - 取不到真实表清单时退回知识库
            logger.warning("SHOW TABLES failed on %s: %s", source.database, exc)
            catalog = TABLES if source.key == "doris" else MYSQL_TABLES
            names = list(catalog)
        names = sorted({name for name in names if TABLE_NAME_PATTERN.match(name)})
        self._table_cache[source.key] = (now, names)
        return names

    async def _find_tables(self, args: dict[str, Any]) -> ToolOutcome:
        keyword = str(args.get("keyword") or "").strip().lower()
        source_keys = (
            [str(args.get("source")).lower()]
            if args.get("source")
            else list(self.sources)
        )
        lines: list[str] = []
        found: list[str] = []
        for key in source_keys:
            source = self.source(key)
            snapshot = self._peek_schema(source)
            if snapshot is None:
                self._warm_schema(source)
            names = (
                snapshot.table_names()
                if snapshot is not None
                else await self._table_names(source)
            )
            catalog = TABLES if key == "doris" else MYSQL_TABLES
            matches = []
            for name in names:
                meta = catalog.get(name) or {}
                haystack = (
                    name
                    + " "
                    + str(meta.get("purpose") or "")
                    + " "
                    + " ".join((meta.get("key_fields") or {}).keys())
                ).lower()
                if not keyword or keyword in haystack:
                    matches.append((name, meta))
            if matches:
                lines.append(
                    f"[{source.key}/{source.database}] 精选知识库匹配 {len(matches)} 张表，"
                    f"库中共 {len(names)} 张："
                )
                for name, meta in matches[:40]:
                    purpose = str(meta.get("purpose") or "")[:90]
                    lines.append(f"- {name}: {purpose}".rstrip(": "))
                    found.append(name)
                if len(matches) > 40:
                    lines.append(f"……另有 {len(matches) - 40} 张未列出，请缩小关键词")
            # 精选目录只是索引，不是全库；关键词不命中时再去真实 information_schema 兜底。
            extra: dict[str, list[str]] = {}
            if keyword and len(matches) < 3:
                tokens = [
                    token for token in re.split(r"[\s,，、;；]+", keyword) if token
                ][:5]
                safe = [
                    token
                    for token in tokens
                    if re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{1,40}", token)
                ]
                hits: list[dict[str, Any]] = []
                if safe:
                    try:
                        hits = await self._column_hits(source, safe)
                    except Exception as exc:  # noqa: BLE001 - 元数据失败不阻断
                        lines.append(f"[{source.key}/{source.database}] 字段兜底失败：{exc}")
                seen = {name for name, _ in matches}
                for row in hits:
                    table_name = str(row.get("TABLE_NAME") or "")
                    column_name = str(row.get("COLUMN_NAME") or "")
                    if not TABLE_NAME_PATTERN.match(table_name) or table_name in seen:
                        continue
                    extra.setdefault(table_name, []).append(column_name)
                if extra:
                    lines.append(
                        f"[{source.key}/{source.database}] 真实库字段兜底"
                        f"（精选知识库未收录 {len(extra)} 张）："
                    )
                    for table_name, columns in list(extra.items())[:20]:
                        lines.append(
                            f"- {table_name}: 命中字段 {'、'.join(columns[:3])}"
                        )
                        found.append(table_name)
            if not matches and not extra:
                lines.append(
                    f"[{source.key}/{source.database}] 没有匹配 {keyword!r} 的表或字段"
                )
        return ToolOutcome(
            tool="find_tables",
            ok=True,
            summary="\n".join(lines),
            facts={"keyword": keyword, "tables": found},
        )

    def _peek_schema(self, source: QuerySource) -> Any | None:
        if self.schema_cache is None:
            return None
        try:
            return self.schema_cache.peek(source)
        except Exception:  # noqa: BLE001 - 缓存异常按无缓存处理
            return None

    def _warm_schema(self, source: QuerySource) -> None:
        """首次没快照时后台构建，不阻塞当前查询；下次直接命中。"""
        if self.schema_cache is None:
            return
        key = f"{source.key}:{source.database}"
        task = self._schema_tasks.get(key)
        if task is not None and not task.done():
            return
        self._schema_tasks[key] = asyncio.create_task(self.schema_cache.get(source))

    async def _schema(self, source: QuerySource, refresh: bool = False) -> Any | None:
        """取库表结构快照；没有缓存或构建失败时返回 None，调用方回退实时查询。"""
        if self.schema_cache is None:
            return None
        try:
            snapshot = await self.schema_cache.get(source, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - 缓存故障不能影响调查
            logger.warning("schema snapshot failed on %s: %s", source.database, exc)
            return None
        if snapshot is None or not getattr(snapshot, "tables", None):
            return None
        return snapshot

    async def _column_hits(
        self, source: QuerySource, tokens: list[str]
    ) -> list[dict[str, Any]]:
        """在 information_schema 中按字段名/注释命中，只读元数据；优先读快照。"""
        snapshot = self._peek_schema(source)
        if snapshot is not None:
            return snapshot.search(tokens)
        self._warm_schema(source)
        # 用 INSTR 而不是 LIKE '%kw%'：Archery 的 %s 绑定会把 "%s..." 误当占位符。
        conditions = " OR ".join(
            f"INSTR(COLUMN_NAME, '{token}') > 0 OR INSTR(COLUMN_COMMENT, '{token}') > 0"
            for token in tokens
        )
        sql = (
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
            f"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = '{source.database}' "
            f"AND ({conditions}) ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT 100"
        )
        return await source.provider.ad_hoc_query(sql, limit=100)

    async def _search_columns(self, args: dict[str, Any]) -> ToolOutcome:
        """在真实 information_schema 中按字段名/注释检索，弥补本地知识库的盲区。"""
        keyword = str(args.get("keyword") or "").strip()
        if not keyword:
            raise ToolboxError("search_columns 需要 keyword（字段名或中文业务词）")
        tokens = [token for token in re.split(r"[\s,，、;；]+", keyword) if token][:5]
        safe = [
            token
            for token in tokens
            if re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{1,40}", token)
        ]
        if not safe:
            raise ToolboxError("关键词只支持中英文、数字、下划线")
        source_keys = (
            [str(args.get("source")).lower()]
            if args.get("source")
            else list(self.sources)
        )
        lines: list[str] = []
        matches: list[dict[str, Any]] = []
        refresh = bool(args.get("refresh"))
        for key in source_keys:
            source = self.source(key)
            if refresh:
                await self._schema(source, refresh=True)
            else:
                self._warm_schema(source)
            try:
                rows = await self._column_hits(source, safe)
            except Exception as exc:  # noqa: BLE001 - 元数据查询失败要反馈给模型
                lines.append(f"[{source.key}/{source.database}] 元数据查询失败：{exc}")
                continue
            if not rows:
                lines.append(
                    f"[{source.key}/{source.database}] 字段名/注释没有匹配 {'/'.join(safe)}"
                )
                continue
            lines.append(f"[{source.key}/{source.database}] 命中 {len(rows)} 个字段：")
            for row in rows[:60]:
                table_name = str(row.get("TABLE_NAME") or "")
                column_name = str(row.get("COLUMN_NAME") or "")
                comment = str(row.get("COLUMN_COMMENT") or "")
                lines.append(f"- {table_name}.{column_name} {comment}".rstrip())
                matches.append(
                    {
                        "source": source.key,
                        "table": table_name,
                        "column": column_name,
                        "comment": comment,
                    }
                )
            if len(rows) > 60:
                lines.append(f"……另有 {len(rows) - 60} 个未列出，请缩小关键词")
        if not lines:
            lines.append("没有可用数据源")
        return ToolOutcome(
            tool="search_columns",
            ok=True,
            summary="\n".join(lines),
            facts={"keyword": keyword, "matches": matches[:60]},
        )

    async def _describe_table(self, args: dict[str, Any]) -> ToolOutcome:
        table = str(args.get("table") or "").strip()
        if not TABLE_NAME_PATTERN.match(table):
            raise ToolboxError("describe_table 需要合法的表名")
        source = self.source(args.get("source"))
        cache_key = (source.key, table)
        rows = self._describe_cache.get(cache_key)
        note = ""
        if rows is None:
            try:
                rows = await asyncio.wait_for(
                    source.provider.ad_hoc_query(
                        f"SHOW FULL COLUMNS FROM {source.database}.{table}"
                    ),
                    timeout=60,
                )
            except Exception as exc:  # noqa: BLE001 - 真实结构拿不到时用快照/知识库兜底
                snapshot = self._peek_schema(source)
                cached_columns = snapshot.columns(table) if snapshot else []
                if cached_columns:
                    note = f"（实时表结构读取失败：{exc}，以下为 schema 快照）"
                    rows = [
                        {
                            "Field": column.get("name"),
                            "Type": column.get("type"),
                            "Comment": column.get("comment"),
                        }
                        for column in cached_columns
                    ]
                else:
                    note = f"（真实表结构读取失败：{exc}，以下为知识库记录）"
                    rows = self._knowledge_columns(source.key, table)
            self._describe_cache[cache_key] = rows
        catalog = TABLES if source.key == "doris" else MYSQL_TABLES
        meta = catalog.get(table) or {}
        lines = [f"{source.database}.{table}{note}"]
        if meta.get("purpose"):
            lines.append(f"用途：{meta['purpose']}")
        if meta.get("grain"):
            lines.append(f"粒度：{meta['grain']}")
        if rows and "Field" in (rows[0] or {}):
            lines.append("字段 | 类型 | 注释")
            for row in rows[:MAX_SCHEMA_ROWS]:
                lines.append(
                    " | ".join(
                        [
                            str(compact_value(row.get("Field"), 60)),
                            str(compact_value(row.get("Type"), 60)),
                            str(compact_value(row.get("Comment"), 120)),
                        ]
                    )
                )
            if len(rows) > MAX_SCHEMA_ROWS:
                lines.append(f"……还有 {len(rows) - MAX_SCHEMA_ROWS} 个字段")
        elif rows:
            lines.append("字段：" + "、".join(str(row.get("field") or row.get("Field")) for row in rows))
        else:
            lines.append("没有字段信息；请改用 find_tables 或直接查一条 SELECT 确认。")
        caveats = meta.get("caveats") or []
        if caveats:
            lines.append("已知口径边界：")
            lines.extend(f"- {item}" for item in caveats)
        return ToolOutcome(
            tool="describe_table",
            ok=True,
            summary="\n".join(lines),
            facts={"source": source.key, "database": source.database, "table": table},
            rows=rows,
        )

    async def _table_profile(self, args: dict[str, Any]) -> ToolOutcome:
        """判断表是否为空、时间覆盖范围、租户覆盖，避免把空表当 0 值。"""
        table = str(args.get("table") or "").strip()
        if not TABLE_NAME_PATTERN.match(table):
            raise ToolboxError("table_profile 需要合法的表名")
        source = self.source(args.get("source"))
        cache_key = (source.key, table)
        cached = self._profile_cache.get(cache_key)
        if cached is not None:
            return cached
        full = f"{source.database}.{table}"
        count_rows = await asyncio.wait_for(
            source.provider.ad_hoc_query(
                f"SELECT COUNT(*) AS rows_cnt FROM {full}", limit=1
            ),
            timeout=60,
        )
        count = 0
        if count_rows:
            count = int(next(iter(count_rows[0].values())) or 0)
        catalog = TABLES if source.key == "doris" else MYSQL_TABLES
        meta = catalog.get(table) or {}
        time_field = next(
            (field for field in meta.get("time_fields") or [] if isinstance(field, str)),
            "",
        )
        if not time_field:
            snapshot = self._peek_schema(source)
            time_field = snapshot.time_column(table) if snapshot else ""
        if not time_field:
            try:
                columns = await source.provider.ad_hoc_query(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    f"WHERE TABLE_SCHEMA = '{source.database}' AND TABLE_NAME = '{table}' "
                    "AND DATA_TYPE IN ('datetime','timestamp','date') "
                    "ORDER BY ORDINAL_POSITION LIMIT 1",
                    limit=1,
                )
                if columns:
                    time_field = str(columns[0].get("COLUMN_NAME") or "")
            except Exception:  # noqa: BLE001 - 元数据不可用时不影响行数结论
                time_field = ""
        span = ""
        if time_field and re.fullmatch(r"[A-Za-z0-9_]{1,64}", time_field):
            try:
                span_rows = await source.provider.ad_hoc_query(
                    f"SELECT MIN({time_field}) AS min_t, MAX({time_field}) AS max_t "
                    f"FROM {full}",
                    limit=1,
                )
                if span_rows:
                    min_t = span_rows[0].get("min_t")
                    max_t = span_rows[0].get("max_t")
                    if min_t or max_t:
                        span = f"；{time_field} 范围 {str(min_t)[:19]} ~ {str(max_t)[:19]}"
            except Exception:  # noqa: BLE001 - 范围读不到时只给行数
                span = ""
        tenant_count: int | None = None
        try:
            tenant_rows = await source.provider.ad_hoc_query(
                f"SELECT COUNT(DISTINCT tenant_id) AS tenant_cnt FROM {full}", limit=1
            )
            if tenant_rows:
                tenant_count = int(tenant_rows[0].get("tenant_cnt") or 0)
        except Exception:  # noqa: BLE001 - 没有 tenant_id 列是正常情况
            tenant_count = None
        status = "空表（未接入/无数据）" if count == 0 else "有数据"
        lines = [f"{full}：{count} 行，{status}{span}"]
        if tenant_count is not None:
            lines.append(f"tenant_id 覆盖 {tenant_count} 个租户")
        if count == 0:
            lines.append("0 行只表示这张表没接入/没数据，不能当 0 值；请换数据路径。")
        outcome = ToolOutcome(
            tool="table_profile",
            ok=True,
            summary="\n".join(lines),
            facts={
                "source": source.key,
                "database": source.database,
                "table": table,
                "row_count": count,
                "time_field": time_field,
                "tenant_count": tenant_count,
                "empty": count == 0,
            },
        )
        self._profile_cache[cache_key] = outcome
        return outcome

    def _knowledge_columns(self, source_key: str, table: str) -> list[dict[str, Any]]:
        catalog = TABLES if source_key == "doris" else MYSQL_TABLES
        meta = catalog.get(table) or {}
        fields = meta.get("key_fields") or {}
        return [{"field": name, "type": "", "comment": desc} for name, desc in fields.items()]

    def _knowledge(self, args: dict[str, Any]) -> ToolOutcome:
        query = str(args.get("query") or args.get("topic") or "").strip()
        tokens = _keywords(query) or [query.lower()]
        scored: list[tuple[int, str]] = []

        for name, meta in TABLES.items():
            blob = (
                name
                + " "
                + str(meta.get("purpose") or "")
                + " "
                + " ".join((meta.get("key_fields") or {}).keys())
                + " "
                + " ".join((meta.get("key_fields") or {}).values())
                + " "
                + " ".join(meta.get("caveats") or [])
                + " "
                + " ".join(meta.get("used_for") or [])
            )
            score = _score(blob, tokens) + (6 if name in query.lower() else 0)
            if score:
                scored.append((score, f"Doris 表 {name}"))
        for name, meta in MYSQL_TABLES.items():
            blob = (
                name
                + " "
                + str(meta.get("purpose") or "")
                + " "
                + " ".join((meta.get("key_fields") or {}).keys())
                + " "
                + " ".join((meta.get("key_fields") or {}).values())
            )
            score = _score(blob, tokens) + (6 if name in query.lower() else 0)
            if score:
                scored.append((score, f"MySQL 表 {name}"))
        for key, metric in METRICS.items():
            blob = (
                key
                + " "
                + metric.name
                + " "
                + metric.description
                + " "
                + " ".join(metric.aliases)
                + " "
                + " ".join(metric.caveats)
            )
            score = _score(blob, tokens)
            if score:
                scored.append((score, f"指标 {metric.name}（{key}）"))
        for key, extra in EXTRA_METRICS.items():
            blob = " ".join(
                [key, extra["name"], extra["description"], extra["aliases"], extra["caveats"]]
            )
            score = _score(blob, tokens)
            if score:
                scored.append((score, f"工具指标 {extra['name']}（{key}）"))
        for key, item in CONCEPTS.items():
            blob = " ".join(
                [
                    key,
                    item["title"],
                    item["definition"],
                    item["formula"],
                    item["data"],
                    " ".join(item["keywords"]),
                    " ".join(item["caveats"]),
                ]
            )
            score = _score(blob, tokens)
            if score:
                scored.append((score, f"业务口径 {item['title']}（{key}）"))

        lines: list[str] = []
        for _, label in sorted(scored, key=lambda item: -item[0])[:4]:
            lines.append(self._knowledge_block(label))
        if not lines:
            lines.append(
                "知识库没有直接匹配。可先 find_tables 或 describe_table 确认表，"
                "再用 run_sql 小范围求证。"
            )
        return ToolOutcome(
            tool="knowledge",
            ok=True,
            summary="\n\n".join(lines),
            facts={"query": query},
        )

    @staticmethod
    def _scop_rows(
        stations: list[dict[str, Any]],
        cold_rows: list[dict[str, Any]],
        elec_rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按冷站名称与表计名称的共同关键词归集冷量/电量，返回每个冷站的 SCOP。"""
        vocab = ("工艺", "空调", "冷源", "综合")

        def tokens(name: str) -> list[str]:
            return [word for word in vocab if word in name]

        tagged = [(station, tokens(str(station.get("cooling_name") or ""))) for station in stations]

        def best_station(meter_name: str, prefix_bonus: bool) -> int | None:
            best: int | None = None
            best_score = 0
            for index, (_, station_tokens) in enumerate(tagged):
                score = 0
                for word in station_tokens:
                    if word in meter_name:
                        score += len(word)
                        if prefix_bonus and meter_name.startswith(word):
                            score += 3
                if prefix_bonus and station_tokens and meter_name.startswith(station_tokens[0]):
                    # 站名第一个词就是表计名前缀时最具区分度（工艺… / 空调…）。
                    score += 5
                if score > best_score:
                    best, best_score = index, score
                elif score == best_score and score > 0 and best is not None:
                    # 同分时选更“具体”的冷站（关键词更少），避免“工艺空调系统”
                    # 把纯“空调”表计抢走。
                    if len(station_tokens) < len(tagged[best][1]):
                        best = index
            return best

        cold_totals = [0.0] * len(tagged)
        elec_totals = [0.0] * len(tagged)
        unassigned: list[dict[str, Any]] = []
        for row in cold_rows:
            name = str(row.get("meter_name") or "")
            index = best_station(name, prefix_bonus=False)
            if index is None:
                continue
            cold_totals[index] += float(row.get("kwh") or 0)
        for row in elec_rows:
            name = str(row.get("meter_name") or "")
            index = best_station(name, prefix_bonus=True)
            if index is None:
                if float(row.get("kwh") or 0) > 0:
                    unassigned.append({"meter_name": name, "kwh": float(row.get("kwh") or 0)})
                continue
            elec_totals[index] += float(row.get("kwh") or 0)

        cold_all = round(sum(float(row.get("kwh") or 0) for row in cold_rows), 2)
        elec_all = round(sum(float(row.get("kwh") or 0) for row in elec_rows), 2)
        out: list[dict[str, Any]] = []
        for index, (station, station_tokens) in enumerate(tagged):
            cold = round(cold_totals[index], 2)
            elec = round(elec_totals[index], 2)
            aggregate = not station_tokens or station_tokens == ["综合"]
            if aggregate:
                cold, elec = cold_all, elec_all
            scop = round(cold / elec, 2) if cold > 0 and elec > 0 else None
            if aggregate:
                status = "汇总口径（全部冷站）"
            elif cold <= 0:
                status = "无冷量数据"
            elif elec <= 0:
                status = "无用电数据"
            elif scop is not None and scop > 8:
                # 冷站 SCOP 正常上限约 6~7；>8 基本是电表覆盖不全或冷量计偏大。
                status = "电量存疑（SCOP 高于 8，电表覆盖可能不全）"
            else:
                status = "可用"
            out.append(
                {
                    "冷站": station.get("cooling_name") or station.get("application_id"),
                    "application_id": station.get("application_id"),
                    "冷量kWh": cold,
                    "用电kWh": elec,
                    "SCOP": scop,
                    "状态": status,
                }
            )
        return out, unassigned

    @staticmethod
    def _knowledge_block(label: str) -> str:
        if label.startswith("Doris 表 ") or label.startswith("MySQL 表 "):
            name = label.split(" ", 2)[2]
            catalog = TABLES if label.startswith("Doris") else MYSQL_TABLES
            meta = catalog.get(name) or {}
            fields = meta.get("key_fields") or {}
            parts = [f"### {label}"]
            if meta.get("purpose"):
                parts.append(f"用途：{meta['purpose']}")
            if meta.get("grain"):
                parts.append(f"粒度：{meta['grain']}")
            if fields:
                parts.append(
                    "字段：" + "；".join(f"{key}({value})" for key, value in fields.items())
                )
            for caveat in meta.get("caveats") or []:
                parts.append(f"边界：{caveat}")
            if meta.get("not_for"):
                parts.append("不能这样用：" + "；".join(meta["not_for"]))
            return "\n".join(parts)

        key = label.split("（")[1].rstrip("）")
        if label.startswith("业务口径 "):
            item = CONCEPTS.get(key) or {}
            return "\n".join(
                [
                    f"### {label}",
                    f"定义：{item.get('definition', '')}",
                    f"公式：{item.get('formula', '')}",
                    f"本项目情况：{item.get('data', '')}",
                    "边界：" + "；".join(item.get("caveats") or []),
                    "相关表：" + "、".join(item.get("tables") or []),
                ]
            )
        if label.startswith("工具指标 "):
            extra = EXTRA_METRICS.get(key) or {}
            return "\n".join(
                [
                    f"### {label}",
                    f"定义：{extra.get('description', '')}",
                    f"调用：metric={key}",
                    f"参数：{extra.get('args', '')}",
                    f"边界：{extra.get('caveats', '')}",
                ]
            )
        metric = METRICS.get(key)
        if metric is None:
            return label
        return "\n".join(
            [
                f"### {label}",
                f"定义：{metric.description}",
                f"数据源：{metric.source}；单位：{metric.unit or '无'}；新鲜度：{metric.freshness}",
                f"相关表：{', '.join(metric.tables)}",
                "口径边界：" + "；".join(metric.caveats),
            ]
        )

    def _read_code(self, args: dict[str, Any]) -> ToolOutcome:
        query = str(args.get("query") or "").strip()
        raw_path = str(args.get("path") or "").strip().replace("\\", "/")
        if not query and not raw_path:
            raise ToolboxError("read_code 需要 query 或 path")
        if raw_path:
            path = (PROJECT_ROOT / raw_path).resolve()
            if not self._allowed(path):
                raise ToolboxError(f"只允许读取项目内的 {', '.join(CODE_ROOTS)} 下的代码/文档")
            files = [path]
        else:
            files = [
                path
                for root in CODE_ROOTS
                for path in sorted((PROJECT_ROOT / root).rglob("*"))
                if path.is_file() and path.suffix in CODE_EXTS and self._allowed(path)
            ]
        lines_out: list[str] = []
        matched_files = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            source_lines = text.splitlines()
            hits = [
                index
                for index, line in enumerate(source_lines)
                if query.lower() in line.lower()
            ] if query else list(range(min(len(source_lines), 40)))
            if not hits:
                continue
            matched_files += 1
            lines_out.append(f"### {path.relative_to(PROJECT_ROOT).as_posix()}")
            shown: set[int] = set()
            for hit in hits[:4]:
                start = max(0, hit - 4)
                end = min(len(source_lines), hit + 5)
                for index in range(start, end):
                    if index in shown:
                        continue
                    shown.add(index)
                    lines_out.append(f"{index + 1:>4}: {source_lines[index]}")
                    if len(lines_out) >= MAX_CODE_LINES:
                        break
                if len(lines_out) >= MAX_CODE_LINES:
                    break
            if len(lines_out) >= MAX_CODE_LINES or matched_files >= 3:
                break
        if not lines_out:
            return ToolOutcome(
                tool="read_code",
                ok=True,
                summary=f"没有在项目代码/文档里找到 {query!r}，换个关键词或直接查库确认。",
                facts={"query": query},
            )
        return ToolOutcome(
            tool="read_code",
            ok=True,
            summary="\n".join(lines_out),
            facts={"query": query, "files": matched_files},
        )

    @staticmethod
    def _allowed(path: Path) -> bool:
        try:
            relative = path.relative_to(PROJECT_ROOT)
        except ValueError:
            return False
        if relative.parts and relative.parts[0] in CODE_ROOTS:
            return path.suffix in CODE_EXTS
        return relative.as_posix() == "README.md"

    def _tenants(self, args: dict[str, Any]) -> ToolOutcome:
        rows = [
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name or "未提供",
                "code": tenant.code,
                "doris_db": tenant.doris_db,
                "mysql_db": tenant.mysql_db,
            }
            for tenant in TENANT_CATALOG
        ]
        return ToolOutcome(
            tool="tenants",
            ok=True,
            summary=_rows_to_text(rows, limit=30),
            facts={"tenants": rows},
            rows=rows,
        )

    async def _metric(self, args: dict[str, Any]) -> ToolOutcome:
        name = str(args.get("metric") or "").strip()
        metric = METRICS.get(name)
        if metric is None and name not in {
            "space_energy",
            "floor_energy",
            "device_readings",
            "running_devices",
            "cold_station_scop",
            "cold_station_devices",
        }:
            raise ToolboxError(
                f"未知指标 {name!r}；可用："
                "energy_total / energy_trend / energy_ranking / control_success / "
                "alarm_stats / room_temperature / device_count / meter_count / "
                "cold_station_config / data_quality / space_energy / "
                "floor_energy / device_readings / running_devices / cold_station_scop / cold_station_devices"
            )
        metric_name = metric.name if metric else name
        start = _parse_day(args.get("start"), self.today)
        end = _parse_day(args.get("end"), start)
        if end < start:
            start, end = end, start
        keyword = str(args.get("keyword") or "").strip()

        doris = self.sources.get("doris")
        mysql = self.sources.get("mysql")

        if name == "energy_total" and doris:
            result = await doris.provider.energy_total(start, end)
            text = (
                f"{start}~{end} 总用电 {result.get('value')} kWh，"
                f"{result.get('active_meters')} 块表有数据"
            )
        elif name == "energy_trend" and doris:
            rows = await doris.provider.energy_trend(start, end)
            values = [float(row.get("energy_value") or 0) for row in rows]
            result = {
                "rows": rows[-31:],
                "days": len(rows),
                "total": round(sum(values), 2),
                "average": round(sum(values) / len(values), 2) if values else 0,
                "max": max(values) if values else 0,
                "min": min(values) if values else 0,
            }
            text = (
                f"{start}~{end} 共 {result['days']} 天，总用电 {result['total']} kWh，"
                f"日均 {result['average']} kWh\n"
                + _rows_to_text(rows[-31:], limit=31)
            )
        elif name == "energy_ranking" and doris:
            rows = await doris.provider.meter_ranking(start, end, 10)
            result = {"rows": rows}
            text = f"按表计 Top10（{start}~{end}）：\n" + _rows_to_text(rows, limit=10)
        elif name == "control_success" and doris:
            result = await doris.provider.control_stats(start, end)
            text = (
                f"{start}~{end} 控制任务 {result.get('total')} 条，"
                f"成功 {result.get('success')}，失败 {result.get('failed')}，"
                f"成功率 {result.get('success_rate')}%"
            )
        elif name == "alarm_stats" and doris:
            result = await doris.provider.alarm_stats(start, end)
            if result.get("available"):
                text = (
                    f"{start}~{end} 告警 {result.get('count')} 条；"
                    "级别/状态分布：\n" + _rows_to_text(result.get("by_level_status") or [], 20)
                )
            else:
                text = f"告警表在 {start}~{end} 没有数据（表示未接入，不等于 0 条告警）"
        elif name in ("room_temp", "room_temperature") and doris:
            result = await doris.provider.room_temp(keyword)
            if result.get("available"):
                text = _rows_to_text(result.get("devices") or [], 20)
            else:
                text = f"没有查到室温数据：{result.get('reason')}"
        elif name == "data_quality" and doris and hasattr(doris.provider, "hourly_completeness"):
            result = await doris.provider.hourly_completeness(start, end)
            text = _rows_to_text(result.get("rows") or [], 24) if result.get("available") else "小时表不可用"
        elif name == "device_count" and mysql:
            count = await mysql.provider.count_devices()
            result = {"device_count": count}
            text = f"设备台账（deleted=0）{count} 台"
        elif name == "meter_count" and mysql:
            count = await mysql.provider.count_meters()
            result = {"meter_count": count}
            text = f"表计档案（deleted=0）{count} 块"
        elif name == "cold_station_config" and mysql:
            rows = await mysql.provider.cooling_stations()
            result = {"rows": rows, "count": len(rows)}
            text = f"冷站配置 {len(rows)} 条：\n" + _rows_to_text(rows, limit=20)
        elif name == "cold_station_devices" and mysql:
            sql = (
                "SELECT c.application_id, b.cooling_name, "
                "d.device_category_name AS category, COUNT(*) AS cnt "
                f"FROM {mysql.database}.eo_energy_saving_cooling_device_configs c "
                f"LEFT JOIN {mysql.database}.eo_energy_saving_cooling_base_configs b "
                "ON b.application_id = c.application_id "
                "AND b.tenant_id = c.tenant_id AND b.deleted = 0 "
                f"LEFT JOIN {mysql.database}.eo_devices d ON d.id = c.device_id "
                f"WHERE c.tenant_id = {int(mysql.tenant_id)} "
                f"AND c.project_id = {int(mysql.project_id)} AND c.deleted = 0 "
                "GROUP BY c.application_id, b.cooling_name, d.device_category_name "
                "ORDER BY c.application_id, cnt DESC"
            )
            rows = await mysql.provider.ad_hoc_query(sql, limit=200)
            result = {"rows": rows, "count": len(rows)}
            text = (
                f"冷站设备分类（MySQL {mysql.database}，{len(rows)} 行）：\n"
                + _rows_to_text(rows, limit=50)
                if rows
                else "没有设备分类配置；冷站设备表可能为空。"
            )
        elif (
            name == "cold_station_scop"
            and doris
            and mysql
            and hasattr(mysql.provider, "cooling_stations")
        ):
            stations = await mysql.provider.cooling_stations()
            if not stations:
                result = {"rows": [], "note": "冷站配置为空"}
                text = "冷站配置为空：这家租户没有配置冷站，不能计算 SCOP。"
            else:
                cold_sql = (
                    f"SELECT meter_name, ROUND(SUM(energy_value),2) AS kwh "
                    f"FROM {doris.database}.dm_eo_e_meter_di "
                    f"WHERE tenant_id = {int(doris.tenant_id)} "
                    f"AND project_id = {int(doris.project_id)} "
                    f"AND energy_type = 'cold' "
                    f"AND data_time BETWEEN '{start}' AND '{end}' "
                    f"GROUP BY meter_name LIMIT 200"
                )
                elec_sql = (
                    f"SELECT meter_name, ROUND(SUM(energy_value),2) AS kwh "
                    f"FROM {doris.database}.dm_eo_e_meter_di "
                    f"WHERE tenant_id = {int(doris.tenant_id)} "
                    f"AND project_id = {int(doris.project_id)} "
                    f"AND energy_type = 'electric' "
                    f"AND data_time BETWEEN '{start}' AND '{end}' "
                    f"GROUP BY meter_name LIMIT 500"
                )
                cold_rows = await doris.provider.ad_hoc_query(cold_sql, limit=200)
                elec_rows = await doris.provider.ad_hoc_query(elec_sql, limit=500)
                rows, unassigned = self._scop_rows(stations, cold_rows, elec_rows)
                result = {
                    "rows": rows,
                    "unassigned_meters": unassigned,
                    "cold_meters": len(cold_rows),
                    "electric_meters": len(elec_rows),
                    "note": (
                        "冷量计和电表按名称归到冷站，是名称归集不是结构化映射；"
                        "状态=无用电数据/电量存疑的行不能当有效 SCOP。"
                    ),
                }
                text = (
                    f"{start}~{end} 冷站 SCOP（冷站配置 MySQL {mysql.database}；"
                    f"冷量/电量 Doris {doris.database}，按名称归集）：\n"
                    + _rows_to_text(rows, limit=20)
                    + "\n归集说明：" + str(result["note"])
                )
                if unassigned:
                    text += (
                        f"\n未归属到任何冷站的电表 {len(unassigned)} 块："
                        + "、".join(str(item["meter_name"]) for item in unassigned[:10])
                    )
        elif (
            name == "space_energy"
            and doris
            and hasattr(doris.provider, "energy_by_space")
        ):
            result = await doris.provider.energy_by_space(start, end)
            spaces = result.get("spaces") or []
            if spaces:
                text = (
                    f"{start}~{end} 按空间聚合：{result.get('space_count')} 个空间，"
                    f"合计 {result.get('total')} kWh\n"
                    + _rows_to_text(spaces, limit=20)
                    + "\n注意：" + str(result.get("note") or "")
                )
            else:
                text = "按空间聚合没有结果；这张表可能没有写 space_name/space_code。"
        elif (
            name == "floor_energy"
            and doris
            and hasattr(doris.provider, "energy_by_floor")
        ):
            result = await doris.provider.energy_by_floor(start, end)
            floors = result.get("floors") or []
            if floors:
                text = (
                    f"{start}~{end} 按表计名解析楼层：{result.get('floor_count')} 层，"
                    f"覆盖 {result.get('coverage_pct')}% 电量"
                    f"（可解析 {result.get('covered')} kWh / 合计 {result.get('total')} kWh，"
                    f"未识别 {result.get('unmatched')} kWh）\n"
                    + _rows_to_text(floors, limit=20)
                    + "\n注意：" + str(result.get("note") or "")
                )
            else:
                text = (
                    "按表计名解析不到楼层；"
                    f"合计 {result.get('total')} kWh，说明表计名称里没有楼层信息。"
                )
        elif (
            name == "running_devices"
            and doris
            and hasattr(doris.provider, "running_devices")
        ):
            hours = int(args.get("hours") or 168)
            ratio = float(args.get("min_ratio") or 0.8)
            result = await doris.provider.running_devices(hours, ratio)
            rows = result.get("rows") or []
            if rows:
                text = (
                    f"最近 {result.get('window_hours')} 小时，"
                    f"{result.get('checked_devices')} 台设备中 "
                    f"{result.get('matched_devices')} 台持续运行"
                    f"（活跃占比 ≥ {result.get('min_ratio')}）：\n"
                    + _rows_to_text(rows, limit=20)
                    + "\n说明：" + str(result.get("note") or "")
                )
            else:
                text = (
                    f"最近 {result.get('window_hours')} 小时没有满足持续运行条件的设备"
                    f"（检查了 {result.get('checked_devices')} 台）。"
                )
        elif (
            name == "device_readings"
            and doris
            and hasattr(doris.provider, "device_readings")
        ):
            raw_codes = args.get("point_codes") or ""
            if isinstance(raw_codes, str):
                codes = tuple(
                    part
                    for part in re.split(r"[,，\s]+", raw_codes)
                    if part.strip()
                )
            elif isinstance(raw_codes, (list, tuple)):
                codes = tuple(str(part) for part in raw_codes)
            else:
                codes = ()
            hours = int(args.get("hours") or 24)
            result = await doris.provider.device_readings(keyword, codes, hours)
            rows = result.get("rows") or []
            if rows:
                text = (
                    f"最近 {result.get('window_hours')} 小时，"
                    f"匹配“{keyword or '全部'}”的 {len(rows)} 组设备/点位：\n"
                    + _rows_to_text(rows, limit=30)
                    + "\n说明：" + str(result.get("note") or "")
                )
            else:
                text = (
                    f"没有查到设备点位：{result.get('reason') or '匹配不到设备或点位'}"
                )
        else:
            raise ToolboxError(
                f"指标 {name} 需要的数据源没有启用；可改用 run_sql 查询，"
                "或先确认当前租户的 Doris/MySQL 是否可用。"
            )

        return ToolOutcome(
            tool="metric",
            ok=True,
            summary=text,
            facts={
                "metric": name,
                "metric_name": metric_name,
                "time_range": {"start": start.isoformat(), "end": end.isoformat()},
                "result": result,
            },
            rows=(result.get("rows") if isinstance(result, dict) else []) or [],
        )
