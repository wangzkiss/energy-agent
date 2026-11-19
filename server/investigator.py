"""只读数据调查 Agent（多工具版）。

与固定模板的 EnergyAgent 分工：
- 明显能命中固定口径的问题仍走 providers.py 的固定模板，快且稳定；
- 其余问题交给这里：模型先分析问题，自己决定
  “查表结构 / 查口径知识 / 读代码 / 跑固定指标 / 跑只读 SQL”，
  观察结果后继续推理，最后给出结论。

设计参考了 deepseek-harness 的 agent loop：
- 一次 turn 内按 step 循环：模型请求 → 工具调用 → 观察 → 下一步；
- 工具统一经过 toolbox 和 sqlguard，模型不能绕过租户/库/时间/限行约束；
- 最多执行有限步，达到上限后强制基于已有观察收口；
- 不把完整数据库结果塞回模型，只回传压缩后的行和错误，避免上下文膨胀。

安全边界（与 sqlguard 一致）：
- 只允许 SELECT / WITH / SHOW / DESC；
- 单语句、禁止注释、禁止系统库、禁止写操作；
- 只允许访问当前租户的当前库；禁止引用其它租户库；
- 有 tenant_id 的数据源必须带 tenant_id = 当前租户；
- 事实表必须带时间过滤；自动限行；单次调查最多若干步。

兼容：模块继续导出 QuerySource / SQLGuardError / validate_readonly_sql /
MYSQL_FACT_TABLES，老调用方和单测不需要改。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from config import Settings
from knowledge import MYSQL_TABLES, TABLES
from llm import LLMClient
from sqlguard import (
    DEFAULT_TIME_COLUMNS,
    DORIS_FACT_TABLES,
    MYSQL_FACT_TABLES,
    ROW_LIMIT,
    SQLGuardError,
    QuerySource,
    forbidden_databases,
    validate_readonly_sql,
)
from caches import SCHEMA_CACHE, SQL_CACHE
from toolbox import AgentToolbox, ProgressCallback

logger = logging.getLogger("energy-investigator")

MAX_STEPS = 12
# 常规问题 8 步内收口；只有 search_columns/table_profile 这类发现工具用过，
# 才允许继续到 MAX_STEPS，避免“简单问题也跑满 12 步”。
SOFT_STEPS = 8
DISCOVERY_TOOLS = {"search_columns", "table_profile"}
QUERY_TIMEOUT_SECONDS = 60
OBSERVATION_BUDGET = 16000

__all__ = [
    "DataInvestigator",
    "Investigation",
    "MYSQL_FACT_TABLES",
    "QuerySource",
    "ROW_LIMIT",
    "SQLGuardError",
    "validate_readonly_sql",
]


@dataclass
class Investigation:
    answer: str
    facts: dict[str, Any] = field(default_factory=dict)
    sql_log: list[dict[str, Any]] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)


def _schema_text(database: str) -> str:
    lines: list[str] = []
    for name, meta in TABLES.items():
        fields = ", ".join((meta.get("key_fields") or {}).keys())
        purpose = str(meta.get("purpose") or "")[:120]
        lines.append(f"- {database}.{name}: {fields}；{purpose}")
    return "\n".join(lines)


def _mysql_schema_text(database: str) -> str:
    lines = [
        f"MySQL 业务库：{database}（每个租户一个库；完整表清单用 find_tables 或 SHOW TABLES 查）。",
        "关键表：",
    ]
    for name, meta in MYSQL_TABLES.items():
        fields = ", ".join((meta.get("key_fields") or {}).keys())
        purpose = str(meta.get("purpose") or "")[:160]
        lines.append(f"- {database}.{name}: {fields}；{purpose}")
    lines.append(
        "- 其它表不要猜字段：先 find_tables 找表，再 describe_table 看结构；"
        "Archery 对 DESC 支持不稳定，工具内部用的是 SHOW FULL COLUMNS。"
    )
    return "\n".join(lines)


def _parse_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("\x60\x60\x60"):
        raw = re.sub(r"^\x60\x60\x60(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*\x60\x60\x60$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(raw[start : end + 1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _compact_json(value: Any, limit: int = 600) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _markdown_table(rows: list[dict[str, Any]], limit: int = 10) -> list[str]:
    """把查询结果渲染成 Markdown 表格，用于模型总结失败时的兜底。"""
    clean = [row for row in rows if isinstance(row, dict)]
    if not clean:
        return ["（没有可展示的行）"]
    columns = list(clean[0].keys())
    lines = [
        "| " + " | ".join(str(column) for column in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in clean[:limit]:
        values = [
            str(row.get(column, "")).replace("|", "\\|").replace("\n", " ")
            for column in columns
        ]
        lines.append("| " + " | ".join(values) + " |")
    if len(clean) > limit:
        lines.append("")
        lines.append(f"（共 {len(clean)} 行，只展示前 {limit} 行）")
    return lines


class DataInvestigator:
    """用模型做“分析问题 → 选工具 → 观察 → 修正 → 结论”的多步只读调查。"""

    FACT_TABLES = DORIS_FACT_TABLES

    def __init__(
        self,
        llm: LLMClient,
        config: Settings,
        today: date | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.llm = llm
        self.config = config
        self.today = today or date.today()
        self.max_steps = max(3, min(int(max_steps or MAX_STEPS), 12))
        self.soft_steps = min(SOFT_STEPS, self.max_steps)

    def doris_source(self, provider: Any) -> QuerySource:
        cfg = getattr(provider, "config", self.config)
        database = str(getattr(cfg, "doris_db", ""))
        tenant_id = int(getattr(cfg, "doris_tenant_id", 0))
        project_id = int(getattr(cfg, "doris_project_id", 1))
        return QuerySource(
            key="doris",
            label="Doris 分析库",
            database=database,
            tenant_id=tenant_id,
            project_id=project_id,
            provider=provider,
            schema_text=_schema_text(database),
            fact_tables={table: DEFAULT_TIME_COLUMNS for table in self.FACT_TABLES},
            require_tenant=True,
            forbidden_databases=forbidden_databases(database),
        )

    def mysql_source(self, provider: Any) -> QuerySource:
        database = str(getattr(provider, "database", self.config.mysql_db))
        tenant_id = int(getattr(provider, "tenant_id", self.config.doris_tenant_id))
        project_id = int(getattr(self.config, "doris_project_id", 1))
        return QuerySource(
            key="mysql",
            label="MySQL 业务库",
            database=database,
            tenant_id=tenant_id,
            project_id=project_id,
            provider=provider,
            schema_text=_mysql_schema_text(database),
            fact_tables=MYSQL_FACT_TABLES,
            require_tenant=False,
            forbidden_databases=forbidden_databases(database),
        )

    async def investigate(
        self,
        question: str,
        provider: Any,
        tenant_facts: dict[str, Any] | None = None,
        context_facts: dict[str, Any] | None = None,
        progress: ProgressCallback | None = None,
    ) -> Investigation | None:
        if not self.llm.configured or getattr(provider, "mode", "") != "archery":
            return None
        source = self.doris_source(provider)
        if not re.fullmatch(r"[A-Za-z0-9_]+", source.database):
            return None
        return await self.investigate_sources(
            question,
            [source],
            default_source=source.key,
            tenant_facts=tenant_facts,
            context_facts=context_facts,
            progress=progress,
        )

    async def investigate_sources(
        self,
        question: str,
        sources: list[QuerySource],
        *,
        default_source: str | None = None,
        tenant_facts: dict[str, Any] | None = None,
        context_facts: dict[str, Any] | None = None,
        progress: ProgressCallback | None = None,
        seed_facts: dict[str, Any] | None = None,
    ) -> Investigation | None:
        """多数据源多步调查；所有工具调用都经过 toolbox 的只读校验。"""
        if not self.llm.configured or not sources:
            return None
        source_map = {source.key: source for source in sources}
        default_key = default_source or sources[0].key
        if default_key not in source_map:
            default_key = sources[0].key

        toolbox = AgentToolbox(
            sources=sources,
            today=self.today,
            row_limit=ROW_LIMIT,
            progress=progress,
            schema_cache=SCHEMA_CACHE,
            sql_cache=SQL_CACHE,
        )
        system = self._system_prompt(sources, default_key, toolbox)
        observations: list[dict[str, Any]] = []
        seen_calls: dict[str, int] = {}
        parse_failures = 0

        if seed_facts:
            observations.append(
                {
                    "step": 0,
                    "tool": "seed",
                    "args": {},
                    "ok": True,
                    "summary": "固定模板已经算出的结果（可直接使用，但不足以回答时继续查）：\n"
                    + _compact_json(seed_facts, 2500),
                }
            )

        extended = False
        for step in range(1, self.max_steps + 1):
            if step > self.soft_steps and not extended:
                used_discovery = any(
                    observation.get("tool") in DISCOVERY_TOOLS
                    for observation in observations
                )
                if not used_discovery:
                    break
                extended = True
            user = self._user_prompt(
                question,
                sources=sources,
                default_source=default_key,
                context_facts=context_facts,
                observations=observations,
                step=step,
            )
            try:
                raw = await self.llm.complete(
                    system,
                    user,
                    max_tokens=4000,
                    temperature=0,
                )
            except Exception as exc:  # noqa: BLE001 - 调查失败回退固定模板
                logger.warning("investigator model failed: %s", exc)
                fallback = self._observations_answer(observations)
                return self._finish(fallback, sources, observations, toolbox) if fallback else None

            payload = _parse_json(raw)
            if payload is None:
                parse_failures += 1
                observations.append(
                    {
                        "step": step,
                        "tool": "protocol",
                        "ok": False,
                        "error": "上一条输出不是合法 JSON；只允许输出规定的 JSON。",
                    }
                )
                if parse_failures >= 2:
                    break
                continue

            action = str(payload.get("action") or "").strip().lower()
            if action in {"final", "answer"} or (not action and payload.get("answer")):
                answer = str(payload.get("answer") or "").strip()
                if answer:
                    return self._finish(answer, sources, observations, toolbox)
                fallback = self._observations_answer(observations)
                return self._finish(fallback, sources, observations, toolbox) if fallback else None

            tool_name, tool_args = self._read_call(payload)
            if not tool_name:
                observations.append(
                    {
                        "step": step,
                        "tool": "protocol",
                        "ok": False,
                        "error": (
                            "缺少工具名。合法格式："
                            '{"action":"tool","tool":"run_sql","args":{...}} '
                            '或 {"action":"final","answer":"..."}'
                        ),
                    }
                )
                continue

            signature = tool_name + ":" + _compact_json(tool_args, 4000)
            seen_calls[signature] = seen_calls.get(signature, 0) + 1
            if seen_calls[signature] > 1:
                observations.append(
                    {
                        "step": step,
                        "tool": tool_name,
                        "args": tool_args,
                        "ok": False,
                        "error": "重复调用被跳过：参数完全相同。请换一种查法或直接给结论。",
                    }
                )
                continue

            outcome = await toolbox.execute(tool_name, tool_args)
            observations.append(
                {
                    "step": step,
                    "tool": tool_name,
                    "args": tool_args,
                    "ok": outcome.ok,
                    "summary": outcome.summary,
                    "error": outcome.error,
                    "facts": outcome.facts,
                    "rows": outcome.rows,
                }
            )

        # 达到步数上限后，要求模型基于已有观察直接给结论，不再执行工具。
        final_user = self._user_prompt(
            question,
            sources=sources,
            default_source=default_key,
            context_facts=context_facts,
            observations=observations,
            step=min(step, self.max_steps) + 1,
            force_final=True,
        )
        try:
            raw = await self.llm.complete(
                system,
                final_user,
                max_tokens=4000,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("investigator final failed: %s", exc)
            fallback = self._observations_answer(observations)
            return self._finish(fallback, sources, observations, toolbox) if fallback else None
        payload = _parse_json(raw)
        answer = str((payload or {}).get("answer") or "").strip()
        if not answer:
            # 收口时模型偶尔直接给 Markdown 而不是 JSON；只接受“像结论”的文本。
            plain = (raw or "").strip()
            if plain and not plain.startswith("{") and (
                "###" in plain or "结论" in plain or len(plain) >= 60
            ):
                answer = plain
        if not answer:
            # 再给一次机会：不要求 JSON，只要一段结论。比直接返回原始行更有用。
            try:
                answer = await self._final_prose(question, observations, context_facts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("investigator prose final failed: %s", exc)
                answer = ""
        if not answer:
            fallback = self._observations_answer(observations)
            return self._finish(fallback, sources, observations, toolbox) if fallback else None
        return self._finish(answer, sources, observations, toolbox)

    @staticmethod
    def _read_call(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """兼容新协议 {action, tool, args} 与旧协议 {action: run_sql, source, sql}。"""
        tool = str(payload.get("tool") or "").strip().lower()
        args = payload.get("args")
        if not isinstance(args, dict):
            args = payload.get("arguments")
        if isinstance(args, dict):
            args = dict(args)
        else:
            args = {}

        legacy_action = str(payload.get("action") or "").strip().lower()
        if not tool and legacy_action in {"run_sql", "sql"}:
            tool = "run_sql"
        if tool in {"run_sql", "sql"}:
            tool = "run_sql"
            args.setdefault("sql", payload.get("sql") or payload.get("query"))
        if not tool and payload.get("sql"):
            tool = "run_sql"
            args.setdefault("sql", payload.get("sql"))
        if "source" in payload:
            args.setdefault("source", payload.get("source"))
        return tool, {key: value for key, value in args.items() if value not in (None, "")}

    def _observations_answer(self, observations: list[dict[str, Any]]) -> str | None:
        """模型最终总结失败时，用已执行的只读结果拼一个可读的兜底回答。"""
        successful = [
            item
            for item in observations
            if item.get("ok") and (item.get("rows") or item.get("summary"))
        ]
        if not successful:
            if not observations:
                return None
            lines = [
                "### 结论",
                "",
                "这次调查没有拿到可用的数据结果；下面是执行记录，不做数字推测。",
                "",
                "### 执行记录",
                "",
            ]
            for item in observations[-4:]:
                lines.append(f"- [{item.get('tool')}] {item.get('error') or '未返回结果'}")
            return "\n".join(lines)

        lines = [
            "### 结论",
            "",
            "模型没有在步数上限内完成收口。下面是已经查到的只读结果，未做推测；"
            "如果要更完整的结论，可以把问题限定到具体对象或更短的时间段再问一次。",
            "",
            "### 查询结果",
            "",
        ]
        for item in successful[-3:]:
            tool = item.get("tool") or "?"
            args = item.get("args") or {}
            detail = (
                args.get("sql")
                or args.get("metric")
                or args.get("table")
                or args.get("query")
                or args.get("keyword")
                or ""
            )
            detail = " ".join(str(detail).split())
            lines.append(f"- 工具 {tool}：" + (detail[:200] if detail else "无参数"))
            rows = item.get("rows") or []
            lines.append("")
            if rows:
                lines.extend(_markdown_table(rows, 10))
            else:
                summary = str(item.get("summary") or "").strip()
                if len(summary) > 1200:
                    summary = summary[:1200] + "…"
                lines.append(summary)
            lines.append("")
        return "\n".join(lines)

    async def _final_prose(
        self,
        question: str,
        observations: list[dict[str, Any]],
        context_facts: dict[str, Any] | None,
    ) -> str:
        """强制收口失败时的最后一次机会：不要求 JSON，只要一段结论。"""
        system = (
            "你是能效数据分析助手。根据观察中已经执行的只读查询结果写最终结论。\n"
            "只能使用观察里的数字、表名和结论；不要编造，不要写 SQL，不要提“观察/工具”。\n"
            "如果证据不足以回答，明确说明缺什么数据或口径，不要用其它指标替代。\n"
            "用中文 Markdown，只写四节：### 结论 / ### 证据 / ### 口径 / "
            "### 未能回答的部分（没有就省略）。"
        )
        user = (
            f"用户问题：{question}\n\n"
            f"<观察>\n{self._render_observations(observations)}\n</观察>\n\n"
            "请直接输出最终结论。"
        )
        text = await self.llm.complete(system, user, max_tokens=4000, temperature=0.1)
        text = (text or "").strip()
        # 只接受“像结论”的文本；半截 JSON 或闲聊式回答继续走结构化兜底。
        if text.startswith("{") or not (
            "###" in text or "结论" in text or len(text) >= 40
        ):
            return ""
        return text

    def _system_prompt(
        self,
        sources: list[QuerySource],
        default_source: str,
        toolbox: AgentToolbox,
    ) -> str:
        blocks: list[str] = []
        for source in sources:
            tenant_rule = (
                f"必须带 tenant_id = {source.tenant_id}"
                if source.require_tenant
                else "表中有 tenant_id 时也应过滤；没有 tenant_id 的表按租户独立库隔离"
            )
            blocks.append(
                f"### 数据源 {source.key}：{source.label}\n"
                f"数据库：{source.database}；租户 tenant_id={source.tenant_id}；"
                f"project_id={source.project_id}\n"
                f"租户过滤：{tenant_rule}\n"
                f"{source.schema_text}"
            )
        return f"""你是能效数据分析 Agent。你的任务不是背模板，而是像分析师一样：
