"""业务口径知识：回答“怎么算 / 是什么 / 流程 / 区别”这类定义问题。

knowledge.py 回答“查哪张表、哪些字段”；本模块回答“这个指标怎么定义、
公式是什么、本项目有没有数据、边界在哪里”。内容来自 2026-11-18/19 的
现网只读探查与代码核对，未验证的推断会明确标注。
"""
from __future__ import annotations

from typing import Any

CONCEPTS: dict[str, dict[str, Any]] = {
    "cold_station_cop": {
        "title": "冷站制冷量与 COP",
        "keywords": ("冷站的制冷量", "制冷量", "冷量怎么算", "scop", "cop", "能效比", "冷站冷量", "供冷量"),
        "definition": (
            "冷站制冷量是冷量计在统计期内提供的冷量（kWh，冷量单位，不是电量）；"
            "COP = 冷站总制冷量 ÷ 冷站总用电量，量纲 kWh/kWh，是冷站整体能效。"
        ),
        "formula": (
            "COP = Σ冷量(kWh) ÷ Σ冷站用电(kWh)；\n"
            "累计式冷量计：区间冷量 = 期末读数 − 期初读数（不能把读数直接 SUM）；\n"
            "只算主机时得到主机 COP，算整个冷站（主机+水泵+冷却塔）时得到系统 COP。"
        ),
        "data": (
            "按优先级查：① Doris algo_overview_kpi.system_scop（算法落库的现成 SCOP，"
            "多数租户为空）；② dm_eo_e_meter_di 里 energy_type='cold' 的冷量计 ÷ 同冷站电表；"
            "③ MySQL eo_cold_algo_schedule_hourly 的 SUM(cooling_load_kw)/SUM(total_power_kw)。"
            "南网能源（1026）冷站配置 0 条，算不出；亨通（1041）有 3 个冷站、2 块冷量计，"
            "空调冷源站 SCOP 约 4.7~4.9，工艺站电表读数≈0 不可算。"
        ),
        "caveats": (
            "冷量计 energy_type=cold，不能当电表求和。",
            "累计读数不能直接 SUM 成区间冷量。",
            "当前没有验证过的冷量口径，不能给具体 COP 数值。",
        ),
        "tables": (
            "eo_energy_saving_cooling_base_configs",
            "eo_energy_saving_cooling_device_configs",
            "eo_energy_meters",
            "eo_energy_meter_readings",
        ),
        "code": "semantic.py cold_station_cooling；MySQLArcheryProvider.cooling_stations",
    },
    "cold_station_power": {
        "title": "冷站用电口径",
        "keywords": ("冷站用电", "冷站耗电", "冷站电耗", "冷站用了多少电", "冷站电量"),
        "definition": (
            "冷站用电 = 冷站设备（冷机、冷冻泵、冷却泵、冷却塔等）关联电表的电量之和，"
            "不是按设备名称关键字猜出来的电量。"
        ),
        "formula": (
            "冷站用电 = Σ(设备→电表映射命中的表计 energy_value)；\n"
            "链路：冷站配置 application_id → 设备配置 device_id → 表计档案 device_id → 日表 meter_id。"
        ),
        "data": (
            "南网能源冷站配置 0 条、没有设备-电表映射，因此当前无法按冷站统计用电，"
            "不能回答 0 kWh。"
        ),
        "caveats": (
            "没有映射时不能把冷站用电当成 0。",
            "冷量计不能当电表。",
        ),
        "tables": (
            "eo_energy_saving_cooling_base_configs",
            "eo_energy_saving_cooling_device_configs",
            "eo_energy_meters",
            "dm_eo_e_meter_di",
        ),
        "code": "semantic.py cold_station_electricity；providers.cold_station_electricity",
    },
    "sub_metering": {
        "title": "分项计量（照明/空调/动力）",
        "keywords": ("分项计量", "分项", "照明", "动力", "空调怎么分", "分项能耗"),
        "definition": (
            "分项计量是把总用电按用途拆成照明插座、空调、动力、特殊用电等分项，"
            "通常依赖表计档案的分项字段（meter_category_id）或分项表计。"
        ),
        "formula": (
            "分项占比 = 分项电量 ÷ 分项合计电量；\n"
            "总分表关系用 meter_attribute：1 自用表 / 2 公摊表 / 3 总分表。"
        ),
        "data": (
            "本租户 908 块表计的 meter_category_id 全为空、meter_attribute 全为 1（自用表），"
            "没有结构化分项字段；按表计名称关键词近似归类只能覆盖约 25% 电量，动力类命中 0。"
            "因此不能给出严格的照明/空调/动力分项电量。"
        ),
        "caveats": (
            "按名称含“照明/空调”归类只是近似，不能当分项报表。",
            "未命中关键词的电量属于“未分类”，不能默认算动力。",
        ),
        "tables": ("dm_eo_e_meter_di", "eo_energy_meters", "eo_device_categories"),
        "code": "providers.meter_ranking 只有表计维度，没有分项模板",
    },
    "demand": {
        "title": "最大需量",
        "keywords": ("最大需量", "需量", "需量电费"),
        "definition": (
            "最大需量是结算周期内按固定窗口（国内通常 15 分钟）统计的平均有功功率的最大值，"
            "单位 kW；需量电费按最大需量或需量档位计收，和电量（kWh）不是一回事。"
        ),
        "formula": (
            "P15min = 15 分钟有功电量(kWh) ÷ 0.25h；\n"
            "最大需量 = MAX(P15min)；等价于取需量寄存器（MD）读数。"
        ),
        "data": (
            "库中没有需量表/需量字段；唯一功率点位 ActivePowerSum 只有 3 台设备有数据且近 7 天全为 0，"
            "无法计算本项目最大需量。"
        ),
        "caveats": (
            "不能用日电量 ÷ 24 冒充最大需量。",
            "没有 15 分钟粒度功率或需量寄存器时，只能回答“不可用”。",
        ),
        "tables": ("dwi_eo_d_point_data",),
        "code": "providers.device_readings（ActivePowerSum 仅少量设备有值）",
    },
    "tariff": {
        "title": "峰谷平电价与电费",
        "keywords": ("峰谷平", "电价", "电费怎么算", "电费", "分时电价", "尖峰"),
        "definition": (
            "电费 = Σ(各时段电量 × 对应时段单价) + 基本电费/需量电费 + 力调电费；"
            "峰谷平是按时段划分的目录电价，通常按月份和时段（HHmm）配置。"
        ),
        "formula": (
            "电费 = 尖峰电量×尖峰价 + 峰电量×峰价 + 平电量×平价 + 谷电量×谷价 (+基本电费)。"
        ),
        "data": (
            "MySQL eo_energy_prices 在租户 1026 下 0 行；Doris 日表 energy_expense 全为 0（未回填），"
            "宽表只有 segment=0 汇总。因此只能算电量，算不出真实电费。"
        ),
        "caveats": (
            "空电价表不等于免费电，不能编造单价。",
            "energy_expense=0 是未接入/未回填，不是实际电费。",
        ),
        "tables": ("eo_energy_prices", "dm_eo_e_meter_di", "dwr_eo_energy_meter_wide"),
        "code": "providers.energy_total 只返回 energy_value，不返回费用",
    },
    "intensity": {
        "title": "单位面积能耗",
        "keywords": ("单位面积", "面积能耗", "每平米", "kwh/m2", "建筑面积"),
        "definition": (
            "单位面积能耗（EUI）= 统计期总用电量 ÷ 建筑面积，单位 kWh/m²；"
            "分母必须与分子同范围、同期间。"
        ),
        "formula": "EUI = Σ电量(kWh) ÷ 建筑面积(m²)",
        "data": (
            "eo_projects.area 和 eo_spaces.area 字段存在，但本项目全部填成 1（占位值，114 个空间都是 1），"
            "不是真实建筑面积，因此算不出有意义的单位面积能耗（用 1 当分母等于直接用电量）。"
        ),
        "caveats": (
            "不能拿设备数量、楼层数或空间个数当面积。",
            "面积口径（建筑面积/空调面积/可租面积）未定义。",
        ),
        "tables": ("eo_projects", "eo_spaces", "dm_eo_e_meter_di"),
        "code": "无对应指标模板；只能在补齐面积后手算",
    },
    "carbon": {
        "title": "碳排放与折标煤",
        "keywords": ("碳排放", "碳排", "折标煤", "标煤", "碳", "排放因子"),
        "definition": (
            "碳排放 = 电量 × 电网排放因子；折标煤 = 电量 × 折标煤系数。"
            "两者都是电量乘系数的换算，系数由口径（年份、区域、当量值/等价值）决定。"
        ),
        "formula": (
            "CO₂(t) = 电量(kWh) × 排放因子(kgCO₂/kWh) ÷ 1000；\n"
            "标煤(tce) = 电量(kWh) × 折标煤系数(kgce/kWh) ÷ 1000。"
        ),
        "data": (
            "nwdy_dm / nwdy 中没有碳排放因子、折标煤系数或碳排字段，"
            "只能给通用参考值（电折标煤当量值 0.1229 kgce/kWh；电网因子约 0.5366 kgCO₂/kWh，"
            "均为公开标准参考值，不是本项目数据），不能给已核算的碳排量。"
        ),
        "caveats": (
            "参考系数必须注明来源和年份，不能当核算结果。",
            "等价值折标煤随供电煤耗变化，不能固定用旧值。",
        ),
        "tables": ("dm_eo_e_meter_di",),
        "code": "无碳排/折标实现",
    },
    "alarm_process": {
        "title": "告警等级与闭环",
        "keywords": ("告警等级", "告警闭环", "告警流程", "告警状态", "告警级别", "告警", "闭环"),
        "definition": (
            "告警按 alarm_level 分 4 级：0 紧急、1 严重、2 一般、3 警告；"
            "按 alarm_status 分 5 态：0 未处理、1 处理中、2 已处理、3 无需处理、4 误报。"
        ),
        "formula": (
            "闭环链路：告警产生 → 未处理(0) → 处理中(1) → 已处理(2)/无需处理(3)/误报(4)；\n"
            "需要人工介入时写 work_order_code，工单侧用 eo_ticket.alarm_id 回指。"
        ),
        "data": (
            "Doris dim_eo_alarms 近 7 天 0 条，MySQL eo_alarms 全量 0 条，"
            "无法给等级分布或闭环率；空表表示未接入/未产生，不等于 0 条告警。"
        ),
        "caveats": (
            "等级/状态是字典定义，不是本租户实际分布。",
            "审批人、超时升级、SLA 时长在表结构中没有定义，不能编流程细节。",
        ),
        "tables": ("dim_eo_alarms", "eo_alarms", "eo_ticket"),
        "code": "providers.alarm_stats；agent 告警模板",
    },
    "ticket_process": {
        "title": "工单闭环率与处理时长",
        "keywords": ("工单闭环", "闭环率", "处理时长", "工单", "sla"),
        "definition": (
            "工单闭环率 = 已闭环工单数 ÷ 工单总数；处理时长 = 结束时间 − 开始处理时间，"
            "通常同时看均值、中位数和 P90。"
        ),
        "formula": (
            "闭环率 = 已关闭数 ÷ 总数 × 100%；\n"
            "单均处理时长 = AVG(end_time − COALESCE(start_time, created_at))；\n"
            "SLA 达标率 = end_time ≤ deadline_time 的已闭环数 ÷ 已闭环数。"
        ),
        "data": (
            "nwdy.eo_ticket 在本租户 0 行（近一年与全量都是 0），"
            "没有分母，无法计算闭环率和处理时长；status 终态枚举也无法用数据验证。"
        ),
        "caveats": (
            "空工单表不等于没有工单，可能是未接入。",
            "响应时长（start−created）不能和处理时长混算。",
        ),
        "tables": ("eo_ticket",),
        "code": "无工单指标模板",
    },
    "meter_ratio": {
        "title": "表计倍率（CT/PT）",
        "keywords": ("倍率", "ct/pt", "变比", "表计倍率"),
        "definition": (
            "倍率 = CT 变比 × PT 变比；电能表读数差 × 倍率 = 一次侧电量。"
            "高压计量通常有倍率，低压直接计量倍率为 1。"
        ),
        "formula": "一次电量 = 表读数差 × rate；rate = CT × PT。",
        "data": (
            "MySQL eo_energy_meters.rate（decimal(12,2)，注释“倍率”）存在，"
            "但本租户 908 块表的 rate 全部 = 1.00，抄表明细 0 行，"
            "无法从数值上判断 Doris 日表是否已乘倍率。"
        ),
        "caveats": (
            "不能假设已乘或未乘，需用 rate≠1 的样本核对。",
            "没有独立 CT/PT 两列，只有一个 rate。",
        ),
        "tables": ("eo_energy_meters", "eo_energy_meter_readings", "dm_eo_e_meter_di"),
        "code": "代码中没有 rate/倍率换算实现",
    },
    "data_pipeline": {
        "title": "数据同步链路",
        "keywords": ("同步链路", "数据同步", "采集到", "etl", "数据链路", "怎么同步"),
        "definition": (
            "能确认的下游链路是：小时表 dm_eo_e_meter_hi → 日表 dm_eo_e_meter_di"
            "（按日 SUM、按表计去重）；采集端到小时表的上游不在本项目和本库中。"
        ),
        "formula": (
            "验证方式：同一天小时表 24 小时 SUM 与日表当日合计应逐位相等"
            "（2026-11-18 两边都是 4102.44 kWh）。"
        ),
        "data": (
            "2026-11-18 小时表 24×908 行、日表 758 行，合计一致；"
            "MySQL 抄表明细 0 行；项目代码只有查询侧，没有同步/ETL 实现，调度方式未知。"
        ),
        "caveats": (
            "908 → 758 的收敛规则未确认，不能断言过滤条件。",
            "不能把日表=小时表汇总推断成完整采集链路。",
        ),
        "tables": ("dm_eo_e_meter_hi", "dm_eo_e_meter_di", "eo_energy_meter_readings"),
        "code": "无写入/同步代码",
    },
    "space_hierarchy": {
        "title": "空间层级（楼栋/楼层/房间）",
        "keywords": ("空间层级", "楼栋", "楼层", "房间", "空间结构", "园区"),
        "definition": (
            "空间层级通常是 项目/园区 → 楼栋 → 楼层 → 房间；"
            "本项目的 space_code/space_name 是计量范围，不等于房间映射。"
        ),
        "formula": (
            "楼层只能从 meter_name 解析：先 ([0-9]+)[层楼]，再 栋([0-9]+)-，"
            "按栋拆分，覆盖率约 95%。"
        ),
        "data": (
            "dm_eo_e_meter_di.space_name 只有 A1座 等粗粒度；eo_spaces 有 L1~L4 层级"
            "（1/3/67/43 个），但面积全为 1；没有房间级映射。"
        ),
        "caveats": (
            "不能把表计名里的房间号当结构化房间。",
            "楼层排名是表计名解析结果，必须带覆盖率。",
        ),
        "tables": ("dm_eo_e_meter_di", "eo_spaces"),
        "code": "providers.energy_by_floor / energy_by_space",
    },
    "tenant_isolation": {
        "title": "租户与项目隔离",
        "keywords": ("租户隔离", "project_id", "项目隔离", "多租户"),
        "definition": (
            "每个租户一个 Doris 库（{code}_dm）和一个 MySQL 库（{code}）；"
            "查询强制 tenant_id + project_id，跨租户需要显式问租户名。"
        ),
        "formula": "租户 → 库名映射在 MySQL eo_tenant_data_sources（db_name/db_type）。",
        "data": (
            "当前租户 1026 南网能源，Doris nwdy_dm、MySQL nwdy、project_id=1。"
        ),
        "caveats": (
            "这是逻辑隔离，不是数据库账号隔离；生产应由网关/SSO 做身份校验。",
            "SQL Guard 会拒绝没有 tenant_id 的 Doris 查询。",
        ),
        "tables": ("eo_tenant_data_sources",),
        "code": "tenants.py / sqlguard.py / providers.py",
    },
    "anomaly_rule": {
        "title": "用电异常判定",
        "keywords": ("用电异常", "异常判定", "偏高", "偏低", "异常用电"),
        "definition": (
            "用电异常 = 当日电量与前 7 天日均基线比较，偏差超过阈值判偏高/偏低；"
            "它是统计偏差，不是故障诊断。"
        ),
        "formula": "偏差率 = (当日 − 前 7 天日均) ÷ 前 7 天日均。",
        "data": (
            "日表可算偏差；但没有告警、工单、设备状态和分项数据，不能做原因归因。"
        ),
        "caveats": ("不能把偏差直接说成设备故障。",),
        "tables": ("dm_eo_e_meter_di",),
        "code": "providers.energy_anomaly",
    },
    "data_quality_rule": {
        "title": "数据完整性判定",
        "keywords": ("完整性", "缺数", "漏采", "数据质量", "小时表"),
        "definition": (
            "日表完整性看当天应有表计是否都有记录；小时表看已结束的 24 个小时点是否齐全，"
            "当前小时不计缺失。"
        ),
        "formula": "缺失小时 = 应有小时点 − 实际有数据的小时点（排除当前小时）。",
        "data": (
            "小时表每小时约 908 块表；日表每天 758 块表；表数差异可能来自新增/拆除或有效电量过滤。"
        ),
        "caveats": ("行数少不等于采集故障，要结合表计变更。",),
        "tables": ("dm_eo_e_meter_hi", "dm_eo_e_meter_di"),
        "code": "providers.hourly_completeness",
    },
    "device_types": {
        "title": "设备类型",
        "keywords": ("设备类型", "设备分类", "有哪些设备", "设备台账"),
        "definition": (
            "设备类型来自 dim_eo_d_device.device_category_name（空调面板、冷机、水泵、风机、电表等），"
            "点位值按 dwi_eo_d_point_data 的 device_id + point_code 存放。"
        ),
        "formula": "按设备分类统计：GROUP BY device_category_name。",
        "data": (
            "本租户 MySQL eo_devices 共 1,808 台（deleted=0）；空调面板 209 台（183 普通 + 26 智能）；"
            "有点位数据的设备约 212 台。"
        ),
        "caveats": (
            "档案台数不等于在线台数或当天有数据的台数。",
            "设备分类名称不统一，跨租户汇总前要先对齐。",
        ),
        "tables": ("dim_eo_d_device", "eo_devices", "dwi_eo_d_point_data"),
        "code": "providers.device_readings",
    },
    "ranking_scope": {
        "title": "表计排名口径",
        "keywords": ("表计排名", "排名口径", "份额", "占比"),
        "definition": "表计排名是按 meter_id 汇总电量取 Top 10；份额只在这 10 块表内计算。",
        "formula": "份额 = 单表电量 ÷ Top10 合计电量，不是全场占比。",
        "data": "日表可算；但父子表同时存在时可能重复计量。",
        "caveats": ("Top10 份额不代表全场占比。", "总分表关系未校验，排名可能重复计算。"),
        "tables": ("dm_eo_e_meter_di",),
        "code": "providers.meter_ranking",
    },
    "cold_meter": {
        "title": "冷量计与电表的区别",
        "keywords": ("冷量计", "冷表", "冷量单位"),
        "definition": (
            "energy_type=cold 的是冷量计，单位 kWh 冷量；electric 才是电表。"
            "两者不能混在一起求和。"
        ),
        "formula": "按 energy_type 过滤后再聚合。",
        "data": "日表 energy_type 只有 electric；冷站相关表里有冷量计配置样例。",
        "caveats": ("冷量计读数不能当电量。",),
        "tables": ("dm_eo_e_meter_di", "eo_energy_meters"),
        "code": "semantic.py cold_station_cooling",
    },
    "time_window": {
        "title": "时间口径（今天/昨天/T+1）",
        "keywords": ("时间口径", "t+1", "昨天怎么算", "今天数据", "自然月"),
        "definition": (
            "日表是 T+1：昨天 = 今天 − 1 天，已完整；今天的数据可能还在生成，"
            "不能当全天最终值；月份按自然月。"
        ),
        "formula": "昨天 = date(today) − INTERVAL 1 DAY；区间用 [start, end) 左闭右开。",
        "data": "演示日期 2026-11-19；2026-11-18 为完整日（4,102.44 kWh）。",
        "caveats": ("当天数据不能当全天最终值。",),
        "tables": ("dm_eo_e_meter_di",),
        "code": "agent.parse_time_range",
    },
}

