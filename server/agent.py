"""能效 Agent：规则意图 + 固定数据模板 + 开放数据调查 + 模型润色。

原则：
1. 已知指标不生成 SQL，只使用数据层给出的 facts；
2. 固定模板未覆盖的开放问题，允许模型生成只读 SQL，但必须先过
   investigator.validate_readonly_sql()；
3. 所有数字必须来自固定模板或实际只读查询，模型不编造；
4. 模型不可用时，使用确定性模板兜底，保证可演示。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from config import Settings, settings
from investigator import DataInvestigator
from concepts import concept_answer
from knowledge import knowledge_answer
from llm import LLMClient, LLMError
from providers import (
    EnergyProvider,
    MySQLArcheryProvider,
    _fmt,
    get_mysql_provider,
    get_tenant_provider,
)
from semantic import METRICS, QuestionPlan, plan_question
from tenants import Tenant, resolve_tenant, tenant_answer, tenant_rows

logger = logging.getLogger("energy-agent")

CAPABILITY_TEXT = """### 我能查什么

| 能力 | 数据来源 | 说明 |
|---|---|---|
| 期间总用电量 | `dm_eo_e_meter_di` | 日粒度，按租户/项目过滤，单位 kWh |
| 日用电趋势 | `dm_eo_e_meter_di` | 最多 400 天，默认近 7 天 |
| 表计用电排名 | `dm_eo_e_meter_di` | 按表计汇总，Top N |
| 设备室温/设定温度 | `dwi_eo_d_point_data` | 仅最近 2 小时有点位的设备 |
| 控制任务统计 | `eo_device_control_task` | 成功/失败/未知，按时间窗 |
| 告警统计 | `dim_eo_alarms` | 视租户是否接入，可能为空 |
| 小时表缺数/完整性 | `dm_eo_e_meter_hi` | 单日已结束整点覆盖，按小时统计行数/表计数 |
| 线上 MySQL 业务库 | MySQL `<租户code>` | 设备、表计、空间、控制任务、告警、工单、电价等，多步只读分析 |
| 冷站配置 | MySQL `eo_energy_saving_cooling_base_configs` | 冷站名称、cold_site_id、空间、日历、冷站类型 |
| 冷站用电 | MySQL 冷站/设备/电表映射 + Doris `dm_eo_e_meter_di` | 只统计冷站设备关联的正常电表；没有映射时明确说无法计算 |
| 用电异常检测 | Doris `dm_eo_e_meter_di` | 与前 7 天日均基线比较，判断偏高/偏低；只做统计异常，不编造原因 |
| 设备/表计数量 | MySQL `eo_devices` / `eo_energy_meters` | 按租户、项目、deleted=0 固定统计，不走模型 SQL |
| 开放数据调查 | Doris + MySQL | 固定模板未覆盖时，模型选择数据源并生成只读 SQL 多步排查 |
| 租户与数据库关系 | `eo_tenant_data_sources` | 租户名称/编码、MySQL 库、Doris 库 |
| 代码/库表知识 | `server/knowledge.py` | 指标对应哪张表、哪些字段、什么口径 |
| 同一会话追问 | 前端会话历史 | 可承接上一轮的指标、时间、租户、楼层等上下文 |

### 我查不了什么

- **同比/环比**：历史数据不足两年；
- **节能率/节能量**：没有基线，不能编造；
- **房间级能耗**：表计与房间没有结构化映射；
- **整栋建筑总能耗**：当前只有租户/项目级汇总；
- **气象归一化**：没有气象数据；
- **工单闭环率/SLA 结论**：MySQL `eo_ticket` 可查工单明细，但闭环口径要按业务状态和 SLA 确认；无数据时不能编造；
- **电费/费用**：`energy_expense` 字段存在，MySQL 有电价表，但当前未接入正式电费模板，不能用 kWh 直接代替；
- **实时控制**：只读，不能下发指令；
- **小时表缺数/完整性**：支持单日已结束整点的覆盖检查；不判断采集值准确性、补数状态或跨日缺失；
- **跨租户查询**：默认允许按租户名切换；生产仍应由网关/SSO 做租户身份校验。

### 你可以这样问