先拆解问题，再自己选择工具取证，最后给出有证据的结论。

当前日期：{self.today.isoformat()}
默认数据源：{default_source}

{chr(10).join(blocks)}

{toolbox.catalog_text()}

工作方法（必须遵守）：
0. 发现类问题按“调查协议”走，不要一上来就下结论：
   a) 术语展开：把业务词拆成数据对象（SCOP = 冷量 ÷ 冷站电耗；需量 = 功率峰值）；
   b) 找落点：search_columns 搜字段名/注释，确认指标落在哪张表；
   c) 判可用：table_profile 看行数/时间范围，0 行 = 未接入，必须换路径；
   d) 验映射：站/设备/表计怎么关联（application_id、site_id、名称），写清归集假设；
   e) 取数：优先 metric，其次 run_sql；
   f) 交叉校验：汇总值是否等于分项之和？有没有 0 值/异常值？
   g) 结论分层：可用 / 不可用 / 仅汇总口径，并给覆盖率或数据质量标记。
1. 先在心里拆解问题：要回答的指标/对象/时间范围/需要的维度。
   - 问“哪个楼层/哪类设备/哪个系统”就必须按该维度聚合或分组；
   - 问“对比”必须分别查两个对象再比较；
   - 问“为什么/原因”要先给现象数据，再列可能的证据，不能把猜测写成结论。