STRONG_MARKERS = (
    "怎么算",
    "如何计算",
    "怎么计算",
    "计算公式",
    "公式",
    "定义",
    "是什么",
    "什么意思",
    "含义",
    "原理",
    "怎么判断",
    "判定",
    "流程",
    "怎么区分",
    "区别",
    "口径",
    "怎么评估",
    "如何评估",
)

WEAK_MARKERS = (
    "有哪些",
    "在哪里",
    "在哪",
    "有没有",
    "怎么同步",
    "如何同步",
    "怎么做的",
    "怎么实现",
    "如何实现",
    "是多少",
)

DATA_MARKERS = ("昨天", "今天", "本月", "上月", "本周", "上周", "最近", "排名", "趋势")


def _score(text: str, item: dict[str, Any]) -> int:
    lowered = text.lower()
    return sum(len(word) for word in item["keywords"] if word.lower() in lowered)


def match_concept(question: str) -> dict[str, Any] | None:
    """按关键词长度加权匹配，长词优先，避免“设备”“冷量”这种泛词误命中。"""
    text = question.replace(" ", "")
    best: dict[str, Any] | None = None
    best_score = 0
    for item in CONCEPTS.values():
        score = _score(text, item)
        if score > best_score:
            best_score = score
            best = item
    return best if best_score >= 4 else None


