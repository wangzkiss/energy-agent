"""指标语义层。

这一层不执行 SQL，只解决三个问题：

1. 用户这句话问的是哪个**指标**；
2. 这个指标需要哪些**实体、时间、分析类型**；
3. 当前问题是否足够明确，还是应该先澄清。

Agent 负责把 ``QuestionPlan`` 交给对应的执行器。
这样“冷站”“冷站用电”“冷站用电异常”不会被压成同一个 intent。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 指标定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    name: str
    description: str
    source: str
    aliases: tuple[str, ...]
    analyses: tuple[str, ...]
    required_entities: tuple[str, ...]
    unit: str
    freshness: str
    tables: tuple[str, ...]
    joins: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    legacy_intent: str | None = None


@dataclass(frozen=True)
class QuestionPlan:
    metric: MetricDefinition | None
    analysis: str = "value"
    entity: str | None = None
    confidence: float = 0.0
    missing: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    reason: str = ""

    @property
    def key(self) -> str:
        return self.metric.key if self.metric else ""

    @property
    def needs_clarification(self) -> bool:
        return self.metric is None or bool(self.missing)


ENERGY_WORDS = (
    "用电",
    "电量",
    "电耗",
    "耗电",
    "能耗",
    "总用电",
    "用了多少电",
)
ANOMALY_WORDS = (
    "异常",
    "为什么",
    "原因",
    "分析",
    "偏高",
    "偏低",
    "突增",
    "突降",
    "正常吗",
    "是否正常",
)
CONFIG_WORDS = (
    "配置",
    "清单",
    "列表",
    "有哪些",
    "哪些",
    "多少个冷站",
    "几个冷站",
    "多少冷站",
    "冷站数量",
    "冷站个数",
)
COUNT_WORDS = ("多少", "数量", "几个", "几台", "几块", "总数", "count")
TREND_WORDS = ("趋势", "走势", "每天", "逐日", "曲线")
RANKING_WORDS = ("排名", "最多", "top", "哪些表", "哪块表", "排行")
CONTROL_WORDS = ("控制", "下发", "执行")
ALARM_WORDS = ("告警", "报警")
QUALITY_WORDS = ("缺数", "缺少", "缺失", "漏采", "完整性", "数据质量", "断点", "空值")
TEMP_WORDS = ("室温", "室内温度", "房间温度", "设定温度", "多少度")


METRICS: dict[str, MetricDefinition] = {
    "energy_total": MetricDefinition(
        key="energy_total",
        name="总用电量",
        description="指定时间范围内的表计用电汇总，单位 kWh。",
        source="doris",
        aliases=("总用电", "用电量", "用了多少电", "耗电", "电量", "用电"),
        analyses=("value",),
        required_entities=("tenant", "time"),
        unit="kWh",
        freshness="T+1",
        tables=("dm_eo_e_meter_di",),
        caveats=(
            "当天日表可能尚未生成完整，不能把当前值当全天最终值。",
            "只做总量，不回答同比、节能率或房间级能耗。",
        ),
        legacy_intent="total",
    ),
    "energy_trend": MetricDefinition(
        key="energy_trend",
        name="用电趋势",
        description="按日聚合的用电趋势。",
        source="doris",
        aliases=("用电趋势", "电量趋势", "每天用电", "逐日用电", "用电走势"),
        analyses=("trend",),
        required_entities=("tenant", "time"),
        unit="kWh",
        freshness="T+1",
        tables=("dm_eo_e_meter_di",),
        caveats=("只覆盖日粒度，不做同比/环比结论。",),
        legacy_intent="trend",
    ),
    "energy_ranking": MetricDefinition(
        key="energy_ranking",
        name="表计用电排名",
        description="按表计汇总用电量并取 Top 10。",
        source="doris",
        aliases=("表计排名", "用电排名", "哪些表最耗电", "哪块表最多", "表计排行"),
        analyses=("ranking",),
        required_entities=("tenant", "time"),
        unit="kWh",
        freshness="T+1",
        tables=("dm_eo_e_meter_di",),
        caveats=("份额只在这 10 块表内计算，不代表全场占比。",),
        legacy_intent="ranking",
    ),
    "energy_anomaly": MetricDefinition(
        key="energy_anomaly",
        name="用电异常",
        description="将指定时间段的用电量与前 7 天日均基线比较，判断偏高/偏低，不做因果归因。",
        source="doris",
        aliases=("用电异常", "电量异常", "异常用电", "为什么用电", "用电原因", "用电偏高", "用电偏低"),
        analyses=("anomaly", "value"),
        required_entities=("tenant", "time", "baseline"),
        unit="kWh",
        freshness="T+1",
        tables=("dm_eo_e_meter_di",),
        caveats=(
            "异常判断是统计偏差，不等于故障或原因。",
            "做原因分析还需要告警、控制记录、设备状态和分项数据。",
        ),
        legacy_intent="total",
    ),
    "cold_station_config": MetricDefinition(
        key="cold_station_config",
        name="冷站配置",
        description="租户配置的冷站清单、cold_site_id、空间和日历。",
        source="mysql",
        aliases=("冷站配置", "配置了哪些冷站", "有哪些冷站", "冷站清单", "冷站列表"),
        analyses=("config",),
        required_entities=("tenant",),
        unit="",
        freshness="MySQL 实时",
        tables=("eo_energy_saving_cooling_base_configs",),
        caveats=("只代表配置，不代表冷站正在运行或有数据。",),
        legacy_intent="cold_station",
    ),
    "cold_station_electricity": MetricDefinition(
        key="cold_station_electricity",
        name="冷站用电",
        description="按冷站聚合其设备关联电表的用电量；需要冷站配置、设备配置和电表映射齐全。",
        source="multi",
        aliases=("冷站用电", "冷站耗电", "冷站电耗", "冷站用了多少电", "冷站电量"),
        analyses=("value", "trend", "anomaly"),
        required_entities=("tenant", "cold_station", "meter"),
        unit="kWh",
        freshness="Doris T+1",
        tables=(
            "eo_energy_saving_cooling_base_configs",
            "eo_energy_saving_cooling_device_configs",
            "eo_energy_meters",
            "dm_eo_e_meter_di",
        ),
        joins=(
            "base.application_id = device_config.application_id",
            "device_config.device_id = eo_energy_meters.device_id",
            "eo_energy_meters.id = dm_eo_e_meter_di.meter_id",
        ),
        caveats=(
            "冷量计（energy_type=cold）不能当电表使用。",
            "没有设备-电表映射时不能按冷站统计用电，不能回答 0 kWh。",
        ),
        legacy_intent="cold_station",
    ),
    "cold_station_cooling": MetricDefinition(
        key="cold_station_cooling",
        name="冷站冷量",
        description="按冷站聚合冷量表的读数或冷量汇总；当前先作为语义边界，不默认执行。",
        source="mysql",
        aliases=("冷站冷量", "冷站制冷量", "供冷量", "冷量"),
        analyses=("value", "trend"),
        required_entities=("tenant", "cold_station", "cold_meter"),
        unit="kWh",
        freshness="取决于读数表",
        tables=("eo_energy_meters", "eo_energy_meter_readings"),
        caveats=(
            "读数可能是累计值，不能直接 SUM 当区间冷量。",
            "当前没有验证过的冷量口径，遇到该问题应明确边界。",
        ),
        legacy_intent="cold_station",
    ),
    "device_count": MetricDefinition(
        key="device_count",
        name="设备数量",
        description="设备台账中未删除的设备数量。",
        source="mysql",
        aliases=("多少设备", "设备数量", "设备总数", "有几台设备", "设备有多少"),
        analyses=("count",),
        required_entities=("tenant",),
        unit="台",
        freshness="MySQL 实时",
        tables=("eo_devices",),
        caveats=("只统计 eo_devices 台账，不等于表计数量。",),
        legacy_intent="mysql",
    ),
    "meter_count": MetricDefinition(
        key="meter_count",
        name="表计数量",
        description="表计档案中未删除的表计数量。",
        source="mysql",
        aliases=("多少表计", "表计数量", "表计总数", "有几块表", "表有多少"),
        analyses=("count",),
        required_entities=("tenant",),
        unit="块",
        freshness="MySQL 实时",
        tables=("eo_energy_meters",),
        caveats=("表计数量不等于设备数量。",),
        legacy_intent="mysql",
    ),
    "asset_counts": MetricDefinition(
        key="asset_counts",
        name="设备与表计数量",
        description="同时统计设备台账和表计档案中的未删除数量。",
        source="mysql",
        aliases=("设备和表计数量", "设备及表计数量", "设备、表计数量", "多少设备和表计"),
        analyses=("count",),
        required_entities=("tenant",),
        unit="台/块",
        freshness="MySQL 实时",
        tables=("eo_devices", "eo_energy_meters"),
        caveats=("设备数量和表计数量是两类档案，不能互相替代。",),
        legacy_intent="mysql",
    ),
    "room_temperature": MetricDefinition(
        key="room_temperature",
        name="室温与设定温度",
        description="按设备名称/楼层匹配空调面板或温湿度设备，读取最近窗口内的室温和设定温度。",
        source="doris",
        aliases=("室温", "室内温度", "房间温度", "设定温度", "多少度"),
        analyses=("value", "compare", "trend"),
        required_entities=("tenant", "device"),
        unit="℃",
        freshness="近实时（默认 2 小时窗口）",
        tables=("dwi_eo_d_point_data", "dim_eo_d_device"),
        caveats=(
            "点位表没有 tenant_id，必须先通过设备台账按租户筛 device_id。",
            "0℃/负值按无效值剔除，不能当真实室温。",
            "只代表最近一次上报，不代表全天舒适度。",
        ),
        legacy_intent="room_temp",
    ),
    "control_success": MetricDefinition(
        key="control_success",
        name="控制成功率",
        description="控制任务中 status=2 成功、status=3 失败的成功率。",
        source="mysql",
        aliases=("控制成功率", "下发成功率", "控制成功", "执行成功率"),
        analyses=("rate",),
        required_entities=("tenant", "time"),
        unit="%",
        freshness="MySQL 实时",
        tables=("eo_device_control_task",),
        caveats=("待执行/执行中/取消/抢占未计入成功率。",),
        legacy_intent="control",
    ),
    "alarm_stats": MetricDefinition(
        key="alarm_stats",
        name="告警统计",
        description="按级别/状态统计告警；空表表示未接入，不等于 0 条告警。",
        source="mysql",
        aliases=("告警统计", "有多少告警", "告警数量", "报警统计"),
        analyses=("value",),
        required_entities=("tenant", "time"),
        unit="条",
        freshness="MySQL 实时",
        tables=("eo_alarms",),
        caveats=("空结果不等于没有告警，只表示当前租户/时间窗没有接入数据。",),
        legacy_intent="alarm",
    ),
    "data_quality": MetricDefinition(
        key="data_quality",
        name="小时表完整性",
        description="按整点统计小时表覆盖，判断缺失小时或表计不足。",
        source="doris",
        aliases=("缺数", "数据完整性", "数据质量", "小时表缺失", "漏采"),
        analyses=("value",),
        required_entities=("tenant", "time"),
        unit="小时",
        freshness="T+1",
        tables=("dm_eo_e_meter_hi",),
        caveats=("只判断已结束整点，不把当前未结束小时算成缺失。",),
        legacy_intent="data_quality",
    ),
}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return (text or "").replace(" ", "").strip()


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _analysis_for(text: str) -> str:
    if _has(text, ANOMALY_WORDS):
        return "root_cause" if ("为什么" in text or "原因" in text) else "anomaly"
    if _has(text, CONFIG_WORDS):
        return "config"
    if _has(text, RANKING_WORDS):
        return "ranking"
    if _has(text, TREND_WORDS):
        return "trend"
    if _has(text, COUNT_WORDS):
        return "count"
    return "value"


def _entity_for(text: str) -> str | None:
    if "冷站" in text or "冷源" in text or "供冷" in text:
        return "cold_station"
    if "表计" in text or "电表" in text:
        return "meter"
    if "设备" in text:
        return "device"
    if "工单" in text:
        return "ticket"
    if "空间" in text or "楼层" in text:
        return "space"
    return None


def _plan(
    metric: MetricDefinition | None,
    *,
    analysis: str,
    entity: str | None,
    confidence: float,
    reason: str,
    missing: tuple[str, ...] = (),
    candidates: tuple[str, ...] = (),
) -> QuestionPlan:
    return QuestionPlan(
        metric=metric,
        analysis=analysis,
        entity=entity,
        confidence=confidence,
        missing=missing,
        candidates=candidates,
        reason=reason,
    )


def plan_question(
    question: str,
    *,
    context_metric: str | None = None,
) -> QuestionPlan:
    """把自然语言问题解析成指标计划；无法确定时返回带 candidates 的澄清计划。"""
    text = _normalize(question)
    entity = _entity_for(text)
    analysis = _analysis_for(text)
    has_energy = _has(text, ENERGY_WORDS)
    has_anomaly = _has(text, ANOMALY_WORDS)

    # 冷站是当前最容易混淆的实体：配置、用电、冷量是三个不同指标。
    if entity == "cold_station":
        if _has(text, ("用电", "电耗", "耗电", "电量")):
            return _plan(
                METRICS["cold_station_electricity"],
                analysis="anomaly" if has_anomaly else "value",
                entity=entity,
                confidence=0.96,
                reason="问题包含冷站 + 用电，按冷站用电聚合处理。",
            )
        if _has(text, ("冷量", "制冷量", "供冷量")):
            return _plan(
                METRICS["cold_station_cooling"],
                analysis="value",
                entity=entity,
                confidence=0.9,
                reason="问题包含冷站 + 冷量，按冷站冷量口径处理。",
            )
        if _has(text, CONFIG_WORDS):
            return _plan(
                METRICS["cold_station_config"],
                analysis="config",
                entity=entity,
                confidence=0.95,
                reason="问题问冷站清单/配置。",
            )
        return _plan(
            None,
            analysis=analysis,
            entity=entity,
            confidence=0.35,
            reason="只识别到冷站，未识别是配置、用电还是冷量。",
            missing=("metric",),
            candidates=("cold_station_config", "cold_station_electricity", "cold_station_cooling"),
        )

    if has_anomaly and has_energy:
        return _plan(
            METRICS["energy_anomaly"],
            analysis="root_cause" if ("为什么" in text or "原因" in text) else "anomaly",
            entity=entity,
            confidence=0.95,
            reason="问题包含用电 + 异常/原因，需要与基线比较。",
        )

    if "设备" in text and "表计" in text and _has(text, COUNT_WORDS):
        return _plan(
            METRICS["asset_counts"],
            analysis="count",
            entity="device+meter",
            confidence=0.95,
            reason="问题同时问设备和表计数量，分别按两张档案表统计。",
        )

    if entity == "device":
        if _has(text, COUNT_WORDS) or "多少" in text:
            return _plan(
                METRICS["device_count"],
                analysis="count",
                entity=entity,
                confidence=0.95,
                reason="问题问设备数量，按 eo_devices 台账统计。",
            )
        return _plan(
            None,
            analysis=analysis,
            entity=entity,
            confidence=0.4,
            reason="只识别到设备，未识别数量、状态还是其它指标。",
            missing=("metric",),
            candidates=("device_count",),
        )

    if entity == "meter":
        if _has(text, COUNT_WORDS) or "多少" in text:
            return _plan(
                METRICS["meter_count"],
                analysis="count",
                entity=entity,
                confidence=0.95,
                reason="问题问表计数量，按 eo_energy_meters 统计。",
            )
        return _plan(
            None,
            analysis=analysis,
            entity=entity,
            confidence=0.4,
            reason="只识别到表计，未识别数量、读数还是状态。",
            missing=("metric",),
            candidates=("meter_count",),
        )

    if _has(text, CONTROL_WORDS) and _has(text, ("成功率", "成功", "失败")):
        return _plan(
            METRICS["control_success"],
            analysis="rate",
            entity="control_task",
            confidence=0.95,
            reason="问题问控制任务成功/失败。",
        )

    if _has(text, ALARM_WORDS):
        return _plan(
            METRICS["alarm_stats"],
            analysis="value",
            entity="alarm",
            confidence=0.9,
            reason="问题问告警统计。",
        )

    if _has(text, QUALITY_WORDS):
        return _plan(
            METRICS["data_quality"],
            analysis="value",
            entity="hourly_data",
            confidence=0.95,
            reason="问题问小时表缺数/完整性。",
        )

    if _has(text, TEMP_WORDS):
        return _plan(
            METRICS["room_temperature"],
            analysis="value",
            entity="device",
            confidence=0.9,
            reason="问题问室温/设定温度，按设备点位处理。",
        )

    if _has(text, RANKING_WORDS):
        return _plan(
            METRICS["energy_ranking"],
            analysis="ranking",
            entity="meter",
            confidence=0.9,
            reason="问题问表计/用电排名。",
        )

    if _has(text, TREND_WORDS) and has_energy:
        return _plan(
            METRICS["energy_trend"],
            analysis="trend",
            entity=entity,
            confidence=0.92,
            reason="问题问用电趋势。",
        )

    if has_energy:
        return _plan(
            METRICS["energy_total"],
            analysis="value",
            entity=entity,
            confidence=0.85,
            reason="问题问用电，按总用电处理。",
        )

    # 追问“那今天呢/那昨天呢/为什么”时，沿用上一轮指标，但分析类型可以变化。
    if context_metric and context_metric in METRICS:
        metric = METRICS[context_metric]
        follow_analysis = "anomaly" if has_anomaly else metric.analyses[0]
        return _plan(
            metric,
            analysis=follow_analysis,
            entity=entity,
            confidence=0.6,
            reason="没有新指标词，沿用上一轮指标。",
        )

    return _plan(
        None,
        analysis=analysis,
        entity=entity,
        confidence=0.0,
        reason="没有匹配到指标定义。",
    )


def metric_catalog() -> list[dict[str, Any]]:
    """给 /knowledge 或前端展示的指标字典，不包含 SQL 和敏感配置。"""
    return [
        {
            "key": metric.key,
            "name": metric.name,
            "description": metric.description,
            "source": metric.source,
            "unit": metric.unit,
            "freshness": metric.freshness,
            "required_entities": list(metric.required_entities),
            "tables": list(metric.tables),
            "caveats": list(metric.caveats),
        }
        for metric in METRICS.values()
    ]


def format_metric_context(metric: MetricDefinition | None) -> str:
    """把单个指标定义压缩成适合放进调查提示词的文本。"""
    if metric is None:
        return ""
    lines = [
        f"指标：{metric.name}（{metric.key}）",
        f"定义：{metric.description}",
        f"数据源：{metric.source}；单位：{metric.unit or '无'}；新鲜度：{metric.freshness}",
        f"相关表：{', '.join(metric.tables)}",
    ]
    if metric.joins:
        lines.append("关联：" + "；".join(metric.joins))
    if metric.caveats:
        lines.append("口径边界：" + "；".join(metric.caveats))
    return "\n".join(lines)


__all__ = [
    "METRICS",
    "MetricDefinition",
    "QuestionPlan",
    "format_metric_context",
    "metric_catalog",
    "plan_question",
]