2. 优先用 metric 工具拿已注册口径的数字；需要维度、明细、分组、对比时用 run_sql。
   - 冷站 SCOP/系统能效比：用 metric=cold_station_scop（返回每个冷站的冷量、
  用电、SCOP 和数据质量标记；冷量计少或电表为 0 时必须如实说明）。
   默认按最近一个完整日（昨天）算；用户明确说区间才用区间。
   拿到结果后最多再做 1 步交叉校验，不要反复查同一口径。
   - 冷站设备类型/分类：用 metric=cold_station_devices（按冷站统计冷机/水泵/冷却塔数量）。
   - 按楼层排名或对比：优先用 metric=floor_energy（从 meter_name 解析楼层，
     并返回覆盖率/未识别电量）；再用 space_energy 看结构化空间覆盖。
     覆盖不足时必须写明“按表计名解析、覆盖率 X%，不是结构化楼层”，
     不能拿表计排名冒充楼层排名，也不要把 A1座 总量当某层。
   - 室温/压力/开关机等设备点位：用 metric=device_readings。
     点位表 dwi_eo_d_point_data 没有 tenant_id，直接 run_sql 会被租户校验拒绝。
     温度/压力这类物理量用 avg_positive（已剔除 0 值），并写明 zero_samples 有多少条，
     不要把 0 值混进均值。
   - 问“哪些设备一直在运行/持续运行”：用 metric=running_devices（默认近 168 小时、
     活跃占比 ≥ 0.8），结论里必须写明判据、窗口和“统计口径不等于实际制冷”。