- 南网能源昨天总用电多少？
- 荣耀近 30 天用电趋势？
- 南网能源哪块表用电最多？
- 南网能源 36 层会议室现在室温多少？
- 南网能源昨天控制成功率多少？
- 南网能源今天小时表有缺数吗？
- 南网能源 MySQL 里有多少设备、多少表计？
- 南网能源配置了哪些冷站？
- 南网能源冷站今天用电多少？
- 南网能源昨天用电异常吗？为什么？
- 南网能源 MySQL 控制任务成功/失败分布？
- 目前支持哪些租户的数据？
- 南网能源昨天总用电查哪张表、哪些字段？
- 那今天呢？/ 那荣耀呢？/ 那 37 层呢？
"""


@dataclass
class AgentResult:
    intent: str
    title: str
    answer: str
    thinking: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _date_label(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def parse_time_range(question: str, today: date) -> tuple[date, date, str]:
    """解析中文时间表达，返回闭区间。默认近 7 天。"""
    text = question.replace(" ", "")

    explicit = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if explicit:
        try:
            day = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
            return day, day, _date_label(day)
        except ValueError:
            pass

    if "前天" in text:
        day = today - timedelta(days=2)
        return day, day, "前天"
    if "昨天" in text or "昨日" in text:
        day = today - timedelta(days=1)
        return day, day, "昨天"
    if "今天" in text or "今日" in text:
        return today, today, "今天"
    if "上月" in text or "上个月" in text:
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end, "上月"
    if "本月" in text or "这个月" in text:
        return today.replace(day=1), today, "本月"
    if "本周" in text or "这周" in text:
        monday = today - timedelta(days=today.weekday())
        return monday, today, "本周"

    for pattern, days, label in (
        (r"近30天|最近30天|过去30天|近一个月|最近一个月", 30, "近30天"),
        (r"近14天|最近14天|过去14天|近两周", 14, "近14天"),
        (r"近7天|最近7天|过去7天|近一周|最近一周", 7, "近7天"),
        (r"近90天|最近90天|过去90天|近三个月", 90, "近90天"),
    ):
        if re.search(pattern, text):
            return today - timedelta(days=days - 1), today, label

    return today - timedelta(days=6), today, "近7天"


def detect_intent(question: str) -> str:
    text = question.replace(" ", "")
    if any(
        word in text
        for word in (
            "我刚才问",
            "刚才问",
            "上一个问题",
            "上一条",
            "之前问",
            "前面问",
            "我们聊了什么",
            "聊过什么",
            "问过什么",
            "对话历史",
            "上一轮",
            "刚才说",
        )
    ):
        return "memory"
    if "租户" in text and any(
        word in text for word in ("哪些", "支持", "关系", "数据库", "库", "接入", "列表")
    ):
        return "tenants"
    if any(word in text for word in ("哪些库", "数据库关系", "库的关系", "数据源关系")):
        return "tenants"
    if any(
        word in text
        for word in (
            "哪张表",
            "什么表",
            "表结构",
            "字段",
            "数据来源",
            "来源表",
            "怎么查",
            "用什么表",
            "查什么数据",
            "口径",
            "知识库",
            "库表关系",
            "表关系",
            "代码",
            "调用链",
            "接口",
            "架构",
        )
    ):
        return "knowledge"
    if any(
        word in text
        for word in (
            "缺数",
            "缺少",
            "缺失",
            "漏采",
            "漏数",
            "断点",
            "完整性",
            "数据质量",
            "数据不全",
            "数据是否完整",
            "有没有缺",
            "是否有缺",
            "未上报",
            "空值",
            "空窗",
            "补数",
        )
    ) or (
        any(word in text for word in ("小时表", "小时数据"))
        and any(word in text for word in ("缺", "完整", "漏", "断", "有数据", "没数据", "为空"))
    ):
        return "data_quality"
    if any(
        word in text
        for word in ("能做什么", "能力", "边界", "支持什么", "支持哪些", "能查什么", "不能查什么")
    ):
        return "capability"
    lowered = text.lower()
    if any(word in text for word in ("冷站", "冷源", "供冷")):
        return "cold_station"
    if any(
        word in lowered
        for word in ("mysql", "业务库", "关系库", "工单", "设备台账", "设备档案", "表计档案")
    ):
        return "mysql"
    if any(word in text for word in ("告警", "报警")):
        return "alarm"
    if any(word in text for word in ("控制", "成功率", "下发", "执行失败")):
        return "control"
    if any(word in text for word in ("室温", "温度", "设定温度", "空调面板")):
        return "room_temp"
    if any(word in text for word in ("排名", "最多", "top", "哪些表", "哪块表")):
        return "ranking"
    if any(word in text for word in ("趋势", "曲线", "每天", "逐日", "走势")):
        return "trend"
    if any(
        word in text
        for word in (
            "总用电",
            "用电量",
            "用了多少电",
            "耗电",
            "电量",
            "用电数据",
            "用电情况",
            "用电",
        )
    ):
        return "total"
    return "unknown"


def extract_room_keyword(question: str) -> str:
    text = question.replace(" ", "")
    # 优先取楼层，避免“36层会议室”这种复合名称在设备表里匹配不到
    floor = re.search(r"(\d+层)", text)
    if floor:
        return floor.group(1)
    match = re.search(r"(.{1,12}?)(?:现在|目前|的)?(?:室温|温度|设定温度)", text)
    if match:
        keyword = match.group(1)
        keyword = re.sub(r"^(请问|帮我查|查一下|看一下|看看)", "", keyword)
        return keyword.strip("，,。?？:：")[:12]
    # 追问“那 37 层呢 / 那会议室呢”：没有“室温”二字时也取出对象。
    follow = re.search(r"^(?:那|换成|改看|看看)(.{1,12}?)(?:呢|怎么样|如何)?[?？]?$", text)
    if follow:
        candidate = follow.group(1).strip("，,。?？:：")[:12]
        if re.fullmatch(
            r"今天|今日|昨天|昨日|前天|本月|这个月|本周|这周|上个月|上月|"
            r"近.*天|最近.*天|过去.*天|现在|目前",
            candidate,
        ):
            return ""
        return candidate
    return ""


DATA_INTENTS = {"total", "trend", "ranking", "room_temp", "control", "alarm"}
CONTEXT_DATA_INTENTS = DATA_INTENTS | {"data_quality", "mysql", "cold_station"}


def has_time_expression(question: str) -> bool:
    """判断问题里是否显式带了时间词；用于决定是否沿用上一轮时间范围。"""
    text = question.replace(" ", "")
    if re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text):
        return True
    return bool(
        re.search(
            r"前天|昨天|昨日|今天|今日|上月|上个月|本月|这个月|本周|这周|"
            r"近\d+天|最近\d+天|过去\d+天|近一周|最近一周|近一个月|最近一个月|近三个月",
            text,
        )
    )


def looks_like_followup(question: str) -> bool:
    """只有明显的追问才继承上一轮指标，避免把新问题误当成旧指标。"""
    text = question.replace(" ", "").strip("，,。.!！?？")
    if not text:
        return False
    if text.endswith("呢"):
        return True
    if re.match(r"^(那|换成|改看|再看|继续|还有|也|同样|刚才)", text):
        return True
    return bool(
        re.fullmatch(
            r"今天|今日|昨天|昨日|前天|本月|这个月|本周|这周|上个月|上月|"
            r"近\d+天|最近\d+天|过去\d+天|近一周|最近一周|近一个月|最近一个月",
            text,
        )
    )


@dataclass
class ConversationContext:
    """从同一会话的历史 messages 中提取的可继承上下文。"""

    previous_question: str = ""
    intent: str = ""
    metric: str = ""
    tenant: Tenant | None = None
    start: date | None = None
    end: date | None = None
    range_label: str = ""
    room_keyword: str = ""
    user_turns: list[str] = field(default_factory=list)
    assistant_turns: list[str] = field(default_factory=list)

    @property
    def has_context(self) -> bool:
        return bool(self.previous_question or self.intent or self.tenant)

    @classmethod
    def from_messages(
        cls,
        messages: list[dict[str, str]] | None,
        today: date,
    ) -> "ConversationContext":
        context = cls()
        if not messages:
            return context

        cleaned: list[tuple[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            text = str(item.get("content") or "").strip()
            if role in ("user", "assistant") and text:
                cleaned.append((role, text))

        # 最后一条 user 是本轮问题；它之前的 user/assistant 才是上下文。
        last_user = next(
            (index for index in range(len(cleaned) - 1, -1, -1) if cleaned[index][0] == "user"),
            None,
        )
        if last_user is None:
            return context
        prior = cleaned[:last_user]
        context.user_turns = [text for role, text in prior if role == "user"]
        context.assistant_turns = [text for role, text in prior if role == "assistant"]
        if not context.user_turns:
            return context

        context.previous_question = context.user_turns[-1]
        intent = ""
        for text in context.user_turns:
            turn_intent = detect_intent(text)
            turn_plan = plan_question(text)
            if turn_plan.key:
                context.metric = turn_plan.key
            if turn_intent in CONTEXT_DATA_INTENTS:
                intent = turn_intent
            if has_time_expression(text):
                start, end, label = parse_time_range(text, today)
                context.start, context.end, context.range_label = start, end, label
            tenant = resolve_tenant(text)
            if tenant:
                context.tenant = tenant
            if turn_intent == "room_temp" or (turn_intent == "unknown" and intent == "room_temp"):
                keyword = extract_room_keyword(text)
                if keyword:
                    context.room_keyword = keyword
        context.intent = intent
        return context

    def as_facts(self) -> dict[str, Any]:
        tenant = None
        if self.tenant:
            tenant = {
                "tenant_id": self.tenant.tenant_id,
                "name": self.tenant.name or "未提供",
                "code": self.tenant.code,
                "doris_db": self.tenant.doris_db,
                "mysql_db": self.tenant.mysql_db,
            }
        time_range = None
        if self.start and self.end:
            time_range = {
                "start": _fmt(self.start),
                "end": _fmt(self.end),
                "label": self.range_label,
            }
        previous_answer = self.assistant_turns[-1] if self.assistant_turns else ""
        return {
            "previous_question": self.previous_question,
            "previous_answer": previous_answer[:2500] if previous_answer else None,
            "intent": self.intent or None,
            "metric": self.metric or None,
            "tenant": tenant,
            "time_range": time_range,
            "room_keyword": self.room_keyword or None,
            "recent_user_questions": self.user_turns[-5:],
        }


def _thinking_for(intent: str) -> list[str]:
    common = [
        "解析问题与时间范围",
        "选择数据源与指标模板",
        "通过 Archery 查询（只读）",
        "校验数据完整性",
        "组织结论",
    ]
    if intent == "capability":
        return ["读取能力清单", "整理数据口径与边界"]
    if intent == "tenants":
        return ["读取租户目录", "匹配租户与数据库", "核对当前租户上下文"]
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
    if intent == "cold_station_electricity":
        return [
            "识别冷站用电指标",
            "读取冷站与设备映射",
            "检查电表是否可用",
            "按冷站聚合用电",
            "组织结论与边界",
        ]
    if intent == "cold_station_cooling":
        return ["识别冷站冷量指标", "读取冷站配置", "检查冷量口径", "说明边界"]
    if intent == "energy_anomaly":
        return [
            "识别用电异常问题",
            "读取当前区间用电",
            "读取前 7 天基线",
            "计算偏差并判断",
            "组织结论与边界",
        ]
    if intent in ("device_count", "meter_count", "asset_counts"):
        return ["识别实体数量", "读取 MySQL 台账", "过滤租户与删除标记", "返回数量"]
    if intent == "room_temp":
        return ["解析设备名称", "解析设备 ID", "读取最近点位", "校验在线状态"]
    if intent == "control":
        return ["解析时间范围", "选择控制任务模板", "聚合成功/失败", "核对状态码口径"]
    if intent == "alarm":
        return ["解析时间范围", "探测告警表", "按级别/状态聚合", "判断是否可用"]
    return common


def should_investigate(question: str) -> bool:
    """开放调查只处理像数据问题的新问题，避免“你好/谢谢”也去连库。"""
    text = question.replace(" ", "")
    if len(text) < 4:
        return False
    if any(
        word in text
        for word in (
            "你好",
            "谢谢",
            "你是谁",
            "在吗",
            "帮助",
            "天气",
            "股票",
            "新闻",
            "笑话",
            "翻译",
            "写代码",
            "几号",
            "星期几",
        )
    ):
        return False
    return any(
        word in text
        for word in (
            "表",
            "数据",
            "用电",
            "电量",
            "能耗",
            "设备",
            "空调",
            "告警",
            "报警",
            "控制",
            "温度",
            "湿度",
            "压力",
            "小时",
            "缺",
            "异常",
            "统计",
            "多少",
            "几个",
            "哪些",
            "趋势",
            "排名",
            "运行",
            "在线",
            "离线",
            "停机",
            "状态",
            "明细",
            "分布",
            "记录",
            "工单",
            "台账",
            "档案",
            "空间",
            "楼层",
            "楼栋",
            "区域",
            "房间",
            "冷量",
            "电价",
            "电费",
            "mysql",
            "业务库",
            "关系库",
            "昨天",
            "今天",
            "今日",
            "本月",
            "这个月",
            "近",
        )
    )


class EnergyAgent:
    def __init__(
        self,
        provider: EnergyProvider,
        llm: LLMClient,
        today: date | None = None,
        config: Settings = settings,
    ) -> None:
        self.provider = provider
        self.llm = llm
        self.today = today or date.today()
        self._today_fixed = today is not None
        self.config = config
        self.investigator = DataInvestigator(
            llm,
            config,
            today=self.today,
            max_steps=getattr(config, "agent_max_steps", None),
        )

    def refresh_today(self) -> None:
        """进程常驻时按请求刷新业务日期，避免跨午夜后“今天”仍是启动日。"""
        if self._today_fixed:
            return
        self.today = date.today()
        if hasattr(self.investigator, "today"):
            self.investigator.today = self.today

    async def answer(
        self,
        question: str,
        thinking_enabled: bool = False,
        history: list[dict[str, str]] | None = None,
        progress: Any = None,
    ) -> AgentResult:
        """progress: 可选的 async 回调，用于把真实的分析/工具步骤推给前端。"""
        self.refresh_today()
        question = question.strip()
        if progress is not None:
            await progress("解析问题：识别租户、时间范围与指标口径")
        context = ConversationContext.from_messages(history, self.today)
        plan = plan_question(
            question,
            context_metric=context.metric if looks_like_followup(question) else None,
        )
        intent = detect_intent(question)

        # 只有明显的追问才继承上一轮指标；新问题即使没识别出意图，也不能强行套旧指标。
        if intent == "unknown" and context.intent and looks_like_followup(question):
            intent = context.intent
        start, end, range_label = parse_time_range(question, self.today)
        # 追问没有自带时间词时，沿用上一轮显式时间范围。
        if (
            intent in CONTEXT_DATA_INTENTS
            and not has_time_expression(question)
            and context.start is not None
            and context.end is not None
        ):
            start, end, range_label = context.start, context.end, context.range_label
        if (
            intent == "data_quality"
            and not has_time_expression(question)
            and not (context.start and context.end)
        ):
            start = end = self.today
            range_label = "今天"
        thinking = (
            _thinking_for(self._semantic_thinking_key(plan, intent))
            if thinking_enabled
            else []
        )
        title = self._title(question)

        if intent == "memory":
            facts: dict[str, Any] = {
                "intent": intent,
                "question": question,
                "conversation": context.as_facts(),
            }
            return AgentResult(
                intent=intent,
                title="会话记忆",
                answer=self._memory_answer(context, question),
                thinking=thinking,
                facts=facts,
            )
        # 定义/口径类问题（怎么算、公式、流程、区别）优先走业务知识，
        # 不落到数据模板上答成“没有数据”。
        concept = concept_answer(question)
        if concept:
            return AgentResult(
                intent="knowledge",
                title=title,
                answer=concept,
                thinking=thinking,
                facts={
                    "intent": "knowledge",
                    "question": question,
                    "source": "server/concepts.py",
                },
            )
        if intent == "knowledge":
            return AgentResult(
                intent=intent,
                title=title,
                answer=knowledge_answer(question),
                thinking=thinking,
                facts={
                    "intent": intent,
                    "question": question,
                    "source": "server/knowledge.py",
                },
            )
        requested_tenant = resolve_tenant(question)
        if requested_tenant is None and intent in (
            DATA_INTENTS | {"data_quality", "unknown", "cold_station"}
        ):
            requested_tenant = context.tenant
        room_keyword = extract_room_keyword(question)
        if intent == "room_temp" and not room_keyword:
            room_keyword = context.room_keyword
        tenant_facts = self._tenant_facts(requested_tenant) if requested_tenant else None
        query_provider = self.provider

        semantic_result = await self._semantic_result(
            plan,
            question=question,
            requested_tenant=requested_tenant,
            tenant_facts=tenant_facts,
            start=start,
            end=end,
            range_label=range_label,
            thinking=thinking,
        )
        if semantic_result is not None:
            return semantic_result

        # 固定模板只能回答“明确命中口径”的问题。一旦问题要求额外维度
        # （楼层/设备/类型/对比……），先把问题交给能自己选工具的调查循环。
        loop_attempted = False
        if (
            self.config.agent_tool_loop
            and self.llm.configured
            and self._should_use_tool_loop(question, plan, intent)
        ):
            loop_attempted = True
            investigation = await self._try_investigation(
                question,
                intent,
                requested_tenant,
                tenant_facts,
                context,
                progress=progress,
                force=True,
            )
            if investigation is not None:
                return self._investigation_result(
                    intent, title, question, investigation, context, thinking_enabled
                )

        if intent == "cold_station":
            mysql_provider = self._mysql_provider_for_tenant(requested_tenant)
            if mysql_provider is None:
                return AgentResult(
                    intent=intent,
                    title="冷站配置",
                    answer=(
                        "### 结论\n\n"
                        "当前没有可用的 MySQL 冷站配置数据源，无法返回冷站清单。"
                        "没有用设备名称或其它指标替代。\n\n"
                        "### 限制\n\n"
                        "- 冷站配置在 MySQL `eo_energy_saving_cooling_base_configs`，"
                        "需要 `MYSQL_ENABLED=true` 且当前租户库可访问。"
                    ),
                    thinking=thinking,
                    facts=self._base_facts(
                        intent, question, start, end, range_label, tenant_facts
                    ),
                )
            try:
                rows = await mysql_provider.cooling_stations()
            except Exception as exc:  # noqa: BLE001 - 配置表异常要明确报边界
                logger.warning("cold station config query failed: %s", exc)
                facts = self._base_facts(
                    intent, question, start, end, range_label, tenant_facts
                )
                facts["result"] = {
                    "available": False,
                    "error": str(exc),
                    "source": f"{mysql_provider.database}.eo_energy_saving_cooling_base_configs",
                }
                return AgentResult(
                    intent=intent,
                    title="冷站配置",
                    answer=(
                        "### 结论\n\n"
                        "冷站配置表查询失败，当前不能确认配置了哪些冷站；"
                        "这不是“0 个冷站”，下面保留失败原因。\n\n"
                        f"### 失败原因\n\n- `{exc}`\n\n"
                        "### 数据源\n\n"
                        f"- `{mysql_provider.database}.eo_energy_saving_cooling_base_configs`"
                    ),
                    thinking=thinking,
                    facts=facts,
                )
            facts = self._base_facts(
                intent, question, start, end, range_label, tenant_facts
            )
            facts["result"] = {
                "available": True,
                "rows": rows,
                "count": len(rows),
                "source": f"{mysql_provider.database}.eo_energy_saving_cooling_base_configs",
            }
            if context.has_context:
                facts["conversation"] = context.as_facts()
            return AgentResult(
                intent=intent,
                title="冷站配置",
                answer=self._cold_station_answer(
                    rows,
                    tenant_facts=tenant_facts,
                    database=mysql_provider.database,
                ),
                thinking=thinking,
                facts=facts,
            )

        if intent == "data_quality":
            quality_provider = self._provider_for_tenant(requested_tenant)
            if (
                quality_provider is None
                and requested_tenant
                and requested_tenant.tenant_id != self.config.doris_tenant_id
            ):
                return AgentResult(
                    intent=intent,
                    title="小时表缺数检测",
                    answer=self._tenant_query_blocked(requested_tenant, "isolation"),
                    thinking=thinking,
                    facts=self._base_facts(
                        intent, question, start, end, range_label, tenant_facts
                    ),
                )

            if (
                start == end
                and quality_provider is not None
                and hasattr(quality_provider, "hourly_completeness")
            ):
                try:
                    coverage = await quality_provider.hourly_completeness(start, end)
                except Exception as exc:  # noqa: BLE001 - 缺数模板失败时转开放调查
                    logger.warning("hourly completeness failed: %s", exc)
                else:
                    if coverage.get("available"):
                        facts = self._base_facts(
                            intent, question, start, end, range_label, tenant_facts
                        )
                        facts["hourly_completeness"] = coverage
                        if context.has_context:
                            facts["conversation"] = context.as_facts()
                        return AgentResult(
                            intent=intent,
                            title="小时表缺数检测",
                            answer=self._hourly_quality_answer(
                                coverage, start, range_label, tenant_facts
                            ),
                            thinking=thinking,
                            facts=facts,
                        )

            investigation = await self._try_investigation(
                question,
                intent,
                requested_tenant,
                tenant_facts,
                context,
                progress=progress,
                force=True,
            )
            if investigation is not None:
                return self._investigation_result(
                    intent, "小时表缺数检测", question, investigation, context, thinking_enabled
                )
            facts = self._base_facts(
                intent, question, start, end, range_label, tenant_facts
            )
            if context.has_context:
                facts["conversation"] = context.as_facts()
            return AgentResult(
                intent=intent,
                title="小时表缺数检测",
                answer=self._data_quality_answer(start, end, range_label, tenant_facts),
                thinking=thinking,
                facts=facts,
            )

        if intent in ("unknown", "mysql") and not loop_attempted:
            investigation = await self._try_investigation(
                question,
                intent,
                requested_tenant,
                tenant_facts,
                context,
                progress=progress,
                force=True,
            )
            if investigation is not None:
                return self._investigation_result(
                    intent, title, question, investigation, context, thinking_enabled
                )
            if intent == "mysql":
                facts = self._base_facts(intent, question, start, end, range_label, tenant_facts)
                if context.has_context:
                    facts["conversation"] = context.as_facts()
                return AgentResult(
                    intent=intent,
                    title=title,
                    answer=(
                        "### 结论\n\n"
                        "这次 MySQL 只读调查没有完成：可能是数据源未开启、模型未配置，"
                        "或当前租户库不可访问。没有用其它指标替代，也没有编造数字。\n\n"
                        "### 限制\n\n"
                        "- 所有 MySQL 查询都必须经过 Archery 只读通道和 SQL Guard。\n"
                        "- 可先确认 `MYSQL_ENABLED`、`MYSQL_ARCHERY_INSTANCE` 与租户库是否存在。"
                    ),
                    thinking=thinking,
                    facts=facts,
                )

        data_intents = DATA_INTENTS
        if not requested_tenant and "租户" in question and intent in data_intents:
            return AgentResult(
                intent=intent,
                title=title,
                answer=self._unknown_tenant_answer(),
                thinking=thinking,
                facts=self._base_facts(intent, question, start, end, range_label),
            )
        if requested_tenant and requested_tenant.tenant_id != self.config.doris_tenant_id:
            if intent in data_intents and not self.config.allow_tenant_query:
                return AgentResult(
                    intent=intent,
                    title=title,
                    answer=self._tenant_query_blocked(requested_tenant, "isolation"),
                    thinking=thinking,
                    facts=self._base_facts(intent, question, start, end, range_label, tenant_facts),
                )
            if intent in data_intents:
                query_provider = get_tenant_provider(requested_tenant, self.config)
                if query_provider is None:
                    return AgentResult(
                        intent=intent,
                        title=title,
                        answer=self._tenant_query_blocked(requested_tenant, "mock"),
                        thinking=thinking,
                        facts=self._base_facts(
                            intent, question, start, end, range_label, tenant_facts
                        ),
                    )
            elif intent == "unknown":
                return AgentResult(
                    intent=intent,
                    title=title,
                    answer=self._tenant_detail_answer(requested_tenant),
                    thinking=thinking,
                    facts=self._base_facts(intent, question, start, end, range_label, tenant_facts),
                )

        try:
            facts = await self._collect(
                intent,
                question,
                start,
                end,
                range_label,
                provider=query_provider,
                room_keyword=room_keyword,
            )
        except Exception as exc:  # noqa: BLE001 - 返回可读错误，不把堆栈暴露给前端
            facts = self._base_facts(
                intent, question, start, end, range_label, tenant_facts
            )
            facts["error"] = str(exc)
            return AgentResult(
                intent=intent,
                title=title,
                answer=f"查询失败：{exc}\n\n请检查数据源配置，或改用能力问题了解当前可查范围。",
                thinking=thinking,
                facts=facts,
            )

        if tenant_facts:
            facts["tenant"] = tenant_facts
        if context.has_context:
            facts["conversation"] = context.as_facts()

        # 固定模板“查不到/不可用/0 行”不等于问题无解：先让调查 Agent 换路径取证，
        # 它失败再退回确定性说明，避免把空结果直接当结论。
        if (
            not loop_attempted
            and self.config.agent_tool_loop
            and self.llm.configured
            and self._needs_escalation(intent, facts)
        ):
            investigation = await self._try_investigation(
                question,
                intent,
                requested_tenant,
                tenant_facts,
                context,
                progress=progress,
                force=True,
                seed_facts=facts,
            )
            if investigation is not None:
                return self._investigation_result(
                    intent, title, question, investigation, context, thinking_enabled
                )

        if intent == "tenants":
            return AgentResult(
                intent=intent,
                title=title,
                answer=tenant_answer(),
                thinking=thinking,
                facts=facts,
            )
        if intent in ("capability", "unknown"):
            answer = CAPABILITY_TEXT
            if intent == "unknown":
                answer = (
                    "我暂时没有识别出具体指标。当前支持总用电、趋势、表计排名、"
                    "室温、控制统计、告警统计和租户目录。\n\n" + CAPABILITY_TEXT
                )
            return AgentResult(intent=intent, title=title, answer=answer, thinking=thinking, facts=facts)

        if self._is_unavailable(intent, facts):
            # 数据不可用时不能让模型把“空表”润色成“0 条”，直接用确定性说明。
            answer = self._fallback_answer(intent, facts)
            answer += "\n\n### 限制\n\n" + self._limitations(intent, facts)
        else:
            try:
                answer = await self._narrate(question, facts)
            except LLMError as exc:
                # 模型不可用时仍返回结构化结果，避免整个分析失败
                logger.warning("llm narration failed: %s", exc)
                answer = ""
            if answer:
                answer = self._strip_limitations(answer)
                answer += "\n\n### 限制\n\n" + self._limitations(intent, facts)
            else:
                answer = self._fallback_answer(intent, facts)
        if tenant_facts:
            answer = self._tenant_prefix(tenant_facts) + answer
        return AgentResult(intent=intent, title=title, answer=answer, thinking=thinking, facts=facts)

    @staticmethod
    def _tenant_facts(tenant: Tenant) -> dict[str, Any]:
        return {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name or "未提供",
            "code": tenant.code,
            "doris_db": tenant.doris_db,
            "mysql_db": tenant.mysql_db,
        }

    @staticmethod
    def _tenant_prefix(tenant: dict[str, Any]) -> str:
        return (
            f"**数据对象：{tenant['name']}（{tenant['tenant_id']} / "
            f"`{tenant['code']}` / `{tenant['doris_db']}`）**\n\n"
        )

    def _base_facts(
        self,
        intent: str,
        question: str,
        start: date,
        end: date,
        range_label: str,
        tenant: dict[str, Any] | None = None,
        provider: EnergyProvider | None = None,
    ) -> dict[str, Any]:
        active = provider or self.provider
        facts: dict[str, Any] = {
            "intent": intent,
            "question": question,
            "dataset": active.dataset,
            "mode": active.mode,
            "time_range": {"start": _fmt(start), "end": _fmt(end), "label": range_label},
        }
        if tenant:
            facts["tenant"] = tenant
        return facts

    @staticmethod
    def _semantic_thinking_key(plan: QuestionPlan, intent: str) -> str:
        """让前端阶段名跟随语义计划，而不是只看旧 intent。"""
        if plan.metric and plan.key in {
            "energy_anomaly",
            "cold_station_electricity",
            "cold_station_cooling",
            "device_count",
            "meter_count",
            "asset_counts",
        }:
            return plan.key
        return intent

    async def _semantic_result(
        self,
        plan: QuestionPlan,
        *,
        question: str,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        start: date,
        end: date,
        range_label: str,
        thinking: list[str],
    ) -> AgentResult | None:
        """执行语义层中已经定义、且固定模板未覆盖的指标。"""
        key = plan.key
        if key == "energy_anomaly":
            return await self._energy_anomaly_answer(
                question,
                requested_tenant=requested_tenant,
                tenant_facts=tenant_facts,
                start=start,
                end=end,
                range_label=range_label,
                thinking=thinking,
            )
        if key == "cold_station_electricity":
            return await self._cold_station_electricity_answer(
                question,
                requested_tenant=requested_tenant,
                tenant_facts=tenant_facts,
                start=start,
                end=end,
                range_label=range_label,
                thinking=thinking,
                analysis=plan.analysis,
            )
        if key == "cold_station_cooling":
            return await self._cold_station_cooling_answer(
                question,
                requested_tenant=requested_tenant,
                tenant_facts=tenant_facts,
                start=start,
                end=end,
                range_label=range_label,
                thinking=thinking,
            )
        if key == "device_count":
            return await self._count_answer(
                "device_count",
                question,
                requested_tenant=requested_tenant,
                tenant_facts=tenant_facts,
                start=start,
                end=end,
                range_label=range_label,
                thinking=thinking,
            )
        if key == "meter_count":
            return await self._count_answer(
                "meter_count",
                question,
                requested_tenant=requested_tenant,
                tenant_facts=tenant_facts,
                start=start,
                end=end,
                range_label=range_label,
                thinking=thinking,
            )
        if key == "asset_counts":
            return await self._asset_counts_answer(
                question,
                requested_tenant=requested_tenant,
                tenant_facts=tenant_facts,
                start=start,
                end=end,
                range_label=range_label,
                thinking=thinking,
            )
        return None

    def _metric_facts(
        self,
        key: str,
        question: str,
        start: date,
        end: date,
        range_label: str,
        tenant_facts: dict[str, Any] | None,
        *,
        source: str,
        dataset: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "intent": key,
            "metric": key,
            "question": question,
            "dataset": dataset,
            "source": source,
            "time_range": {"start": _fmt(start), "end": _fmt(end), "label": range_label},
            "result": result,
        }
        if tenant_facts:
            facts["tenant"] = tenant_facts
        return facts

    @staticmethod
    def _meter_ids(value: Any) -> list[int]:
        return [
            int(part)
            for part in str(value or "").split(",")
            if part.strip().isdigit() and int(part) > 0
        ]

    async def _count_answer(
        self,
        key: str,
        question: str,
        *,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        start: date,
        end: date,
        range_label: str,
        thinking: list[str],
    ) -> AgentResult:
        label = "设备" if key == "device_count" else "表计"
        unit = "台" if key == "device_count" else "块"
        table = "eo_devices" if key == "device_count" else "eo_energy_meters"
        title = f"{label}数量"
        mysql = self._mysql_provider_for_tenant(requested_tenant)
        if mysql is None:
            return AgentResult(
                intent=key,
                title=title,
                answer=(
                    f"### 结论\n\n当前没有可用的 MySQL 数据源，无法统计{label}数量；"
                    "这不是 0。\n\n"
                    "### 数据源\n\n"
                    f"- MySQL `{table}`（需要 MySQL 数据源可用）"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    key,
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source=f"mysql.{table}",
                    dataset="MySQL（未连接）",
                    result={"available": False, "reason": "mysql provider unavailable"},
                ),
            )
        try:
            count = (
                await mysql.count_devices()
                if key == "device_count"
                else await mysql.count_meters()
            )
            available = True
            error = ""
        except Exception as exc:  # noqa: BLE001 - 明确报数据源边界
            count = 0
            available = False
            error = str(exc)

        scope = tenant_facts.get("name") if tenant_facts else "当前租户"
        tenant_id = tenant_facts.get("tenant_id") if tenant_facts else mysql.tenant_id
        if available:
            answer = (
                "### 结论\n\n"
                f"{scope} MySQL `{table}` 当前 **{_fmt_num(count, 0)} {unit}**"
                f"（tenant_id={tenant_id}，deleted=0）。\n\n"
                "### 口径\n\n"
                f"- 数据源：`{mysql.database}.{table}`\n"
                "- 过滤：当前租户、当前项目、`deleted=0`\n"
                "- 这是档案数量，不等于在线数量或当天有数据的数量。\n"
            )
        else:
            answer = (
                "### 结论\n\n"
                f"{scope} MySQL `{table}` 查询失败，当前不能确认{label}数量；"
                "这不是 0。\n\n"
                f"### 失败原因\n\n- `{error}`\n"
            )
        if tenant_facts:
            answer = self._tenant_prefix(tenant_facts) + answer
        return AgentResult(
            intent=key,
            title=title,
            answer=answer,
            thinking=thinking,
            facts=self._metric_facts(
                key,
                question,
                start,
                end,
                range_label,
                tenant_facts,
                source=f"{mysql.database}.{table}",
                dataset=mysql.dataset,
                result={
                    "available": available,
                    "count": count,
                    "error": error,
                    "table": f"{mysql.database}.{table}",
                },
            ),
        )

    async def _asset_counts_answer(
        self,
        question: str,
        *,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        start: date,
        end: date,
        range_label: str,
        thinking: list[str],
    ) -> AgentResult:
        title = "设备与表计数量"
        mysql = self._mysql_provider_for_tenant(requested_tenant)
        if mysql is None:
            return AgentResult(
                intent="asset_counts",
                title=title,
                answer=(
                    "### 结论\n\n当前没有可用的 MySQL 数据源，无法统计设备/表计数量；"
                    "这不是 0。\n\n"
                    "### 数据源\n\n- MySQL `eo_devices` / `eo_energy_meters`"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "asset_counts",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="mysql.eo_devices+eo_energy_meters",
                    dataset="MySQL（未连接）",
                    result={"available": False, "reason": "mysql provider unavailable"},
                ),
            )
        try:
            devices = await mysql.count_devices()
            meters = await mysql.count_meters()
            available = True
            error = ""
        except Exception as exc:  # noqa: BLE001
            devices = meters = 0
            available = False
            error = str(exc)

        scope = tenant_facts.get("name") if tenant_facts else "当前租户"
        tenant_id = tenant_facts.get("tenant_id") if tenant_facts else mysql.tenant_id
        if available:
            answer = (
                "### 结论\n\n"
                f"{scope} MySQL 当前 **设备 {_fmt_num(devices, 0)} 台**、"
                f"**表计 {_fmt_num(meters, 0)} 块**"
                f"（tenant_id={tenant_id}，deleted=0）。\n\n"
                "### 口径\n\n"
                f"- 设备：`{mysql.database}.eo_devices`；\n"
                f"- 表计：`{mysql.database}.eo_energy_meters`；\n"
                "- 过滤：当前租户、当前项目、`deleted=0`；\n"
                "- 两类档案不是一对一关系，不能互相替代。\n"
            )
        else:
            answer = (
                "### 结论\n\n"
                f"{scope}设备/表计数量查询失败，当前不能确认数量；"
                "这不是 0。\n\n"
                f"### 失败原因\n\n- `{error}`"
            )
        if tenant_facts:
            answer = self._tenant_prefix(tenant_facts) + answer
        return AgentResult(
            intent="asset_counts",
            title=title,
            answer=answer,
            thinking=thinking,
            facts=self._metric_facts(
                "asset_counts",
                question,
                start,
                end,
                range_label,
                tenant_facts,
                source=f"{mysql.database}.eo_devices + eo_energy_meters",
                dataset=mysql.dataset,
                result={
                    "available": available,
                    "devices": devices,
                    "meters": meters,
                    "error": error,
                },
            ),
        )

    async def _energy_anomaly_answer(
        self,
        question: str,
        *,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        start: date,
        end: date,
        range_label: str,
        thinking: list[str],
    ) -> AgentResult:
        title = "用电异常分析"
        provider = self._provider_for_tenant(requested_tenant)
        if provider is None:
            return AgentResult(
                intent="energy_anomaly",
                title=title,
                answer=self._tenant_query_blocked(requested_tenant, "isolation"),
                thinking=thinking,
                facts=self._metric_facts(
                    "energy_anomaly",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset="Doris（不可用）",
                    result={"available": False, "reason": "provider blocked"},
                ),
            )

        # 当天日表通常还未完整生成。只查今天时明确拒绝；区间包含今天时截到昨天。
        if end >= self.today:
            if start >= self.today:
                scope = tenant_facts.get("name") if tenant_facts else "当前租户"
                answer = (
                    "### 结论\n\n"
                    f"{scope}的今天数据尚未结束，日表也可能还是 T+1 未生成，"
                    "因此**不能用当前值判断今天用电是否异常**。\n\n"
                    "### 建议\n\n"
                    "- 查昨天或最近一个完整日，才能和基线比较；\n"
                    "- 如果坚持看今天，只能看当前累计值，不能下异常结论。\n\n"
                    "### 口径\n\n"
                    "- 异常判断需要完整日数据 + 前 7 天基线；\n"
                    "- 当前返回的是边界说明，不是 0 异常。"
                )
                if tenant_facts:
                    answer = self._tenant_prefix(tenant_facts) + answer
                return AgentResult(
                    intent="energy_anomaly",
                    title=title,
                    answer=answer,
                    thinking=thinking,
                    facts=self._metric_facts(
                        "energy_anomaly",
                        question,
                        start,
                        end,
                        range_label,
                        tenant_facts,
                        source="doris.dm_eo_e_meter_di",
                        dataset=provider.dataset,
                        result={"available": False, "reason": "today_not_complete"},
                    ),
                )
            end = self.today - timedelta(days=1)
            requested_days = (self.today - start).days + 1
            if requested_days > 1:
                start = end - timedelta(days=requested_days - 1)
            range_label = f"{range_label}（截至昨日完整日）"

        baseline_start = start - timedelta(days=7)
        baseline_end = start - timedelta(days=1)
        try:
            current = await provider.energy_total(start, end)
            baseline_rows = await provider.energy_trend(baseline_start, baseline_end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("energy anomaly query failed: %s", exc)
            return AgentResult(
                intent="energy_anomaly",
                title=title,
                answer=(
                    "### 结论\n\n用电异常查询失败，当前不能判断是否异常；"
                    "这不是“没有异常”。\n\n"
                    f"### 失败原因\n\n- `{exc}`"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "energy_anomaly",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset=provider.dataset,
                    result={"available": False, "error": str(exc)},
                ),
            )

        if len(baseline_rows) < 3:
            answer = (
                "### 结论\n\n"
                "可用基线不足 3 天，当前不能判断用电是否异常。\n\n"
                "### 口径\n\n"
                "- 异常判断至少需要 3 个完整日作为基线；\n"
                "- 没有用“0”或“正常”替代缺失基线。"
            )
            if tenant_facts:
                answer = self._tenant_prefix(tenant_facts) + answer
            return AgentResult(
                intent="energy_anomaly",
                title=title,
                answer=answer,
                thinking=thinking,
                facts=self._metric_facts(
                    "energy_anomaly",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset=provider.dataset,
                    result={"available": False, "reason": "baseline_insufficient"},
                ),
            )

        baseline_total = sum(float(row.get("energy_value") or 0) for row in baseline_rows)
        baseline_avg = baseline_total / len(baseline_rows)
        if baseline_avg <= 0:
            answer = (
                "### 结论\n\n"
                "前 7 天基线用电为 0 或不可用，当前不能计算异常幅度；"
                "这不是“没有异常”。\n\n"
                "### 口径\n\n"
                "- 异常判断需要非零基线；\n"
                "- 没有用 0 或正常替代缺失基线。"
            )
            if tenant_facts:
                answer = self._tenant_prefix(tenant_facts) + answer
            return AgentResult(
                intent="energy_anomaly",
                title=title,
                answer=answer,
                thinking=thinking,
                facts=self._metric_facts(
                    "energy_anomaly",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset=provider.dataset,
                    result={"available": False, "reason": "zero_baseline"},
                ),
            )
        current_days = max(1, (end - start).days + 1)
        current_avg = float(current.get("value") or 0) / current_days
        deviation = ((current_avg - baseline_avg) / baseline_avg * 100) if baseline_avg else 0.0
        if abs(deviation) < 5:
            status = "基本正常"
        elif deviation > 0:
            status = "明显偏高" if deviation >= 20 else "偏高"
        else:
            status = "明显偏低" if deviation <= -20 else "偏低"

        baseline_table = "\n".join(
            f"| {row.get('date')} | {_fmt_num(row.get('energy_value'))} |"
            for row in baseline_rows[-7:]
        )
        scope = tenant_facts.get("name") if tenant_facts else "当前租户"
        answer = (
            f"### 结论\n\n"
            f"{_date_label(start)} 至 {_date_label(end)}，{scope}日均用电 "
            f"**{_fmt_num(current_avg)} kWh**，较前 7 天可用日日均 "
            f"**{_fmt_num(baseline_avg)} kWh** "
            f"{'高' if deviation >= 0 else '低'} **{abs(deviation):.1f}%**，"
            f"属于**{status}**。\n\n"
            "### 基线\n\n"
            "| 日期 | 用电量（kWh） |\n"
            "|---|---:|\n"
            f"{baseline_table}\n\n"
            "### 口径\n\n"
            f"- 当前区间：{_date_label(start)} 至 {_date_label(end)}（{range_label}），"
            f"共 {current_days} 天；总用电 {_fmt_num(current.get('value'))} kWh；\n"
            f"- 基线：{_date_label(baseline_start)} 至 {_date_label(baseline_end)}，"
            f"共 {len(baseline_rows)} 个可用日；\n"
            "- 数据源：Doris `dm_eo_e_meter_di`，按日汇总后比较日均值。\n\n"
            "### 说明\n\n"
            "- 这是**统计异常**，不是故障结论，也不等于原因；\n"
            "- 仅凭总用电无法判断“为什么异常”，还需要告警、控制记录、设备状态和分项数据；\n"
            "- 当前没有把这些辅助数据强行当成因果证据。"
        )
        if "为什么" in question or "原因" in question:
            answer += "\n- 因此这里只能回答“异常幅度”，不能回答“具体原因”。"
        if tenant_facts:
            answer = self._tenant_prefix(tenant_facts) + answer
        return AgentResult(
            intent="energy_anomaly",
            title=title,
            answer=answer,
            thinking=thinking,
            facts=self._metric_facts(
                "energy_anomaly",
                question,
                start,
                end,
                range_label,
                tenant_facts,
                source="doris.dm_eo_e_meter_di",
                dataset=provider.dataset,
                result={
                    "available": True,
                    "current": current,
                    "baseline": baseline_rows,
                    "baseline_average": round(baseline_avg, 2),
                    "deviation_percent": round(deviation, 2),
                    "status": status,
                },
            ),
        )

    async def _cold_station_cooling_answer(
        self,
        question: str,
        *,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        start: date,
        end: date,
        range_label: str,
        thinking: list[str],
    ) -> AgentResult:
        title = "冷站冷量"
        mysql = self._mysql_provider_for_tenant(requested_tenant)
        if mysql is None:
            return AgentResult(
                intent="cold_station_cooling",
                title=title,
                answer=(
                    "### 结论\n\n当前没有可用的 MySQL 冷站配置数据源，"
                    "无法判断冷站冷量；这不是 0 kWh。"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_cooling",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="mysql.cold_station",
                    dataset="MySQL（未连接）",
                    result={"available": False, "reason": "mysql provider unavailable"},
                ),
            )
        try:
            stations = await mysql.cooling_stations()
        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                intent="cold_station_cooling",
                title=title,
                answer=(
                    "### 结论\n\n冷站配置查询失败，当前不能判断冷量口径；"
                    f"这不是 0 kWh。\n\n### 失败原因\n\n- `{exc}`"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_cooling",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source=f"{mysql.database}.eo_energy_saving_cooling_base_configs",
                    dataset=mysql.dataset,
                    result={"available": False, "error": str(exc)},
                ),
            )

        if not stations:
            answer = (
                "### 结论\n\n当前租户没有有效冷站配置，因此没有冷站冷量；"
                "这不是 0 kWh。"
            )
        else:
            names = "、".join(str(row.get("cooling_name") or "-") for row in stations[:5])
            answer = (
                "### 结论\n\n"
                f"当前租户配置了 **{len(stations)} 个冷站**（{names}），"
                "但**冷量口径尚未接入**，不能返回 kWh 冷量。\n\n"
                "### 原因\n\n"
                "- `eo_energy_meter_readings.reading_value` 是抄表读数，可能是累计值，"
                "不能直接 `SUM` 当区间冷量；\n"
                "- 需要明确冷量表、倍率、首末读数、时间窗口和单位；\n"
                "- 冷量计（`energy_type='cold'`）不能当电表。\n\n"
                "### 当前可答\n\n"
                "- 冷站配置清单；\n"
                "- 冷站用电（前提是设备已映射正常电表）。"
            )
        if tenant_facts:
            answer = self._tenant_prefix(tenant_facts) + answer
        return AgentResult(
            intent="cold_station_cooling",
            title=title,
            answer=answer,
            thinking=thinking,
            facts=self._metric_facts(
                "cold_station_cooling",
                question,
                start,
                end,
                range_label,
                tenant_facts,
                source=f"{mysql.database}.eo_energy_saving_cooling_base_configs",
                dataset=mysql.dataset,
                result={
                    "available": False,
                    "reason": "cooling_metric_not_defined",
                    "stations": stations,
                },
            ),
        )

    async def _cold_station_electricity_answer(
        self,
        question: str,
        *,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        start: date,
        end: date,
        range_label: str,
        thinking: list[str],
        analysis: str = "value",
    ) -> AgentResult:
        title = "冷站用电"
        mysql = self._mysql_provider_for_tenant(requested_tenant)
        if mysql is None:
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=(
                    "### 结论\n\n当前没有可用的 MySQL 冷站配置数据源，"
                    "无法判断冷站用电；这不是 0 kWh。\n\n"
                    "### 数据源\n\n"
                    "- MySQL `eo_energy_saving_cooling_base_configs`"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="mysql.cold_station",
                    dataset="MySQL（未连接）",
                    result={"available": False, "reason": "mysql provider unavailable"},
                ),
            )

        try:
            mapping = await mysql.cold_station_meter_mapping()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cold station mapping failed: %s", exc)
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=(
                    "### 结论\n\n冷站配置/设备映射查询失败，当前不能计算冷站用电；"
                    "这不是 0 kWh。\n\n"
                    f"### 失败原因\n\n- `{exc}`"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="mysql.cold_station",
                    dataset=mysql.dataset,
                    result={"available": False, "error": str(exc)},
                ),
            )

        if not mapping:
            scope = tenant_facts.get("name") if tenant_facts else "当前租户"
            answer = (
                "### 结论\n\n"
                "当前租户**没有有效冷站配置**，所以无法按冷站统计用电；"
                "这**不是 0 kWh**，也不是“冷站停运”。\n\n"
                "### 口径\n\n"
                f"- 数据源：`{mysql.database}.eo_energy_saving_cooling_base_configs`\n"
                f"- 过滤：tenant_id={mysql.tenant_id}、project_id={self.config.doris_project_id}、deleted=0\n"
                "- 没有配置就没有冷站与设备/电表的映射，不能用总用电替代冷站用电。\n\n"
                "### 可以改问\n\n"
                f"- {scope}今天总用电多少？\n"
                f"- {scope}配置了哪些冷站？"
            )
            if tenant_facts:
                answer = self._tenant_prefix(tenant_facts) + answer
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=answer,
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source=f"{mysql.database}.eo_energy_saving_cooling_base_configs",
                    dataset=mysql.dataset,
                    result={"available": False, "reason": "no_cold_station_config", "stations": []},
                ),
            )

        all_ids: list[int] = []
        for row in mapping:
            all_ids.extend(self._meter_ids(row.get("meter_ids")))
        all_ids = sorted(set(all_ids))
        scope = tenant_facts.get("name") if tenant_facts else "当前租户"
        if not all_ids:
            names = "、".join(str(row.get("cooling_name") or "-") for row in mapping[:5])
            answer = (
                "### 结论\n\n"
                f"当前租户配置了 **{len(mapping)} 个冷站**（{names}），"
                "但配置的设备没有关联正常电表，因此**无法统计冷站用电**；"
                "这不是 0 kWh。\n\n"
                "### 原因\n\n"
                "- 冷站用电需要 `eo_energy_saving_cooling_device_configs` 中的设备"
                "映射到 `eo_energy_meters` 中 `energy_type='electric'`、`meter_status=2` 的电表；\n"
                "- 现有冷站表计如果是 `energy_type='cold'` 的冷量计，不能当电表；\n"
                "- 没有映射时，不能用租户总用电代替。\n\n"
                "### 可以改问\n\n"
                f"- {scope}配置了哪些冷站？\n"
                f"- {scope}今天总用电多少？"
            )
            if tenant_facts:
                answer = self._tenant_prefix(tenant_facts) + answer
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=answer,
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source=f"{mysql.database}.eo_energy_meters",
                    dataset=mysql.dataset,
                    result={
                        "available": False,
                        "reason": "no_electric_meter_mapping",
                        "stations": mapping,
                    },
                ),
            )

        if analysis in ("anomaly", "root_cause"):
            answer = (
                "### 结论\n\n"
                f"当前租户有 {len(mapping)} 个冷站，且部分设备已映射电表，"
                "但**冷站用电异常检测尚未接入按日基线**；"
                "这里不把区间汇总值当成“是否异常”的结论。\n\n"
                "### 当前可答\n\n"
                "- 冷站某个完整区间的用电合计；\n"
                "- 冷站配置和电表映射情况；\n"
                "- 租户总用电的异常检测。\n\n"
                "### 不能答\n\n"
                "- 冷站用电“为什么异常”的原因；\n"
                "- 没有基线的冷站异常判断。"
            )
            if tenant_facts:
                answer = self._tenant_prefix(tenant_facts) + answer
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=answer,
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="mysql.cold_station + doris.dm_eo_e_meter_di",
                    dataset=mysql.dataset,
                    result={
                        "available": False,
                        "reason": "cold_station_anomaly_not_supported",
                        "stations": mapping,
                    },
                ),
            )

        doris = self._provider_for_tenant(requested_tenant)
        if doris is None:
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=self._tenant_query_blocked(requested_tenant, "isolation"),
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset="Doris（不可用）",
                    result={"available": False, "reason": "doris provider blocked"},
                ),
            )

        try:
            energy_rows = await doris.energy_by_meters(all_ids, start, end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cold station energy query failed: %s", exc)
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=(
                    "### 结论\n\n冷站电表已映射，但用电查询失败，当前不能计算；"
                    "这不是 0 kWh。\n\n"
                    f"### 失败原因\n\n- `{exc}`"
                ),
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset=doris.dataset,
                    result={"available": False, "error": str(exc), "meter_count": len(all_ids)},
                ),
            )

        by_meter = {int(row.get("meter_id") or 0): row for row in energy_rows}
        station_rows: list[dict[str, Any]] = []
        for station in mapping:
            ids = self._meter_ids(station.get("meter_ids"))
            values = [float(by_meter.get(meter_id, {}).get("energy_value") or 0) for meter_id in ids]
            station_rows.append(
                {
                    "station_id": station.get("station_id"),
                    "cooling_name": station.get("cooling_name"),
                    "energy_value": round(sum(values), 2),
                    "meter_count": len(ids),
                    "meters_with_data": sum(1 for value in values if value),
                }
            )

        total = sum(row["energy_value"] for row in station_rows)
        if not any(row["meters_with_data"] for row in station_rows):
            answer = (
                "### 结论\n\n"
                "冷站设备已映射到电表，但当前时间范围没有可用用电数据；"
                "这不是 0 kWh。\n\n"
                "### 口径\n\n"
                f"- 时间：{_date_label(start)} 至 {_date_label(end)}（{range_label}）\n"
                "- 数据源：Doris `dm_eo_e_meter_di`\n"
                "- 已映射电表："
                f"{len(all_ids)} 块；当前时间窗无返回。"
            )
            if tenant_facts:
                answer = self._tenant_prefix(tenant_facts) + answer
            return AgentResult(
                intent="cold_station_electricity",
                title=title,
                answer=answer,
                thinking=thinking,
                facts=self._metric_facts(
                    "cold_station_electricity",
                    question,
                    start,
                    end,
                    range_label,
                    tenant_facts,
                    source="doris.dm_eo_e_meter_di",
                    dataset=doris.dataset,
                    result={
                        "available": False,
                        "reason": "no_energy_rows",
                        "stations": station_rows,
                        "meter_count": len(all_ids),
                    },
                ),
            )

        table = "\n".join(
            f"| {row['cooling_name'] or '-'} | {_fmt_num(row['energy_value'])} | "
            f"{row['meters_with_data']}/{row['meter_count']} |"
            for row in station_rows
        )
        answer = (
            f"### 结论\n\n"
            f"{_date_label(start)} 至 {_date_label(end)}，冷站合计用电 "
            f"**{_fmt_num(total)} kWh**。\n\n"
            "| 冷站 | 用电量（kWh） | 有数据/已映射表计 |\n"
            "|---|---:|---:|\n"
            f"{table}\n\n"
            "### 口径\n\n"
            f"- 时间：{_date_label(start)} 至 {_date_label(end)}（{range_label}）\n"
            f"- 数据源：MySQL 冷站配置 + Doris `dm_eo_e_meter_di`\n"
            f"- 过滤：tenant_id={mysql.tenant_id}、project_id={self.config.doris_project_id}、deleted=0；"
            "电表 `energy_type='electric'`、`meter_status=2`\n"
            "- 冷站用电只统计映射到冷站设备上的电表，不用租户总用电替代。\n"
        )
        if tenant_facts:
            answer = self._tenant_prefix(tenant_facts) + answer
        return AgentResult(
            intent="cold_station_electricity",
            title=title,
            answer=answer,
            thinking=thinking,
            facts=self._metric_facts(
                "cold_station_electricity",
                question,
                start,
                end,
                range_label,
                tenant_facts,
                source="mysql.cold_station + doris.dm_eo_e_meter_di",
                dataset=doris.dataset,
                result={
                    "available": True,
                    "stations": station_rows,
                    "total": round(total, 2),
                    "meter_count": len(all_ids),
                },
            ),
        )

    @staticmethod
    def _data_quality_answer(
        start: date,
        end: date,
        range_label: str,
        tenant_facts: dict[str, Any] | None = None,
    ) -> str:
        scope = ""
        if tenant_facts:
            scope = (
                f"\n- 数据对象：{tenant_facts.get('name')}（{tenant_facts.get('tenant_id')} / "
                f"`{tenant_facts.get('doris_db')}`）"
            )
        return (
            "### 结论\n\n"
            "当前这次**未能完成“今日小时表是否有缺数”的查询**。"
            "可能是小时表模板执行失败，或当前数据源不可用；这里不做推测。\n\n"
            "### 现状\n\n"
            "- 已接入：日表 `dm_eo_e_meter_di` 的总用电、趋势、表计排名；\n"
            "- 已接入：小时表 `dm_eo_e_meter_hi` 的单日整点覆盖检查；\n"
            "- 已接入：设备室温、控制任务、告警；\n"
            "- 未接入：跨日补数、采集值准确性、采集任务状态。\n\n"
            "### 口径与边界\n\n"
            f"- 时间范围：{_fmt(start)} 至 {_fmt(end)}（{range_label}）{scope}\n"
            "- 日表 Top 10 用电排名不是小时表的完整性检查；用排名替代缺数判断会掩盖问题，"
            "因此这里明确返回“无法判断”，不做推测。\n\n"
            "### 你可以这样问\n\n"
            "- 南网能源昨天总用电多少？\n"
            "- 南网能源近 7 天用电趋势？\n"
            "- 南网能源昨天控制成功率多少？"
        )

    @staticmethod
    def _hourly_quality_answer(
        coverage: dict[str, Any],
        start: date,
        range_label: str,
        tenant_facts: dict[str, Any] | None = None,
    ) -> str:
        rows = coverage.get("rows") or []
        by_hour: dict[int, dict[str, int]] = {}
        for row in rows:
            raw = row.get("data_time")
            try:
                value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            if value.date() != start:
                continue
            by_hour[value.hour] = {
                "row_count": int(row.get("row_count") or 0),
                "meter_count": int(row.get("meter_count") or 0),
            }

        source = coverage.get("source") or "dm_eo_e_meter_hi"
        scope = ""
        if tenant_facts:
            scope = (
                f"\n- 数据对象：{tenant_facts.get('name')}（{tenant_facts.get('tenant_id')} / "
                f"`{tenant_facts.get('doris_db')}`）"
            )
        if not by_hour:
            return (
                "### 结论\n\n"
                f"今日（{_fmt(start)}）小时表没有查到任何数据，**无法排除整表未采集或任务未跑**。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 时间：{_fmt(start)}（{range_label}）{scope}\n\n"
                "### 限制\n\n- 0 行既可能是全量缺失，也可能是当天任务尚未开始；"
                "需要结合采集任务状态进一步确认。"
            )

        now = datetime.now()
        # 当前小时还没结束，不能把“09:00 这一小时”直接算成缺数；
        # 只检查已经结束的整点（00:00 到上一小时）。
        if start == now.date():
            last_expected_hour = now.hour - 1
        else:
            last_expected_hour = 23
        expected_hours = list(range(0, last_expected_hour + 1))
        observed_hours = sorted(by_hour)
        base_meters = max(item["meter_count"] for item in by_hour.values())
        missing_hours = [hour for hour in expected_hours if hour not in by_hour]
        partial_hours = [
            (hour, item)
            for hour, item in sorted(by_hour.items())
            if hour <= last_expected_hour and base_meters and item["meter_count"] < base_meters
        ]
        short_rows = [
            (hour, item)
            for hour, item in sorted(by_hour.items())
            if hour <= last_expected_hour and base_meters and item["row_count"] < base_meters
        ]

        coverage_text = (
            f"覆盖 {observed_hours[0]:02d}:00–{observed_hours[-1]:02d}:00，"
            f"共 {len(observed_hours)} 个小时点"
        )
        if start == now.date():
            coverage_text += (
                f"；按当前 {now.strftime('%H:%M')}，只统计已结束的整点，"
                f"应有 {last_expected_hour + 1} 个"
            )
        else:
            coverage_text += "；该日应有 24 个小时点"

        evidence = "\n".join(
            f"| {hour:02d}:00 | {by_hour[hour]['row_count']} | {by_hour[hour]['meter_count']} |"
            for hour in observed_hours
        )
        issues: list[str] = []
        if missing_hours:
            issues.append("缺少小时点：" + "、".join(f"{hour:02d}:00" for hour in missing_hours))
        if partial_hours:
            issues.append(
                "表计不足：" + "、".join(f"{hour:02d}:00（{item['meter_count']}/{base_meters}）" for hour, item in partial_hours)
            )
        if short_rows:
            issues.append(
                "行数不足：" + "、".join(f"{hour:02d}:00（{item['row_count']}/{base_meters}）" for hour, item in short_rows)
            )

        if not expected_hours:
            conclusion = (
                f"当前 {now.strftime('%H:%M')} 还没有结束的整点，暂不能判断今日小时表是否缺数；"
                f"已观察到 {len(observed_hours)} 个小时点。"
            )
        elif issues:
            conclusion = (
                f"今日小时表**发现疑似缺数**。{coverage_text}；"
                f"基准表计数为 {base_meters}。\n\n" + "\n".join(f"- {item}" for item in issues)
            )
        else:
            conclusion = (
                f"今日小时表**未发现缺数**。{coverage_text}，每个已结束小时点均有 "
                f"{base_meters} 块表计数据。"
            )
            if start == now.date():
                conclusion += (
                    f" 当前 {now.strftime('%H:%M')} 所在的 {now.hour:02d}:00 这一小时"
                    "尚未结束，不计入缺失。"
                )

        return (
            f"### 结论\n\n{conclusion}\n\n"
            f"### 口径\n\n- 来源：`{source}`\n"
            f"- 时间：{_fmt(start)}（{range_label}）{scope}\n"
            "- 判定：以当天应到小时点为基准，检查每个小时点的行数和去重表计数；"
            "表计基准取当天各小时的最大表计数。\n\n"
            "### 证据\n\n| 小时 | 行数 | 表计数 |\n|---|---:|---:|\n"
            f"{evidence}\n\n"
            "### 限制\n\n- 只能发现小时点/表计行缺失，不能判断采集值是否准确；"
            "表计新增、停用或采集延迟需要结合任务状态复核。"
        )

    @staticmethod
    def _memory_answer(context: ConversationContext, question: str) -> str:
        if not context.previous_question:
            return (
                "### 会话记忆\n\n"
                "这是本会话里我收到的第一条问题，还没有上一轮内容可以引用。"
            )
        current = question.replace(" ", "")
        wants_reply = any(word in current for word in ("回答", "回复", "你说"))
        wants_list = any(word in current for word in ("聊了什么", "问过什么", "对话历史"))
        if wants_reply and context.assistant_turns:
            last = context.assistant_turns[-1].strip()
            if len(last) > 1200:
                last = last[:1200].rstrip() + "…"
            return f"### 会话记忆\n\n上一轮我的回答是：\n\n{last}"
        if wants_list:
            recent = context.user_turns[-6:]
            lines = "\n".join(f"{index}. {text}" for index, text in enumerate(recent, 1))
            return f"### 会话记忆\n\n本次会话你问过：\n\n{lines}"
        return (
            "### 会话记忆\n\n"
            f"上一轮你问的是：**{context.previous_question}**\n\n"
            "你可以继续追问，例如“那今天呢”“那荣耀呢”“那 37 层呢”。"
        )

    def _tenant_query_blocked(self, tenant: Tenant, reason: str) -> str:
        name = tenant.name or tenant.code
        lines = [
            "### 已识别租户",
            "",
            f"- 租户：**{name}**",
            f"- 编码：`{tenant.code}`",
            f"- 租户 ID：{tenant.tenant_id}",
            f"- Doris 库：`{tenant.doris_db}`",
            "",
            "### 为什么现在不能查",
            "",
        ]
        if reason == "isolation":
            lines.extend(
                [
                    f"当前会话绑定的是租户 **{self.config.doris_tenant_id} / "
                    f"`{self.config.doris_db}`**，不是荣耀。租户目录只说明“有哪些租户/库”，"
                    "不代表可以跨租户取数；按租户隔离要求，不能仅凭自然语言把查询切到 "
                    f"`{tenant.doris_db}`。",
                    "",
                    "如果要支持运营人员按租户查询，需要先由网关/SSO 完成租户身份校验，"
                    "再在受控环境开启 `ALLOW_TENANT_QUERY=true`；这个开关不是鉴权，"
                    "不能直接暴露给普通租户用户。",
                ]
            )
        else:
            lines.extend(
                [
                    f"已识别荣耀，但当前 `DATA_PROVIDER=mock`，只有南网能源的演示数据，"
                    f"没有 `{tenant.doris_db}` 的真实读数。",
                    "",
                    "要返回荣耀真实用电，需要把数据源切到 `DATA_PROVIDER=archery`，"
                    "并通过 Archery 完成只读授权；跨租户仍需网关/SSO 校验。",
                ]
            )
        return "\n".join(lines)

    def _unknown_tenant_answer(self) -> str:
        names = "、".join(
            f"{row['name']}（{row['code']}）"
            for row in tenant_rows()
            if row.get("name")
        )
        return "\n".join(
            [
                "### 没有识别到具体租户",
                "",
                "我看到了“租户”，但没有匹配到租户目录中的名称或编码。",
                "请使用租户名称或编码，例如：荣耀（rongyao）、南网能源（nwdy）。",
                "",
                f"当前已提供名称的租户：{names}。",
                "",
                "也可以先问：“目前支持哪些租户的数据？”",
            ]
        )

    def _tenant_detail_answer(self, tenant: Tenant) -> str:
        name = tenant.name or tenant.code
        return "\n".join(
            [
                "### 已识别租户",
                "",
                f"- 租户：**{name}**",
                f"- 编码：`{tenant.code}`",
                f"- 租户 ID：{tenant.tenant_id}",
                f"- MySQL 库：`{tenant.mysql_db}`",
                f"- Doris 库：`{tenant.doris_db}`",
                "",
                "可以继续问：“荣耀今天用电多少？”或“荣耀近 7 天用电趋势？”",
            ]
        )

    def _cold_station_answer(
        self,
        rows: list[dict[str, Any]],
        *,
        tenant_facts: dict[str, Any] | None,
        database: str,
    ) -> str:
        tenant = tenant_facts or {}
        name = tenant.get("name") or "当前租户"
        tenant_id = tenant.get("tenant_id") or self.config.doris_tenant_id
        source = f"{database}.eo_energy_saving_cooling_base_configs"
        lines: list[str] = []

        if not rows:
            lines += [
                "### 结论",
                "",
                f"**{name}当前没有配置冷站。** MySQL 冷站配置表 "
                f"`{source}` 中，`tenant_id = {tenant_id}`、`project_id = "
                f"{self.config.doris_project_id}`、`deleted = 0` 的记录为 **0 条**。",
                "",
                "### 查询口径",
                "",
                f"- 数据源：`{source}`",
                f"- 过滤：`tenant_id = {tenant_id}`、`project_id = "
                f"{self.config.doris_project_id}`、`deleted = 0`",
                "- 空结果表示当前租户没有有效冷站配置，不能据此说“冷站停运”或“冷站不存在设备”。",
            ]
        else:
            type_map = {1: "冷源系统", 2: "集中供冷"}
            lines += [
                "### 结论",
                "",
                f"**{name}共配置 {len(rows)} 个冷站。**",
                "",
                "### 冷站清单",
                "",
                "| 冷站名称 | cold_site_id | 空间编码 | 日历编码 | 冷站类型 | 创建时间 |",
                "|---|---|---|---|---|---|",
            ]
            for row in rows:
                raw_type = row.get("cold_type")
                try:
                    type_text = type_map.get(int(raw_type), f"{raw_type}（源表注释未覆盖）")
                except (TypeError, ValueError):
                    type_text = str(raw_type or "")
                lines.append(
                    "| {name} | `{site}` | `{space}` | `{calendar}` | {type_text} | {created} |".format(
                        name=str(row.get("cooling_name") or "").replace("|", "\\|"),
                        site=str(row.get("cold_site_id") or ""),
                        space=str(row.get("space_code") or ""),
                        calendar=str(row.get("calendar_code") or ""),
                        type_text=type_text,
                        created=str(row.get("create_time") or ""),
                    )
                )
            lines += [
                "",
                "### 查询口径",
                "",
                f"- 数据源：`{source}`",
                f"- 过滤：`tenant_id = {tenant_id}`、`project_id = "
                f"{self.config.doris_project_id}`、`deleted = 0`",
                "- `cold_site_id` 是冷站业务 ID；设备配置通过 `application_id` 关联，"
                "点位配置通过 `site_id` 关联。",
            ]

        lines += [
            "",
            "### 相关字段",
            "",
            "| 字段 | 含义 |",
            "|---|---|",
            "| `cooling_name` | 冷站名称 |",
            "| `cold_site_id` | 冷站业务 ID |",
            "| `space_code` | 所属空间编码，可能是逗号分隔的路径 |",
            "| `calendar_code` | 日历编码；与 `eo_calendar.calendar_id` 不一定同值，需业务确认 |",
            "| `cold_type` | 源表注释：1=冷源系统，2=集中供冷；出现 3 时注释未定义 |",
            "| `application_id` | 关联 `eo_applications.id`，对应冷站应用/菜单 |",
            "| `deleted` | 逻辑删除，0=有效 |",
            "",
            "如果要继续下钻，可以问：“这个冷站配置了哪些设备/点位？”或"
            "“冷站 A 的制冷量/COP 配置是多少？”。",
        ]
        return "\n".join(lines)

    def _title(self, question: str) -> str:
        clean = re.sub(r"\s+", " ", question).strip("，,。?？:：")
        if not clean:
            return "能效分析"
        return clean[:24]

    def _provider_for_tenant(self, tenant: Tenant | None) -> EnergyProvider | None:
        if not tenant or tenant.tenant_id == self.config.doris_tenant_id:
            return self.provider
        if not self.config.allow_tenant_query:
            return None
        return get_tenant_provider(tenant, self.config)

    @staticmethod
    def _has_any(text: str, words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    def _should_use_tool_loop(
        self,
        question: str,
        plan: QuestionPlan,
        intent: str,
    ) -> bool:
        """固定模板只覆盖“明确命中口径”的问题；额外维度交给工具循环。

        典型需要交给循环的情况：
        - 按楼层/空间/设备分组或排名（模板只有表计维度）；
        - 两个对象对比；
        - 告警/控制问“类型、哪些设备、明细”；
        - 没有任何指标命中（plan.metric 为空）但看起来是数据问题。
        """
        if intent in ("memory", "knowledge", "tenants", "capability"):
            return False
        text = question.replace(" ", "")
        # SCOP/系统能效必须走冷站专项指标，不能落到冷量边界模板。
        if "scop" in text.lower():
            return True
        # 冷站设备类型/分类/点位不是冷站清单，走调查循环查设备配置。
        if intent == "cold_station" and any(
            word in text for word in ("设备", "类型", "分类", "点位")
        ):
            return True
        if intent in ("unknown", "mysql"):
            return should_investigate(question)
        if self._has_any(
            text,
            (
                "对比",
                "比较",
                "相比",
                "谁高",
                "谁低",
                "谁多",
                "谁少",
                "哪个高",
                "哪个低",
                "哪层高",
                "哪层低",
                "差多少",
                "高多少",
                "低多少",
                "vs",
            ),
        ):
            # 模板只能算单个对象/总量；两个对象谁高谁低必须走调查。
            return True
        key = plan.key
        if not key:
            return should_investigate(question)
        if intent == "cold_station":
            return key not in (
                "cold_station_config",
                "cold_station_electricity",
                "cold_station_cooling",
            )
        if key == "energy_ranking":
            return self._has_any(
                text, ("楼层", "楼", "空间", "房间", "区域", "对比", "比较", "占比", "比例")
            )
        if key == "energy_total":
            return self._has_any(
                text,
                (
                    "对比",
                    "比较",
                    "分别",
                    "占比",
                    "比例",
                    "排名",
                    "最多",
                    "楼层",
                    "空间",
                    "房间",
                    "区域",
                    "设备",
                    "表计",
                ),
            )
        if key == "energy_trend":
            return self._has_any(text, ("对比", "比较", "分别", "占比"))
        if key in ("room_temperature", "room_temp"):
            return self._has_any(
                text,
                (
                    "几块",
                    "几台",
                    "数量",
                    "平均",
                    "最高",
                    "最低",
                    "对比",
                    "比较",
                    "分别",
                    "趋势",
                    "异常",
                    "哪些",
                    "一直",
                    "持续",
                ),
            )
        if key == "control_success":
            return self._has_any(
                text, ("哪些", "明细", "原因", "设备", "类型", "分布", "分别", "楼层")
            )
        if key == "alarm_stats":
            return self._has_any(
                text, ("类型", "哪些", "设备", "明细", "原因", "分布", "分别", "楼层")
            )
        return False

    @staticmethod
    def _needs_escalation(intent: str, facts: dict[str, Any]) -> bool:
        """固定模板返回空/不可用/报错时，是否值得换工具再查一次。"""
        if facts.get("error"):
            return True
        result = facts.get("result")
        if not isinstance(result, dict):
            return False
        if intent in ("total", "trend", "ranking"):
            return not (result.get("rows") or result.get("value"))
        if intent == "room_temp":
            # 有设备上报但读数为 0：这是数据质量问题，模板已经能解释清楚，
            # 不需要再花几轮去查点位表。
            return result.get("available") is False and not result.get("invalid_devices")
        if intent in ("control", "alarm"):
            return result.get("available") is False
        return False

    @staticmethod
    def _is_unavailable(intent: str, facts: dict[str, Any]) -> bool:
        """数据不可用时禁止交给模型润色，避免“空表”被写成“0 条”。"""
        result = facts.get("result")
        if not isinstance(result, dict):
            return False
        if intent in ("room_temp", "control", "alarm"):
            return result.get("available") is False
        return False

    async def _try_investigation(
        self,
        question: str,
        intent: str,
        requested_tenant: Tenant | None,
        tenant_facts: dict[str, Any] | None,
        context: ConversationContext,
        *,
        progress: Any = None,
        force: bool = False,
        seed_facts: dict[str, Any] | None = None,
    ) -> Any | None:
        if not self.config.agent_tool_loop and not force:
            return None
        if intent == "unknown" and not should_investigate(question):
            return None
        if not force and intent not in ("unknown", "data_quality", "mysql"):
            return None

        sources = []
        query_provider = self._provider_for_tenant(requested_tenant)
        if query_provider is not None:
            sources.append(self.investigator.doris_source(query_provider))
        mysql_provider = self._mysql_provider_for_tenant(requested_tenant)
        if mysql_provider is not None:
            sources.append(self.investigator.mysql_source(mysql_provider))
        if not sources:
            return None

        default_source = sources[0].key
        if intent == "mysql" or self._prefers_mysql(question):
            if any(source.key == "mysql" for source in sources):
                default_source = "mysql"
        if progress is not None:
            await progress("拆解问题：确定指标、对象、时间范围和需要的维度")
        investigation = await self.investigator.investigate_sources(
            question,
            sources,
            default_source=default_source,
            tenant_facts=tenant_facts,
            context_facts=context.as_facts() if context.has_context else None,
            progress=progress,
            seed_facts=seed_facts,
        )
        if investigation is None:
            return None
        if progress is not None:
            await progress("汇总查询结果并组织结论")
        return investigation

    @staticmethod
    def _prefers_mysql(question: str) -> bool:
        text = question.replace(" ", "").lower()
        if any(
            word in text
            for word in (
                "mysql",
                "业务库",
                "关系库",
                "设备台账",
                "设备档案",
                "表计档案",
                "工单",
            )
        ):
            return True
        if any(word in text for word in ("设备", "表计", "控制任务", "告警")):
            return not any(
                word in text
                for word in ("总用电", "用电量", "耗电", "趋势", "排名", "室温", "温度")
            )
        return False

    def _mysql_provider_for_tenant(self, tenant: Tenant | None) -> MySQLArcheryProvider | None:
        if (
            tenant
            and tenant.tenant_id != self.config.doris_tenant_id
            and not self.config.allow_tenant_query
        ):
            return None
        return get_mysql_provider(tenant, self.config)

    def _investigation_result(
        self,
        intent: str,
        title: str,
        question: str,
        investigation: Any,
        context: ConversationContext,
        thinking_enabled: bool,
    ) -> AgentResult:
        facts = dict(investigation.facts)
        facts["intent"] = intent
        facts["question"] = question
        if context.has_context:
            facts["conversation"] = context.as_facts()
        thinking = (
            [
                "识别数据源与问题类型",
                "读取 Doris/MySQL 表结构",
                "生成只读 SQL",
                "执行并观察结果",
                "必要时修正查询",
                "组织结论",
            ]
            if thinking_enabled
            else []
        )
        return AgentResult(
            intent=intent,
            title=title,
            answer=investigation.answer,
            thinking=thinking,
            facts=facts,
        )

    async def _collect(
        self,
        intent: str,
        question: str,
        start: date,
        end: date,
        range_label: str,
        provider: EnergyProvider | None = None,
        room_keyword: str | None = None,
    ) -> dict[str, Any]:
        active = provider or self.provider
        base = {
            "intent": intent,
            "question": question,
            "dataset": active.dataset,
            "mode": active.mode,
            "time_range": {"start": _fmt(start), "end": _fmt(end), "label": range_label},
        }
        if intent == "tenants":
            return {
                **base,
                "result": {
                    "tenants": tenant_rows(),
                    "source": "energyOperation.eo_tenant_data_sources",
                },
            }
        if intent == "total":
            total = await active.energy_total(start, end)
            if start == end == self.today:
                total["note"] = (
                    "Doris 日表以 T+1 为主；查询当天时，当日汇总可能尚未生成或不完整，"
                    "不能把当前值当作全天最终用电。"
                )
            return {**base, "result": total}
        if intent == "trend":
            rows = await active.energy_trend(start, end)
            total = round(sum(float(row["energy_value"]) for row in rows), 2)
            values = [float(row["energy_value"]) for row in rows] or [0]
            return {
                **base,
                "result": {
                    "rows": rows[-31:],
                    "total": total,
                    "days": len(rows),
                    "max": max(values),
                    "min": min(values),
                    "average": round(sum(values) / len(values), 2),
                    "unit": "kWh",
                    "source": "dm_eo_e_meter_di",
                },
            }
        if intent == "ranking":
            rows = await active.meter_ranking(start, end, 10)
            total = sum(float(row["energy_value"]) for row in rows)
            return {
                **base,
                "result": {
                    "rows": rows,
                    "top_total": round(total, 2),
                    "unit": "kWh",
                    "source": "dm_eo_e_meter_di",
                    "note": "排名按表计汇总；份额仅在这 10 块表内计算，不代表全场占比。",
                },
            }
        if intent == "room_temp":
            keyword = room_keyword or extract_room_keyword(question)
            room = await active.room_temp(keyword)
            return {**base, "keyword": keyword, "result": room}
        if intent == "control":
            control = await active.control_stats(start, end)
            return {**base, "result": control}
        if intent == "alarm":
            alarm = await active.alarm_stats(start, end)
            return {**base, "result": alarm}
        return {**base, "result": active.capability()}

    @staticmethod
    def _strip_limitations(answer: str) -> str:
        """模型偶尔会自行编“限制”段；统一由本地模板追加，避免出现 facts 之类内部词。"""
        match = re.search(r"(?m)^#{2,4}\s*(?:\d+[.、]\s*)?限制\s*$", answer)
        if match:
            return answer[: match.start()].rstrip()
        return answer.strip()

    def _limitations(self, intent: str, facts: dict[str, Any]) -> str:
        result = facts.get("result") or {}
        tr = facts.get("time_range", {})
        notes: list[str] = []
        if facts.get("mode") == "mock":
            notes.append("当前为演示数据（mock），数字仅用于展示量级，不代表真实生产读数。")

        tenant = facts.get("tenant")
        if tenant and intent not in ("tenants", "capability"):
            notes.append(
                f"数据对象：{tenant.get('name')}（{tenant.get('tenant_id')} / "
                f"`{tenant.get('doris_db')}`）。"
            )

        if intent == "total":
            if result.get("note"):
                notes.append(str(result["note"]))
            active = result.get("active_meters")
            total = result.get("total_meters")
            if active and total:
                notes.append(f"{active}/{total} 块表有数据，未覆盖全部表计。")
            notes.append("不支持同比、节能率、房间级能耗或整栋建筑总能耗。")
        elif intent == "trend":
            notes.append(f"仅覆盖 {tr.get('start')} 至 {tr.get('end')} 的日粒度数据。")
            notes.append("不支持同比/环比、气象归一化或分时电价分析。")
        elif intent == "ranking":
            notes.append("仅展示 Top 10；份额只在这 10 块表内计算，不代表全场占比。")
            notes.append("表计与房间没有结构化映射，不能下钻到房间。")
        elif intent == "room_temp":
            notes.append("只代表最近一次上报，不代表全天舒适度；掉线设备可能不在结果中。")
        elif intent == "control":
            notes.append("状态码 2=成功、3=失败；待执行/执行中/取消/抢占未计入成功率。")
            notes.append("控制任务表按 create_time 过滤；历史累计值不能直接当区间值。")
        elif intent == "alarm":
            if not result.get("available"):
                notes.append(f"告警不可用：{result.get('reason', '告警表为空')}。")
                notes.append("空结果不等于“没有告警”，只表示当前租户/时间窗没有接入数据。")
            else:
                notes.append("告警级别/状态按源表字典解释，未做业务归并。")
        elif intent == "capability":
            notes.append("能力边界随租户接入情况变化，接入新表后需要更新模板。")
        elif intent == "memory":
            notes.append("记忆来自本次会话携带的历史消息，不是跨会话的长期用户画像。")
        else:
            notes.append("已知指标支持总用电、趋势、表计排名、室温、控制统计和告警统计；"
                         "其他数据问题可走开放调查。")

        notes.append("所有查询均为只读；已知指标用固定模板，开放调查的模型 SQL 会经过只读/租户/限行校验。")
        return "\n".join(f"- {note}" for note in notes)

    async def _narrate(self, question: str, facts: dict[str, Any]) -> str:
        if not self.llm.configured:
            return ""
        system = (
            "你是企业能效分析助手。你只能使用 <facts> 中给出的数字、表名和结论，"
            "不得编造、不得改变数字、不得补充未提供的维度。\n"
            "回答使用中文 Markdown，只写三节：\n"
            "1. 结论：1-3 句，先给关键数字；\n"
            "2. 口径：时间范围、数据集、来源表、单位；\n"
            "3. 证据：列出关键数字或表格（表格最多 10 行）。\n"
            "不要写“限制”一节，系统会自动追加。\n"
            "表计数量用“块”，金额/电量保留两位小数并使用千分位。\n"
            "不要出现“facts”这个词，改用“当前数据”。\n"
            "如果 facts.conversation 存在，说明这是同一会话的追问：要承接上一轮的指标、"
            "时间、租户或楼层上下文，但所有数字必须以本轮 facts.result 为准，不能复用历史数字。\n"
            "不要输出 SQL，不要输出思考过程，不要使用给定数据以外的内容。"
        )
        user = (
            f"用户问题：{question}\n\n"
            f"<facts>\n{json.dumps(facts, ensure_ascii=False, indent=2, default=str)}\n</facts>\n\n"
            "请按上述结构回答。"
        )
        try:
            return await self.llm.complete(system, user, max_tokens=1400)
        except LLMError:
            return ""

    def _fallback_answer(self, intent: str, facts: dict[str, Any]) -> str:
        result = facts.get("result") or {}
        tr = facts.get("time_range", {})
        source = result.get("source") or facts.get("dataset", "")
        if "error" in facts:
            return f"查询失败：{facts['error']}"

        if intent == "total":
            return (
                f"### 结论\n\n{tr.get('label', '')}（{tr.get('start')} 至 {tr.get('end')}）"
                f"总用电量为 **{_fmt_num(result.get('value'))} kWh**，"
                f"{result.get('active_meters', '-')} 块表有数据。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 单位：kWh\n- 日粒度，按租户/项目过滤\n\n"
                f"### 证据\n\n- 天数：{result.get('days', '-')}\n"
                f"- 日均：{_fmt_num(result.get('daily_average'))} kWh\n"
                f"- 表计：{result.get('active_meters', '-')} / {result.get('total_meters', '-')}\n\n"
                "### 限制\n\n- 不能据此计算同比、节能率或房间级能耗。"
            )
        if intent == "trend":
            rows = result.get("rows") or []
            table = "\n".join(
                f"| {row.get('date')} | {_fmt_num(row.get('energy_value'))} | {row.get('active_meters', '-')} |"
                for row in rows[-10:]
            )
            return (
                f"### 结论\n\n{tr.get('label', '')}共 {result.get('days', '-')} 天，"
                f"总用电 **{_fmt_num(result.get('total'))} kWh**，日均 "
                f"**{_fmt_num(result.get('average'))} kWh**。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 单位：kWh\n- 日粒度\n\n"
                "### 证据\n\n| 日期 | 用电量(kWh) | 有效表计 |\n|---|---:|---:|\n"
                f"{table}\n\n"
                "### 限制\n\n- 仅展示最近 10 天；完整数据受查询窗口限制。\n"
                "- 不能做同比/环比，不能做气象归一化。"
            )
        if intent == "ranking":
            rows = result.get("rows") or []
            table = "\n".join(
                f"| {row.get('rank')} | {row.get('meter_name')} | {_fmt_num(row.get('energy_value'))} |"
                for row in rows
            )
            return (
                "### 结论\n\n按表计汇总，用电量最高的表计如下（Top 10）。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 单位：kWh\n- 时间：{tr.get('start')} 至 {tr.get('end')}\n\n"
                "### 证据\n\n| 排名 | 表计 | 用电量(kWh) |\n|---:|---|---:|\n"
                f"{table}\n\n"
                "### 限制\n\n- 份额只在这 10 块表内计算，不代表全场占比。\n"
                "- 表计与房间没有结构化映射，不能下钻到房间。"
            )
        if intent == "room_temp":
            devices = result.get("devices") or []
            invalid = result.get("invalid_devices") or []
            if not devices:
                reason = result.get("reason") or "没有查到匹配设备的室温数据。"
                lines = [
                    "### 结论\n\n" + reason + "。\n\n",
                    f"### 口径\n\n- 来源：`{source}`\n"
                    "- 只统计最近 2 小时有点位数据的设备\n\n",
                ]
                if invalid:
                    lines.append("### 证据\n\n| 设备 | 原始室温 | 最近上报 |\n|---|---:|---|\n")
                    lines.extend(
                        f"| {row.get('device_name')} | {row.get('raw_room_temp', '-')} | "
                        f"{row.get('last_seen', '-')} |"
                        for row in invalid[:10]
                    )
                    lines.append("\n")
                lines.append(
                    "### 建议\n\n- 换一个设备名，或确认设备是否有点位上报；"
                    "如果读数持续为 0，需要排查采集链路而不是当作真实室温。\n"
                )
                return "".join(lines)
            table = "\n".join(
                f"| {row.get('device_name')} | {row.get('room_temp', '-')} | "
                f"{row.get('setpoint', '-')} | {row.get('last_seen', '-')} |"
                for row in devices
            )
            return (
                "### 结论\n\n查到以下设备的室温与设定温度。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 单位：℃\n- 最近点位\n\n"
                "### 证据\n\n| 设备 | 室温(℃) | 设定(℃) | 最近上报 |\n|---|---:|---:|---|\n"
                f"{table}\n\n"
                "### 限制\n\n- 只代表最近一次上报，不代表全天舒适度。"
            )
        if intent == "control":
            if not result.get("available", True):
                return f"### 结论\n\n控制任务表在时间窗内没有数据。\n\n来源：`{source}`"
            return (
                f"### 结论\n\n{tr.get('label', '')}控制任务共 **{result.get('total', '-')}** 条，"
                f"成功 **{result.get('success', '-')}** 条，失败 **{result.get('failed', '-')}** 条，"
                f"成功率 **{result.get('success_rate', '-')}%**。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 时间：{result.get('window', '-')}\n"
                "- 成功=status 2，失败=status 3，其余为待执行/执行中/取消/抢占\n\n"
                "### 限制\n\n- 控制任务是累计快照时，需要按 create_time 过滤；当前值仅作演示。"
            )
        if intent == "alarm":
            if not result.get("available"):
                return (
                    f"### 结论\n\n当前数据源查不到告警：{result.get('reason', '告警表为空')}。\n\n"
                    f"### 口径\n\n- 来源：`{source}`\n- 时间：{tr.get('start')} 至 {tr.get('end')}\n\n"
                    "### 限制\n\n- 不同租户的告警接入情况不同，不能把空结果当成“没有告警”。"
                )
            return (
                f"### 结论\n\n时间窗内共 **{result.get('count', '-')}** 条告警。\n\n"
                f"### 口径\n\n- 来源：`{source}`\n- 时间：{tr.get('start')} 至 {tr.get('end')}\n"
            )
        return CAPABILITY_TEXT


__all__ = ["AgentResult", "EnergyAgent", "detect_intent", "parse_time_range", "CAPABILITY_TEXT"]