def is_concept_question(question: str) -> bool:
    text = question.replace(" ", "")
    if match_concept(question) is None:
        return False
    if any(marker in text for marker in STRONG_MARKERS):
        return True
    if not any(marker in text for marker in WEAK_MARKERS):
        return False
    # “昨天有没有异常用电”这类要数据，不要返回定义。
    return not any(word in text for word in DATA_MARKERS)


def concept_answer(question: str) -> str | None:
    """定义类问题直接返回结构化口径；不是定义类问题返回 None。"""
    if not is_concept_question(question):
        return None
    item = match_concept(question)
    if item is None:
        return None
    lines = [
        f"### {item['title']}",
        "",
        "### 结论",
        item["definition"],
        "",
        "### 计算口径",
        item["formula"],
        "",
        "### 本项目数据情况",
        item["data"],
        "",
        "### 边界与注意事项",
    ]
    lines += [f"- {caveat}" for caveat in item["caveats"]]
    lines += [
        "",
        "### 相关表与代码",
        "- 表：" + "、".join(f"「{table}」" for table in item["tables"]),
        f"- 代码：{item['code']}",
    ]
    return "\n".join(lines)


def concept_catalog() -> dict[str, Any]:
    """给 GET /knowledge 与知识库工具用的结构化清单。"""
    return {
        key: {
            "title": item["title"],
            "definition": item["definition"],
            "formula": item["formula"],
            "data": item["data"],
            "caveats": item["caveats"],
            "tables": item["tables"],
            "code": item["code"],
        }
        for key, item in CONCEPTS.items()
    }


__all__ = [
    "CONCEPTS",
    "DATA_MARKERS",
    "STRONG_MARKERS",
    "WEAK_MARKERS",
    "concept_answer",
    "concept_catalog",
    "is_concept_question",
    "match_concept",
]