3. 表名或字段不确定时，先 search_columns（按字段名/注释在全库检索），
   再 find_tables / describe_table，不要猜字段；确认表后先用 table_profile
   看行数和时间范围，再决定查不查。
   本地知识库只是精选表；find_tables 没结果不代表库里没有，必须用 search_columns
   搜业务词（如 SCOP、冷量、需量、电价）的字段名或注释。
4. 口径不确定（同名字段、重复行、快照表、单位）时，用 knowledge 查口径，必要时用 read_code 读实现。
5. 每次工具调用只解决一个具体问题；看到结果后判断还缺什么，不要连续盲查。
   已经能回答时就收口：拿到关键数字后最多再补 1 步校验，同一事实不要查两遍。
6. 工具失败时阅读错误并修正（换表、补时间过滤、补 tenant_id、减少字段），不要重复同一调用。
7. 常规问题 {self.soft_steps} 步内收口；只有用过 search_columns/table_profile
   的发现类问题才允许用到 {self.max_steps} 步。证据足够就 final，不要为了多查而多查。
8. 查不到就明确说“当前数据无法回答”，并说明缺什么（没有表/没有数据/没有权限/口径未定义），
   不能用相近指标替代，也不能编造数字。
9. 通常 2-4 步就能回答；不要为了凑步骤把整张表拉回来看，也不要重复查同一个总数。

已知数据坑（先验证再用，验证方式写进结论）：
- dm_eo_e_meter_di 的 space_code/space_name 只有少量空间值（A1座 / 35-37 层）；
  “楼层/房间”不一定能直接聚合，楼层信息更多体现在 meter_name 前缀。
  按楼层排名前必须先统计 space_name 的分组覆盖，不能把表计排名当成楼层排名。
- 问“楼层排名”或对比“A1座 vs 某个楼层”：
  1) 先 metric=space_energy；如果只有 A1座 一个分组，说明结构化空间维度没有楼层；
  2) 需要楼层明细时，用 metric=floor_energy（它已兼容 “19层xx” 和 “A2栋36-8” 两种命名）；
     自己写 SQL 时用 COALESCE(regexp_extract(meter_name,'([0-9]+)[层楼]',1),
     regexp_extract(meter_name,'栋([0-9]+)-',1))，并写明覆盖率；
  3) 不要把 A1座 总量当成某个楼层，也不要反复换表（dm_eo_s_space_di 同样没有楼层行）。
- dm_eo_s_space_di 同一天同一空间可能有多行重复，直接 SUM 会放大，必须先去重。
- dwr_eo_energy_meter_wide / dwi_eo_energy_usage 是当日快照，不能当历史趋势。
- dim_eo_alarms 名字是 dim，实际是告警事实表；空表表示未接入，不等于 0 条告警。
- dim_eo_d_device.device_status 不可靠，不能用来判断设备在线/离线。
- 判断设备是否运行，优先看 dwi_eo_d_point_data 的 FanStartSwitchSp / ModeSp /
  ActivePowerSum 是否大于 0，并在结论里写明用了哪个判据。
- MySQL eo_devices 与 Doris dim_eo_d_device 是两套台账，可以交叉核对数量。
- dwi_eo_d_point_data 没有 tenant_id/project_id，直接 run_sql 会被校验拒绝；
  设备点位请用 metric=device_readings（服务端已按租户筛 device_id 再关联）。
- 冷站 SCOP 不在冷站配置表里：先 search_columns 'SCOP 能效'，再查
  algo_overview_kpi.system_scop / dm_eo_e_meter_di 的 energy_type='cold'；
  某张表 0 行只能说明该表未接入，不能推断“没有冷量计”。

final 的回答格式（Markdown）：
### 结论
1-3 句，先给关键数字和直接答案。
### 证据
关键数字或表格（最多 10 行），每个数字要能对应到查询结果。
### 口径
时间范围、数据源库表、过滤条件、单位。
### 未能回答的部分
只有在确实缺数据/缺口径时写；没有就省略这一节。

只允许输出以下两种 JSON 之一，不要输出其它文字：
{{"action":"tool","tool":"metric|run_sql|find_tables|search_columns|describe_table|table_profile|knowledge|read_code|tenants","args":{{...}}}}
{{"action":"final","answer":"Markdown 格式的最终结论"}}

SQL 规则（会被程序强制校验，违反会直接失败）：
- 只允许 SELECT / WITH / SHOW / DESC，单语句，禁止注释和任何写操作。
- 不要使用 OR / UNION（会被直接拒绝）；条件之间用 AND，多值匹配用 IN (...) 或 RLIKE 'a|b'。
- SELECT 必须使用当前数据源数据库的全限定表名，例如 {sources[0].database}.table_name。
- 有 tenant_id 列的数据源必须带 tenant_id = 当前租户；MySQL 的 eo_ticket 没有 tenant_id，不要因此拒绝。
- 事实表必须带时间过滤（Doris: data_time/create_time/alarm_time/reading_time；MySQL: alarm_time/reading_time/create_time/exec_time/created_at 等）。
- 查询最多返回 {ROW_LIMIT} 行，必须显式写 LIMIT {ROW_LIMIT} 或更小。
- 不要访问 information_schema/mysql/sys 等系统库；不要引用其它租户的库。
- MySQL 很多表有 deleted 字段，业务明细优先加 deleted = 0；以字段注释为准。
- Doris 的 dm_eo_s_space_di 同一天同一空间可能有多行重复，不能直接 SUM；
  dwr_eo_energy_meter_wide / dwi_eo_energy_usage 是当日快照，不能当历史趋势。
- 设备点位 dwi_eo_d_point_data 没有 tenant_id/project_id，必须先用 dim_eo_d_device 按租户筛出 device_id。"""

    def _user_prompt(
        self,
        question: str,
        *,
        sources: list[QuerySource],
        default_source: str,
        context_facts: dict[str, Any] | None,
        observations: list[dict[str, Any]],
        step: int,
        force_final: bool = False,
    ) -> str:
        context = json.dumps(context_facts or {}, ensure_ascii=False)[:3000]
        observation_text = self._render_observations(observations)
        final_rule = (
            "已经达到工具调用步数上限，请不要再调用工具，直接基于已有观察输出 final。"
            if force_final
            else "判断下一步：调用哪个工具，或直接 final。"
        )
        source_lines = "\n".join(
            f"- {source.key}: {source.label} / {source.database}" for source in sources
        )
        return f"""用户问题：
{question}

可用数据源：
{source_lines}
默认数据源：{default_source}
同一会话上下文（可能为空；可参考上一轮结论继续分析，但不能复用上一轮数字）：
{context}

已经执行的观察（按时间顺序；tool/args/结果/错误）：
{observation_text or "（还没有执行任何工具）"}

当前是第 {step} 步。{final_rule}
只输出 JSON。"""

    @staticmethod
    def _render_observations(observations: list[dict[str, Any]]) -> str:
        if not observations:
            return ""
        rendered: list[str] = []
        for item in observations:
            header = f"#{item.get('step', '?')} [{item.get('tool')}]"
            args = item.get("args") or {}
            if args:
                header += " " + _compact_json(args, 500)
            if item.get("ok") is False:
                rendered.append(header + "\n错误：" + str(item.get("error") or "未知错误"))
            else:
                summary = str(item.get("summary") or "")
                if len(summary) > 3500:
                    summary = summary[:3500] + "\n…（结果已截断）"
                rendered.append(header + "\n" + summary)
        text = "\n\n".join(rendered)
        if len(text) > OBSERVATION_BUDGET:
            text = "（更早的观察已省略）\n\n" + text[-OBSERVATION_BUDGET:]
        return text

    def _finish(
        self,
        answer: str,
        sources: list[QuerySource],
        observations: list[dict[str, Any]],
        toolbox: AgentToolbox,
    ) -> Investigation:
        answer = (answer or "").strip()
        if len(answer) > 8000:
            answer = answer[:8000].rstrip() + "…"

        records: list[dict[str, Any]] = []
        for item in observations:
            if item.get("tool") in {"protocol", "seed"}:
                continue
            tool = item.get("tool") or "?"
            args = item.get("args") or {}
            facts = item.get("facts") or {}
            raw_sql = str(facts.get("sql") or args.get("sql") or "").strip()
            detail = (
                raw_sql
                or args.get("metric")
                or args.get("table")
                or args.get("query")
                or args.get("keyword")
                or ""
            )
            detail = " ".join(str(detail).split())
            records.append(
                {
                    "n": len(records) + 1,
                    "tool": tool,
                    "source": args.get("source") or facts.get("source") or "",
                    "database": facts.get("database") or "",
                    "ok": item.get("ok") is not False,
                    "error": str(item.get("error") or ""),
                    # detail 给非 SQL 工具展示；sql 保留完整语句，供前端逐条复制。
                    "detail": detail[:8000],
                    "sql": raw_sql[:8000],
                    "rows": facts.get("row_count"),
                    "cached": facts.get("cached") if isinstance(facts.get("cached"), bool) else None,
                }
            )
        if records:
            payload = json.dumps(
                {"records": records}, ensure_ascii=False, separators=(",", ":")
            )
            record_block = "\n\n```query-log\n" + payload + "\n```"
        else:
            record_block = "\n\n- 未调用工具（模型直接给出边界说明）。"

        source_summary = "；".join(
            f"{source.key}={source.database}（tenant_id={source.tenant_id}）"
            for source in sources
        )
        answer += (
            "\n\n### 查询记录\n\n"
            + record_block
            + "\n\n### 限制\n\n"
            + "- 调查由模型自行选择工具，所有 SQL 都经过只读、单语句、租户/库隔离、"
            "事实表时间过滤和限行校验；复杂口径仍需人工复核。\n"
            + f"- 本次可用数据源：{source_summary}。"
        )

        sql_log = [
            {
                "source": (item.get("args") or {}).get("source"),
                "database": (item.get("facts") or {}).get("database"),
                "sql": (item.get("facts") or {}).get("sql"),
                "row_count": (item.get("facts") or {}).get("row_count"),
                "error": item.get("error"),
            }
            for item in observations
            if (item.get("facts") or {}).get("sql")
        ]
        tool_log = [
            {
                "tool": item.get("tool"),
                "args": item.get("args"),
                "ok": item.get("ok"),
                "error": item.get("error"),
            }
            for item in observations
        ]
        return Investigation(
            answer=answer,
            facts={
                "source": "llm_tool_investigation",
                "sources": [
                    {
                        "key": source.key,
                        "label": source.label,
                        "database": source.database,
                        "tenant_id": source.tenant_id,
                    }
                    for source in sources
                ],
                "database": sources[0].database,
                "tenant_id": sources[0].tenant_id,
                "tool_calls": tool_log,
                "sql_log": sql_log,
                "model": self.llm.model,
            },
            sql_log=sql_log,
            tool_log=tool_log,
        )
