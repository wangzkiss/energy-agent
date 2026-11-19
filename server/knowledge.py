"""能效 Agent 的代码逻辑与库表知识库。

这个模块是运行时可检索的“知识点”，不是 SQL 执行器：
- 已知指标由 providers.py 的固定模板查询；
- 固定模板未覆盖的问题由 investigator.py 做只读多步调查；
- 本模块负责回答“这个指标用哪张表/哪些字段/什么口径/代码在哪”，
  同时把表结构作为开放调查的上下文；
- 不包含数据库密码、appkey、租户连接串等敏感信息。

数据口径基于 2026-11-18 的 nwdy_dm 现网只读探查与当前代码。
"""
from __future__ import annotations

from typing import Any

from concepts import CONCEPTS, concept_catalog
from semantic import metric_catalog

KNOWLEDGE_VERSION = "2026-11-19"
CURRENT_TENANT_ID = 1026
CURRENT_TENANT_NAME = "南网能源"
CURRENT_TENANT_DB = "nwdy_dm"
CURRENT_PROJECT_ID = 1

TABLES: dict[str, dict[str, Any]] = {
    "dm_eo_e_meter_di": {
        "db": "每个租户的 Doris 库，如 nwdy_dm / rongyao_dm",
        "layer": "Doris 日粒度事实表",
        "grain": "tenant_id + project_id + meter_id + data_time + energy_type",
        "purpose": "表计日用电汇总，是总用电、趋势、表计排名的唯一主数据源",
        "used_for": ["total", "trend", "ranking"],
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "meter_id": "表计 ID",
            "meter_name": "表计名称",
            "data_time": "日期（日粒度）",
            "energy_type": "能源类型，电为 electric",
            "energy_value": "能耗值，单位 kWh",
            "energy_expense": "能耗费用（代码尚未接入）",
            "meter_attribute": "1 自用表 / 2 公摊表 / 3 总分表",
            "space_code": "计量范围空间编码",
            "space_name": "计量范围空间名称",
        },
        "filters": [
            "tenant_id = 当前租户",
            "project_id = 当前项目（默认 1）",
            "data_time BETWEEN 起止日期",
            "energy_type = 'electric'",
        ],
        "aggregations": {
            "total": "SUM(energy_value)，COUNT(DISTINCT meter_id)",
            "trend": "按 data_time GROUP BY，SUM(energy_value)，LIMIT 400",
            "ranking": "按 meter_id, meter_name GROUP BY，SUM(energy_value) DESC，Top 10",
        },
        "code": [
            "providers.ArcheryProvider.energy_total",
            "providers.ArcheryProvider.energy_trend",
            "providers.ArcheryProvider.meter_ranking",
        ],
        "not_for": [
            "不能把 energy_expense 当成当前已返回的电费；代码只返回 energy_value",
            "不能直接下钻到房间；space_code 只是计量范围，不是已验证的房间映射",
            "不能做同比/环比结论；虽然表里有 energy_tb/chain_ratio 字段，当前代码未启用",
        ],
        "profile_nwdy": "2026-04-01 ~ 2026-11-18，49,527 行，761 块表，1 个项目；按 meter_id+data_time+energy_type 无重复",
    },
    "dm_eo_e_meter_hi": {
        "db": "每个租户的 Doris 库，如 nwdy_dm / rongyao_dm",
        "layer": "Doris 小时粒度事实表",
        "grain": "tenant_id + project_id + meter_id + data_time + energy_type",
        "purpose": "小时表缺数/完整性排查；按小时统计行数和表计数",
        "used_for": ["data_quality"],
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "meter_id": "表计 ID",
            "data_time": "小时时间点（datetime）",
            "energy_type": "能源类型，电为 electric",
            "energy_value": "小时能耗值",
            "space_name": "空间名称",
        },
        "filters": [
            "tenant_id = 当前租户",
            "project_id = 当前项目（默认 1）",
            "data_time 当日",
            "energy_type = 'electric'",
        ],
        "aggregations": {
            "completeness": "按 data_time GROUP BY，COUNT(*) 行数、COUNT(DISTINCT meter_id) 表计数",
        },
        "code": ["providers.ArcheryProvider.hourly_completeness"],
        "not_for": [
            "不能替代日表总用电/趋势/排名",
            "当前模板只做单日小时点覆盖，不做跨日补数判断",
            "不把行数少直接等同于采集故障；需结合采集延迟和表计变更",
        ],
        "profile_nwdy": "2026-11-19 00:00~08:00 每小时 908 块表；09:00 是否完整需按查询时点判断，当前小时不计入缺失",
    },
    "dwi_eo_d_point_data": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 设备点位事实表",
        "grain": "device_id + point_code + data_time",
        "purpose": "设备实时/历史点位，室温查询取最近 2 小时的最新值",
        "used_for": ["room_temp"],
        "key_fields": {
            "device_id": "设备 ID，对应 dim_eo_d_device.id",
            "point_code": "点位编码；室温 RoomTemp，设定温度 RoomTempSp",
            "data_time": "采集时间",
            "point_value": "点位值，数值型",
        },
        "filters": [
            "device_id IN (从 dim_eo_d_device 按名称筛出的设备)",
            "point_code IN ('RoomTemp', 'RoomTempSp')",
            "data_time >= NOW() - 2 小时",
            "ROW_NUMBER() 按 device_id, point_code 取最新一条",
        ],
        "code": ["providers.ArcheryProvider.room_temp"],
        "not_for": [
            "表本身没有 tenant_id/project_id，租户隔离依赖设备维度表筛出的 device_id",
            "不能拿点位表直接算房间/楼层总能耗",
            "只代表最近一次上报，不代表全天舒适度",
        ],
        "profile_nwdy": "2026-03-31 ~ 2026-11-18，65,807,622 行，1,100 个设备；点位包含 RoomTemp/RoomTempSp/ModeSp 等",
    },
    "dim_eo_d_device": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 设备维度表",
        "grain": "id（设备）",
        "purpose": "设备名称、空间、分类、租户/项目归属；室温查询先在这里找设备",
        "used_for": ["room_temp", "设备维度"],
        "key_fields": {
            "id": "设备 ID",
            "name": "设备名称",
            "code": "设备编码",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "space_code": "空间编码",
            "space_name": "空间名称",
            "device_category_name": "设备分类名称",
            "device_status": "设备状态",
            "deleted": "删除标识，0 未删除",
        },
        "filters": ["tenant_id", "project_id", "deleted = 0", "name LIKE '%关键词%'", "LIMIT 5"],
        "code": ["providers.ArcheryProvider.room_temp"],
        "not_for": ["不能把 device_status 当作点位在线状态；代码返回的“在线”只表示最近 2 小时有点位"],
        "profile_nwdy": "1,808 行，全部 deleted=0",
    },
    "dm_eo_abnormal_meter_reading_record": {
        "db": "每个租户的 Doris 库，如 nwdy_dm / rongyao_dm",
        "layer": "Doris 异常抄表记录表",
        "grain": "tenant_id + project_id + meter_id + reading_time",
        "purpose": "异常表计/异常抄表记录排查",
        "used_for": ["open_investigation"],
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "meter_id": "表计 ID",
            "meter_name": "表计名称",
            "energy_type": "能源类型",
            "reading_time": "异常抄表发生时间",
            "data_source": "数据来源",
            "is_delete": "是否删除",
        },
        "filters": [
            "tenant_id = 当前租户",
            "reading_time 时间范围",
            "is_delete = '0'（如业务需要）",
        ],
        "code": ["investigator.DataInvestigator -> ad_hoc_query"],
        "not_for": ["不能把“异常抄表记录”直接等同于设备故障", "当前 nwdy 当天的异常表计记录为 0 行"],
        "profile_nwdy": "2026-11-19 当天 0 行；有记录时按 reading_time 查明细",
    },
    "eo_device_control_task": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 控制任务事实表",
        "grain": "task_id（设备控制任务）",
        "purpose": "控制任务成功/失败统计",
        "used_for": ["control"],
        "key_fields": {
            "task_id": "任务 ID",
            "device_id": "设备 ID（字符串）",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "status": "0 PENDING / 1 EXECUTING / 2 SUCCESS / 3 FAIL / 4 CANCELLED / 5 PREEMPTED",
            "create_time": "创建时间，业务查询时间列",
            "deleted": "删除标识，0 未删除",
            "error_msg": "失败原因",
            "device_name": "设备名称",
        },
        "filters": ["tenant_id", "project_id", "deleted = 0", "create_time BETWEEN 起止时间"],
        "aggregations": "按 status GROUP BY；成功=2，失败=3，其余计入 unknown；成功率=成功/总数",
        "code": ["providers.ArcheryProvider.control_stats"],
        "not_for": [
            "待执行/执行中/取消/抢占不能算成功或失败",
            "控制任务表不是设备状态表；不能回答“现在设备开没开”",
        ],
        "profile_nwdy": "2026-06-18 ~ 2026-11-18，96,174 行；样本 status 只有 2/3",
    },
    "dim_eo_alarms": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 告警事实表（名字是 dim_，实际按事实使用）",
        "grain": "id（告警记录）",
        "purpose": "告警数量、级别、状态统计",
        "used_for": ["alarm"],
        "key_fields": {
            "id": "告警记录 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "alarm_time": "告警采集时间",
            "alarm_level": "0 紧急 / 1 严重 / 2 一般 / 3 警告",
            "alarm_status": "0 未处理 / 1 处理中 / 2 已处理 / 3 无需处理 / 4 误报",
            "alarm_name": "告警名称",
            "device_id": "设备 ID",
            "work_order_code": "工单编号",
            "deleted": "删除标识，0 未删除",
        },
        "filters": ["tenant_id", "project_id", "deleted = 0", "alarm_time BETWEEN 起止时间"],
        "code": ["providers.ArcheryProvider.alarm_stats"],
        "not_for": [
            "空结果只能回答“不可用/无数据”，不能回答“0 条告警”",
            "不能把 alarm_status=1 当成告警；1 是处理中",
            "工单编号存在不代表有工单闭环表",
        ],
        "profile_nwdy": "0 行；nwdy_dm 当前不可用",
    },
    "dm_eo_s_space_di": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 空间日粒度表",
        "grain": "空间 + 日期 + 能源类型（但同一业务键可能多行）",
        "purpose": "空间/楼层维度的能耗，可用于未来房间级分析",
        "used_for": [],
        "key_fields": {
            "space_code": "空间编码",
            "space_name": "空间名称",
            "parent_code": "父空间编码",
            "data_time": "日期",
            "energy_type": "能源类别",
            "energy_value": "能耗值",
            "energy_expense": "能耗费用",
        },
        "filters": ["tenant_id", "project_id", "data_time", "energy_type"],
        "code": [],
        "not_for": [
            "当前代码不使用它算总能耗",
            "同一 space_code+data_time+energy_type 最多有 4 行，直接 SUM 会重复计算",
            "没有经过验证的表计→房间结构化映射，不能回答房间级能耗",
        ],
        "profile_nwdy": "776 行；同一 space_code+data_time+energy_type 最多 4 行",
    },
    "dwr_eo_energy_meter_wide": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 表计宽表（当前日快照）",
        "grain": "表计/设备/空间/日期/分时段",
        "purpose": "当前日的表计-设备-空间宽表，含总/峰/谷/平/尖分时段",
        "used_for": [],
        "key_fields": {
            "meter_id": "表计 ID",
            "device_id": "设备 ID",
            "space_code": "空间编码",
            "data_time": "日期",
            "energy_value": "能耗值",
            "energy_expense": "能耗费用",
            "segment": "0 总 / 1 峰 / 2 谷 / 3 平 / 4 尖",
            "power_direct": "1 正向耗能 / -1 反向产能",
        },
        "filters": ["tenant_id", "project_id", "data_time = 当前日"],
        "code": [],
        "not_for": [
            "只有当前日快照，不能做历史趋势",
            "当前代码不使用它；峰谷/需量分析尚未接入",
        ],
        "profile_nwdy": "2026-11-18 单日 758 行",
    },
    "dwi_eo_energy_usage": {
        "db": "每个租户的 Doris 库",
        "layer": "Doris 用能快照表",
        "grain": "meter_id + report_time + point_code + segment + power_direct",
        "purpose": "当前用能快照/手动抄表点位",
        "used_for": [],
        "key_fields": {
            "meter_id": "表计 ID",
            "report_time": "上报时间",
            "point_code": "点位代码",
            "segment": "0 总 / 1 峰 / 2 谷 / 3 平 / 4 尖",
            "energy_usage": "能耗使用值",
            "energy_expense": "能耗费用",
            "energy_saving": "节能量",
        },
        "filters": ["report_time"],
        "code": [],
        "not_for": ["只有当前时点快照，不能做趋势", "当前代码不使用它"],
        "profile_nwdy": "2026-11-18 00:00 单时点 758 行",
    },
    "algo_overview_kpi": {
        "db": "每个租户的 Doris 库",
        "layer": "冷站算法总览 KPI",
        "grain": "site_id + data_time",
        "purpose": (
            "冷站系统 SCOP 的落库表：system_scop、当前冷负荷、在运主机数；"
            "SCOP 问题应先查这里，有值就直接用"
        ),
        "used_for": [],
        "key_fields": {
            "site_id": "冷站 ID（对应 MySQL cold_site_id）",
            "data_time": "数据生成时间",
            "system_scop": "系统 SCOP（算法已算出时才有值）",
            "current_load": "当前实际冷负荷 kW",
            "running_chiller_cnt": "在运主机数",
            "total_chiller_cnt": "主机总数",
            "algorithm_status": "算法状态：正常/告警/异常",
            "energy_save_pct": "节能率 %",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
        },
        "filters": ["tenant_id", "project_id", "data_time"],
        "code": [],
        "not_for": [
            "亨通 2026-11-19 实测 0 行：算法未跑或未接入，不能当 0 SCOP",
            "没有 tenant_id 时不能查（表有 tenant_id，必须带租户过滤）",
        ],
        "profile_nwdy": "南网能源 0 行；亨通 0 行",
    },
    "algo_intraday_cop_compare": {
        "db": "每个租户的 Doris 库",
        "layer": "冷站日内 COP 对比",
        "grain": "site_id + curve_date + hour_slot",
        "purpose": "逐小时当前 COP 与基准 COP，用于日内能效对标",
        "used_for": [],
        "key_fields": {
            "site_id": "冷站 ID",
            "curve_date": "曲线日期",
            "hour_slot": "时段 0-23",
            "cop_current": "当前 COP",
            "cop_baseline": "基准 COP",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
        },
        "filters": ["tenant_id", "project_id", "curve_date"],
        "code": [],
        "not_for": ["亨通 2026-11-19 实测 0 行", "表里是 COP 曲线，不是系统 SCOP 汇总"],
        "profile_nwdy": "亨通 0 行",
    },
    "eo_tenant_data_sources": {
        "db": "MySQL energyOperation（配置库，不是 Doris 业务库）",
        "layer": "MySQL 租户数据源配置表",
        "grain": "tenant_id + db_type（每个租户可有多条连接）",
        "purpose": "租户与 MySQL/Doris 库名的映射来源",
        "used_for": ["tenants"],
        "key_fields": {
            "tenant_id": "租户 ID",
            "db_type": "数据库类型（MySQL/Doris 两类）",
            "db_name": "库名前缀",
            "deleted": "删除标识",
        },
        "filters": ["deleted = 0", "只取 tenant_id / db_type / db_name；不读取账号密码"],
        "code": ["tenants.TENANT_CATALOG（静态同步）", "GET /tenants"],
        "not_for": [
            "不能把连接串、账号、口令返回给前端或模型",
            "静态目录不会实时反映新增租户；需更新 tenants.py",
        ],
        "profile_nwdy": "15 个租户；Doris 库名为 db_name + '_dm'；当前 1026/nwdy_dm",
    },
}

# 线上 MySQL 业务库：每个租户一个库，库名=租户 code（如 nwdy / rongyao）。
# 这里只列高价值表，完整 219 张表由调查 Agent 用 SHOW TABLES LIKE 自行发现。
MYSQL_TABLES: dict[str, dict[str, Any]] = {
    "eo_projects": {
        "purpose": "项目主数据；确认项目名称、时区、面积、地址",
        "key_fields": {
            "id": "项目 ID",
            "tenant_id": "租户 ID",
            "name": "项目名称",
            "code": "项目编号",
            "time_zone": "时区",
            "area": "面积（平方米）",
            "deleted": "删除标识，0 正常 / 1 删除",
        },
        "time_fields": [],
    },
    "eo_spaces": {
        "purpose": "空间/楼层/房间主数据；space_code 与设备、表计、告警关联",
        "key_fields": {
            "id": "空间 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "name": "空间名称",
            "level": "空间层级",
            "parent_code": "父空间编码",
            "code": "空间编码",
            "space_path": "空间路径",
            "deleted": "删除标识",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_devices": {
        "purpose": "设备台账；设备名称、分类、空间、状态、告警状态",
        "key_fields": {
            "id": "设备 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "name": "设备名称",
            "code": "设备编码",
            "space_code": "空间编码",
            "space_name": "空间名称",
            "device_category_name": "设备分类名称",
            "device_status": "设备状态",
            "alarm_status": "告警状态",
            "alarm_time": "最近告警时间",
            "deleted": "删除标识",
        },
        "time_fields": ["create_time", "update_time", "alarm_time"],
    },
    "eo_energy_meters": {
        "purpose": "表计档案；表计编号、能源类型、表计属性、关联设备/空间",
        "key_fields": {
            "id": "表计 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "name": "表计名称",
            "meter_number": "表计编号",
            "meter_attribute": "1 自用表 / 2 公摊表 / 3 总分表",
            "energy_type": "能源类型，电通常为 electric",
            "meter_status": "0 已报废 / 2 正常",
            "device_id": "关联设备 ID",
            "install_space_code": "安装空间编码",
            "deleted": "删除标识",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_energy_meter_readings": {
        "purpose": "MySQL 抄表读数明细；按 meter_id + reading_time 查询",
        "key_fields": {
            "id": "主键",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "meter_id": "表计 ID",
            "reading_value": "抄表读数",
            "reading_time": "抄表时间",
            "reading_source": "数据来源",
            "pvf": "峰谷平标记",
            "deleted": "删除标识",
        },
        "time_fields": ["reading_time", "create_time"],
    },
    "eo_alarms": {
        "purpose": "MySQL 告警明细；按 alarm_time 过滤，关联设备/空间/工单",
        "key_fields": {
            "id": "告警 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "alarm_name": "告警名称",
            "alarm_level": "0 紧急 / 1 严重 / 2 一般 / 3 警告",
            "alarm_status": "0 未处理 / 1 处理中 / 2 已处理 / 3 无需处理 / 4 误报",
            "alarm_time": "告警时间",
            "alarm_recovery_time": "恢复时间",
            "device_id": "设备 ID",
            "space_code": "空间编码",
            "work_order_code": "关联工单编号",
            "deleted": "删除标识",
        },
        "time_fields": ["alarm_time", "create_time"],
    },
    "eo_device_control_task": {
        "purpose": "MySQL 设备控制任务；按 create_time 统计成功/失败/取消",
        "key_fields": {
            "task_id": "任务唯一标识",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "device_id": "设备 ID（场景控制时为空）",
            "device_name": "设备名称",
            "task_type": "0 实时 / 1 场景 / 2 定时 / 3 联动",
            "status": "0 待执行 / 1 执行中 / 2 成功 / 3 失败 / 4 取消 / 5 被抢占",
            "error_msg": "失败原因",
            "create_time": "创建时间",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_device_control_log": {
        "purpose": "MySQL 设备控制执行日志；exec_result、操作人、执行时间",
        "key_fields": {
            "id": "主键",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "device_id": "设备 ID",
            "exec_result": "执行结果",
            "exec_time": "执行时间",
            "control_type": "控制类型",
            "deleted": "删除标识",
        },
        "time_fields": ["exec_time", "create_time"],
    },
    "eo_ticket": {
        "purpose": "MySQL 工单；注意该表没有 tenant_id，按租户独立库隔离",
        "key_fields": {
            "id": "工单 ID",
            "ticket_no": "工单编号",
            "title": "工单标题",
            "status": "工单状态",
            "priority": "优先级",
            "device_name": "设备名称",
            "alarm_id": "关联告警 ID",
            "created_at": "创建时间",
            "start_time": "开始时间",
            "end_time": "结束时间",
            "deadline_time": "SLA 截止时间",
        },
        "time_fields": ["created_at", "start_time", "end_time", "deadline_time"],
    },
    "eo_energy_prices": {
        "purpose": "MySQL 电价方案；峰谷平价格，可用于电费分析",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "energy_type": "能源类型",
            "price": "价格",
            "pvf": "峰谷平标记",
            "start_time": "开始时间 HHmm",
            "end_time": "结束时间 HHmm",
            "plan_name": "方案名称",
            "deleted": "删除标识",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_ai_strategy": {
        "purpose": "MySQL AI 温控策略；区域均温、AI 目标温度、天气、是否执行",
        "key_fields": {
            "id": "主键",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "zone_id": "分区 ID",
            "space_id": "空间 ID",
            "execute_time": "执行时间",
            "zone_avg_temp": "区域均温",
            "ai_adjust_temp": "AI 调测目标温度",
            "weather": "天气",
            "executed": "0 待执行 / 1 已执行",
            "deleted": "删除标识",
        },
        "time_fields": ["execute_time", "create_time"],
    },
    "eo_ai_strategy_execute_log": {
        "purpose": "MySQL AI 温控策略执行记录；与 eo_ai_strategy 对应",
        "key_fields": {
            "id": "主键",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "zone_id": "分区 ID",
            "space_id": "空间 ID",
            "execute_time": "执行时间",
            "zone_avg_temp": "区域均温",
            "ai_adjust_temp": "AI 调测目标温度",
            "weather": "天气",
            "deleted": "删除标识",
        },
        "time_fields": ["execute_time", "create_time"],
    },
    "eo_env_device": {
        "purpose": "MySQL 环境设备/指标绑定；设备、空间、区域、指标编码",
        "key_fields": {
            "device_id": "设备 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "property_code": "指标编码",
            "device_name": "设备名称",
            "space_code": "空间编码",
            "zone_id": "区域 ID",
            "deleted": "删除标识",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_device_real_readings": {
        "purpose": "MySQL 设备实时点位快照；param_json 内是点位值，不能直接做趋势",
        "key_fields": {
            "device_id": "设备 ID",
            "device_category_id": "设备分类 ID",
            "project_id": "项目 ID",
            "report_time": "上报时间",
            "param_json": "点位值 JSON",
        },
        "time_fields": ["report_time"],
    },
    "eo_energy_saving_cooling_base_configs": {
        "purpose": "冷站主配置表；回答“配置了哪些冷站”首先查这张表",
        "key_fields": {
            "id": "主键",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID，关联 eo_applications.id",
            "cooling_name": "冷站名称",
            "cold_site_id": "冷站业务 ID，字符串",
            "space_code": "所属空间编码，可能是逗号分隔路径（如 10001,1000100001）",
            "calendar_code": "日历编码；样例值为 1，与 eo_calendar.calendar_id 不一定同值",
            "cold_type": "源表注释：1=冷源系统，2=集中供冷；部分租户出现 3，注释未定义",
            "description": "描述",
            "deleted": "逻辑删除，0=有效",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_energy_saving_cooling_device_configs": {
        "purpose": "冷站设备额定参数配置；冷机/水泵/冷却塔等",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID，关联冷站主配置",
            "device_id": "设备 ID，关联 eo_devices.id",
            "device_category_id": "设备分类 ID，关联 eo_device_categories.id",
            "category_code": "分类编码，如 Chiller / ChillerWaterPump",
            "cop": "额定 COP",
            "power": "额定功率",
            "cooling_capacity": "额定制冷量",
            "flow": "额定流量",
            "lift": "额定扬程",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_energy_saving_cooling_device_properties": {
        "purpose": "冷站点位/指标配置；把 metrics_type 映射到设备点位",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID",
            "site_id": "冷站 ID（样例存在 undefined，不能当可靠键）",
            "device_id": "设备 ID",
            "device_category_id": "设备分类 ID",
            "device_category_name": "设备分类名称",
            "metrics_type": "指标类型，如 ChilledWaterSupplyTemp",
            "point_code": "点位配置信息",
            "point_name": "点位名称",
            "point_name_cn": "点位中文名称",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_energy_saving_cooling_system_configs": {
        "purpose": "冷站系统系数配置，如 frozen_pump / cooling_tower / cooling_side",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID",
            "config_type": "配置类型，常见 factor（系数）",
            "config_code": "配置编码，如 frozen_pump、cooling_tower、cooling_side",
            "config_value": "配置值（字符串）",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_energy_saving_cooling_alarm_configs": {
        "purpose": "冷站告警阈值配置",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID",
            "alarm_type_id": "告警类型 ID",
            "quota_threshold_period": "统计维度：hour/day/month",
            "threshold_value": "高于阈值（用电量/制冷量）",
            "threshold_lower": "SCOP 下限",
            "threshold_upper": "SCOP 上限",
            "alarm_content": "告警内容模板",
            "enable_status": "0=停用，1=启用",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_cold_algo_config": {
        "purpose": "冷站算法开关配置；algo_type 0=无算法、1=设施云、2=自研",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID",
            "algo_type": "0-无算法，1-设施云，2-自研算法",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_cold_algo_schedule": {
        "purpose": "冷站算法排期/冷负荷预测；对应应用已配置算法时才有数据",
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID",
            "schedule_date": "日期 yyyyMMdd",
            "cooling_load_peak_kw": "冷负荷峰值 kW",
            "cooling_load_total_kwh": "冷负荷总量 kWh",
            "total_energy_kwh": "算法预估总耗电量 kWh",
            "confirm_status": "PENDING / CONFIRMED_ALGO / CONFIRMED_CUSTOM",
            "deleted": "逻辑删除",
        },
        "time_fields": ["schedule_date", "generated_at", "create_time"],
    },
    "eo_cold_algo_schedule_hourly": {
        "purpose": (
            "冷站逐小时排期：冷负荷 kW 与系统/冷机功率 kW，可直接算 SCOP = "
            "SUM(cooling_load_kw)/SUM(total_power_kw)；2026-11-19 现网该表为 0 行"
        ),
        "key_fields": {
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "application_id": "冷站应用 ID，关联冷站主配置",
            "schedule_date": "日期 yyyyMMdd",
            "hour": "小时 0-23",
            "run_ratio": "系统运行时长占比 0~1",
            "cooling_load_kw": "该时段冷负荷 kW",
            "chiller_power_kw": "该时段冷机功率 kW",
            "total_power_kw": "该时段整体系统功率 kW",
            "load_kw": "冷量 kW（与 cooling_load_kw 同义）",
            "deleted": "逻辑删除",
        },
        "time_fields": ["schedule_date", "create_time"],
    },
    "eo_applications": {
        "purpose": "应用/菜单目录；冷站的 application_id 对应这里的 id",
        "key_fields": {
            "id": "应用 ID",
            "tenant_id": "租户 ID",
            "name": "应用/冷站名称",
            "app_type": "应用类型",
            "parent_id": "父级 ID",
            "module_code": "模块编码",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_device_categories": {
        "purpose": "设备分类；冷站设备通过 device_category_id 关联",
        "key_fields": {
            "id": "设备分类 ID",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "name": "设备分类名称，如冷机/冷冻水泵",
            "parent_id": "父级分类 ID",
            "level": "层级",
            "type": "0=文件夹，1=节点",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
    "eo_calendar": {
        "purpose": "日历配置；冷站 calendar_code 可能引用这里的业务日历",
        "key_fields": {
            "id": "主键",
            "tenant_id": "租户 ID",
            "project_id": "项目 ID",
            "calendar_id": "日历 ID（设施云规则生成）",
            "calendar_name": "日历名称",
            "loop_calendar_data": "周一至周日状态，如 1,1,1,1,1,0,0",
            "deleted": "逻辑删除",
        },
        "time_fields": ["create_time", "update_time"],
    },
}

MYSQL_RELATIONSHIPS: list[str] = [
    "eo_devices.space_code → eo_spaces.code（逻辑关联，无外键）",
    "eo_energy_meters.device_id → eo_devices.id（逻辑关联）",
    "eo_energy_meter_readings.meter_id → eo_energy_meters.id（逻辑关联）",
    "eo_alarms.device_id → eo_devices.id；eo_alarms.space_code → eo_spaces.code",
    "eo_alarms.work_order_code → eo_ticket.ticket_no（按工单编号）",
    "eo_device_control_task.device_id → eo_devices.id（类型为 varchar，需按实际值比较）",
    "eo_device_control_log.device_id → eo_devices.id",
    "eo_ai_strategy.zone_id / space_id → 分区/空间（逻辑关联）",
    "eo_energy_saving_cooling_base_configs.application_id → eo_applications.id",
    "eo_energy_saving_cooling_base_configs.cold_site_id → eo_energy_saving_cooling_device_properties.site_id（点位配置；样例有 undefined，需判空）",
    "eo_energy_saving_cooling_device_configs.application_id → eo_energy_saving_cooling_base_configs.application_id",
    "eo_energy_saving_cooling_device_configs.device_id → eo_devices.id",
    "eo_energy_saving_cooling_device_configs.device_category_id → eo_device_categories.id",
    "eo_energy_saving_cooling_device_properties.application_id → 冷站应用 ID；device_id → eo_devices.id",
    "eo_energy_saving_cooling_system_configs.application_id → 冷站应用 ID",
    "eo_energy_saving_cooling_alarm_configs.application_id → 冷站应用 ID；alarm_type_id → 告警类型",
    "eo_cold_algo_config.application_id → 冷站应用 ID；eo_cold_algo_schedule.application_id → 冷站应用 ID",
    "eo_energy_saving_cooling_base_configs.space_code → eo_spaces.code（逗号分隔路径，需拆分/FIND_IN_SET）",
    "eo_energy_saving_cooling_base_configs.calendar_code → eo_calendar 的业务日历（样例为 1，不能直接等同 calendar_id）",
    "MySQL 每个租户一个库；eo_ticket 没有 tenant_id，靠库隔离，不能跨库查询",
]

INTENT_ROUTES: dict[str, dict[str, Any]] = {
    "memory": {
        "question": "我刚才问了什么 / 上一轮你怎么回答的",
        "tables": ["（会话历史，不查询业务数据）"],
        "filter": "无",
        "return": "上一轮用户问题、最近几轮问题或上一轮回答",
        "code": "agent.answer(memory) -> _memory_answer；ConversationContext",
    },
    "cold_station": {
        "question": "配置了哪些冷站 / 冷站清单",
        "tables": ["eo_energy_saving_cooling_base_configs"],
        "filter": "tenant_id + project_id + deleted=0",
        "return": "冷站名称、cold_site_id、space_code、calendar_code、cold_type",
        "code": "agent.detect_intent(cold_station) -> _cold_station_answer；MySQLArcheryProvider.cooling_stations",
    },
    "data_quality": {
        "question": "小时表的数据今日有缺少的吗 / 数据完整性如何",
        "tables": ["dm_eo_e_meter_hi"],
        "filter": "tenant_id + project_id + energy_type='electric' + data_time 当日",
        "return": "按小时统计行数/去重表计数；对比已结束整点，判断缺失小时点/表计不足",
        "code": "agent.answer(data_quality) -> ArcheryProvider.hourly_completeness -> _hourly_quality_answer",
    },
    "total": {
        "question": "昨天/今天/本月总用电多少",
        "tables": ["dm_eo_e_meter_di"],
        "filter": "tenant_id + project_id + data_time + energy_type='electric'",
        "return": "SUM(energy_value) kWh，有效表计数",
        "code": "agent._collect(total) -> ArcheryProvider.energy_total",
    },
    "trend": {
        "question": "近 30 天用电趋势",
        "tables": ["dm_eo_e_meter_di"],
        "filter": "tenant_id + project_id + data_time 区间 + energy_type",
        "return": "按日 SUM(energy_value)，最多 400 天",
        "code": "agent._collect(trend) -> ArcheryProvider.energy_trend",
    },
    "ranking": {
        "question": "哪块表用电最多",
        "tables": ["dm_eo_e_meter_di"],
        "filter": "tenant_id + project_id + data_time 区间 + energy_type",
        "return": "按 meter_id/meter_name 汇总，Top 10",
        "code": "agent._collect(ranking) -> ArcheryProvider.meter_ranking",
    },
    "room_temp": {
        "question": "36 层现在室温多少",
        "tables": ["dim_eo_d_device", "dwi_eo_d_point_data"],
        "filter": "设备名 LIKE + deleted=0；点位 RoomTemp/RoomTempSp，最近 2 小时",
        "return": "设备名、室温、设定温度、最近上报时间",
        "code": "agent._collect(room_temp) -> ArcheryProvider.room_temp",
    },
    "control": {
        "question": "昨天控制成功率多少",
        "tables": ["eo_device_control_task"],
        "filter": "tenant_id + project_id + deleted=0 + create_time",
        "return": "总数/成功/失败/未知/成功率；2=成功，3=失败",
        "code": "agent._collect(control) -> ArcheryProvider.control_stats",
    },
    "alarm": {
        "question": "昨天有多少告警",
        "tables": ["dim_eo_alarms"],
        "filter": "tenant_id + project_id + deleted=0 + alarm_time",
        "return": "按级别/状态计数；0 行时回答“不可用”",
        "code": "agent._collect(alarm) -> ArcheryProvider.alarm_stats",
    },
    "tenants": {
        "question": "目前支持哪些租户的数据",
        "tables": ["eo_tenant_data_sources"],
        "filter": "静态租户目录；不查询业务表",
        "return": "租户名称/编码/ID、MySQL 库、Doris 库、当前租户",
        "code": "agent.answer -> tenant_answer；GET /tenants",
    },
    "knowledge": {
        "question": "总用电量查哪张表/字段/口径",
        "tables": ["（知识库本身，不查询业务数据）"],
        "filter": "无",
        "return": "问题→意图→库表→字段→过滤条件→代码位置",
        "code": "agent.answer(knowledge) -> knowledge_answer",
    },
}

RELATIONSHIPS: list[dict[str, str]] = [
    {
        "from": "dim_eo_d_device.id",
        "to": "dwi_eo_d_point_data.device_id",
        "type": "1:N",
        "usage": "室温查询：先按设备名筛设备，再取点位最新值",
    },
    {
        "from": "dim_eo_d_device.id",
        "to": "dwr_eo_energy_meter_wide.device_id",
        "type": "1:N",
        "usage": "宽表里有设备/空间/表计关联；当前代码未使用",
    },
    {
        "from": "dm_eo_e_meter_di.meter_id",
        "to": "dwr_eo_energy_meter_wide.meter_id",
        "type": "1:N",
        "usage": "宽表可用于表计-空间桥接；当前代码未使用",
    },
    {
        "from": "dm_eo_e_meter_di.space_code",
        "to": "dm_eo_s_space_di.space_code",
        "type": "N:1（逻辑）",
        "usage": "空间维度桥接；空间表有重复业务键，不能直接 SUM",
    },
    {
        "from": "eo_device_control_task.device_id",
        "to": "dim_eo_d_device.id",
        "type": "N:1（逻辑，需 CAST）",
        "usage": "控制任务可关联设备；当前代码不 JOIN",
    },
    {
        "from": "dim_eo_alarms.device_id",
        "to": "dim_eo_d_device.id",
        "type": "N:1（逻辑）",
        "usage": "告警可关联设备；当前代码不 JOIN",
    },
    {
        "from": "eo_tenant_data_sources.tenant_id",
        "to": "各 Doris 库的 tenant_id",
        "type": "1:N",
        "usage": "租户目录/动态 Provider；Doris 库名 = db_name + '_dm'",
    },
]

UNSUPPORTED: list[dict[str, str]] = [
    {"topic": "同比/环比", "reason": "历史不足两年；字段 energy_tb/chain_ratio 存在但未接入"},
    {"topic": "节能率/节能量", "reason": "没有可靠基线；energy_saving_consumption 未验证"},
    {"topic": "房间级能耗", "reason": "表计到房间没有经过验证的结构化映射"},
    {"topic": "整栋建筑总能耗", "reason": "当前只有租户/项目级汇总"},
    {"topic": "气象归一化", "reason": "没有气象数据"},
    {"topic": "工单闭环率/SLA 结论", "reason": "MySQL eo_ticket 可查工单明细，但闭环口径要按业务状态和 SLA 确认；无数据时不能编造"},
    {"topic": "实时控制", "reason": "系统只读，不能下发指令"},
    {"topic": "峰谷/需量分析", "reason": "宽表只有 segment=0 快照；没有需量表，ActivePowerSum 仅 3 台设备且全为 0"},
    {"topic": "电费", "reason": "eo_energy_prices 在租户 1026 下 0 行，energy_expense 全为 0；只能讲公式，不能给金额"},
    {"topic": "分项计量", "reason": "meter_category_id 全空、meter_attribute 全为 1；按名称近似归类只覆盖约 25% 电量"},
    {"topic": "单位面积能耗", "reason": "eo_projects.area 与 eo_spaces.area 全部为占位值 1"},
    {"topic": "碳排/折标煤", "reason": "库内没有排放因子和折标煤系数，只能给公开参考值"},
    {"topic": "倍率核对", "reason": "eo_energy_meters.rate 全为 1，抄表明细 0 行，无法判断日表是否已乘倍率"},
    {"topic": "跨日补数/采集值准确性", "reason": "已支持单日整点覆盖检查；补数状态、采集值准确性和采集任务状态未接入"},
]

CODE_FLOW: list[dict[str, str]] = [
    {"layer": "浏览器", "path": "web/src/services/chat/store.ts", "role": "调用 BFF，解析 Anthropic SSE"},
    {"layer": "BFF 入口", "path": "server/app.py", "role": "鉴权、ChatRequest 解析、SSE 事件组装"},
    {"layer": "Agent", "path": "server/agent.py", "role": "意图、时间、租户、会话上下文、facts、答案与兜底"},
    {"layer": "会话记忆", "path": "web/src/services/chat/reply.ts", "role": "同一会话历史 messages → ConversationContext"},
    {"layer": "数据层", "path": "server/providers.py", "role": "ArcheryProvider（Doris 固定 SQL）/ MySQLArcheryProvider（MySQL 业务库）/ MockProvider"},
    {"layer": "开放调查", "path": "server/investigator.py", "role": "多源 Agent loop：模型选源→只读 SQL→观察→修正→结论；SQL Guard 强制租户/库隔离/时间/限行"},
    {"layer": "租户目录", "path": "server/tenants.py", "role": "名称/编码/ID→Doris/MySQL 库；跨租户开关"},
    {"layer": "模型", "path": "server/llm.py", "role": "组织固定模板 facts；开放调查时生成受控只读 SQL"},
    {"layer": "知识库", "path": "server/knowledge.py", "role": "问题→意图→库表→字段→代码位置"},
]

EXAMPLES: list[dict[str, str]] = [
    {
        "question": "昨天总用电多少？",
        "intent": "total",
        "table": "nwdy_dm.dm_eo_e_meter_di",
        "result": "SUM(energy_value) = 4,102.44 kWh（2026-11-18 实际日汇总）",
    },
    {
        "question": "36 层现在室温多少？",
        "intent": "room_temp",
        "table": "nwdy_dm.dim_eo_d_device + nwdy_dm.dwi_eo_d_point_data",
        "result": "按设备名找设备，再取 RoomTemp/RoomTempSp 最近 2 小时最新值",
    },
    {
        "question": "昨天控制成功率多少？",
        "intent": "control",
        "table": "nwdy_dm.eo_device_control_task",
        "result": "status=2 为成功、3 为失败；其余为 unknown",
    },
    {
        "question": "查询一下租户荣耀的今日用电数据",
        "intent": "total",
        "table": "rongyao_dm.dm_eo_e_meter_di",
        "result": "已识别荣耀/1033/rongyao_dm；ALLOW_TENANT_QUERY=true 时按荣耀库查询",
    },
    {
        "question": "昨天总用电多少？→ 那今天呢？",
        "intent": "total（承接上一轮指标）",
        "table": "nwdy_dm.dm_eo_e_meter_di",
        "result": "ConversationContext 继承 total；本轮时间词“今天”覆盖上一轮“昨天”",
    },
    {
        "question": "查询荣耀昨日用电 → 那今天呢？",
        "intent": "total（承接上一轮指标和租户）",
        "table": "rongyao_dm.dm_eo_e_meter_di",
        "result": "继承 tenant=荣耀/1033；只把时间切换为今天",
    },
    {
        "question": "南网能源 MySQL 里有多少设备、多少表计？",
        "intent": "mysql（多源开放调查）",
        "table": "nwdy.eo_devices / nwdy.eo_energy_meters",
        "result": "实测 1,808 台设备、908 个表计（deleted=0）",
    },
    {
        "question": "南网能源 MySQL 控制任务成功/失败分布？",
        "intent": "mysql（多步调查）",
        "table": "nwdy.eo_device_control_task",
        "result": "实测 4,664 条：status=2 成功 4,660，status=3 失败 4；成功率约 99.91%",
    },
    {
        "question": "我刚才问了什么？",
        "intent": "memory",
        "table": "（会话历史，不查库）",
        "result": "返回同一会话上一轮用户问题",
    },
]


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def route_tables(question: str) -> list[str]:
    """根据问题关键词选择最相关的表；用于知识问答，不执行 SQL。"""
    text = question.replace(" ", "")
    if _has(text, ("室温", "温度", "设定温度", "空调面板", "点位", "roomtemp")):
        return ["dwi_eo_d_point_data", "dim_eo_d_device"]
    if _has(text, ("控制", "成功率", "下发", "执行失败", "任务状态")):
        return ["eo_device_control_task"]
    if _has(text, ("告警", "报警", "alarm")):
        return ["dim_eo_alarms"]
    if _has(text, ("排名", "最多", "top", "表计")):
        return ["dm_eo_e_meter_di"]
    if _has(text, ("趋势", "曲线", "每天", "逐日", "走势")):
        return ["dm_eo_e_meter_di"]
    if _has(text, ("峰谷", "尖峰", "分时", "宽表", "快照", "需量")):
        return ["dwr_eo_energy_meter_wide", "dwi_eo_energy_usage"]
    if _has(text, ("空间", "楼层", "房间", "space")):
        return ["dm_eo_s_space_di", "dm_eo_e_meter_di"]
    if _has(text, ("用电", "电量", "能耗", "电费", "总用电")):
        return ["dm_eo_e_meter_di"]
    if _has(text, ("租户", "数据源", "库名", "db_name")):
        return ["eo_tenant_data_sources"]
    if _has(text, ("设备", "device")):
        return ["dim_eo_d_device"]
    return []


def _table_line(name: str) -> str:
    table = TABLES[name]
    return f"| `{name}` | {table['layer']} | {table['purpose']} |"


def _table_detail(name: str) -> str:
    table = TABLES[name]
    fields = "、".join(f"`{field}`" for field in table["key_fields"])
    used_for = "、".join(table["used_for"]) or "当前代码未使用"
    filters = "；".join(table["filters"])
    code = "、".join(f"`{item}`" for item in table["code"]) or "无"
    aggregations = table.get("aggregations")
    if isinstance(aggregations, dict):
        agg_text = "；".join(f"{key}: {value}" for key, value in aggregations.items())
    else:
        agg_text = aggregations or "无"
    notes = "\n".join(f"- {item}" for item in table["not_for"])
    return "\n".join(
        [
            f"### `{name}`",
            "",
            f"- 所在库：{table['db']}",
            f"- 表层：{table['layer']}",
            f"- 粒度：{table['grain']}",
            f"- 用途：{table['purpose']}",
            f"- 支持意图：{used_for}",
            f"- 关键字段：{fields}",
            f"- 过滤条件：{filters}",
            f"- 聚合/返回：{agg_text}",
            f"- 代码位置：{code}",
            f"- 数据画像：{table['profile_nwdy']}",
            "",
            "**不能这样用**",
            "",
            notes,
        ]
    )


def _mysql_knowledge_answer() -> str:
    lines = [
        "### 线上 MySQL 业务库（Archery 只读）",
        "",
        "每个租户一个库，库名等于租户 code（如 `nwdy`、`rongyao`）；"
        "不建立 MySQL 直连，也不读取账号密码。调查 Agent 通过 Archery 切换 `db_name` 查询。",
        "",
        "| 表 | 用途 | 关键字段 |",
        "|---|---|---|",
    ]
    for name, meta in MYSQL_TABLES.items():
        fields = "、".join(f"`{field}`" for field in (meta.get("key_fields") or {}))
        lines.append(f"| `{name}` | {meta.get('purpose', '')} | {fields} |")
    lines += [
        "",
        "### 逻辑关系（无外键）",
        "",
        "- "
        + "\n- ".join(MYSQL_RELATIONSHIPS),
        "",
        "### 查询约定",
        "",
        "- 先用 `SHOW TABLES LIKE '%关键词%'` 找表，再用 `SHOW FULL COLUMNS FROM <库>.<表>` 看字段；"
        "Archery 对 `DESC` 支持不稳定。",
        "- 有 `tenant_id` 的表必须带当前租户过滤；`eo_ticket` 没有 `tenant_id`，靠每租户独立库隔离。",
        "- 事实表必须带时间过滤：告警 `alarm_time`、控制任务 `create_time`、"
        "抄表 `reading_time`、工单 `created_at`。",
        "- 完整 219 张表不逐一硬编码，由调查 Agent 按问题动态发现；"
        "本页只列高价值表，避免把库结构泄露给前端。",
    ]
    return "\n".join(lines)


def _cold_station_knowledge_answer() -> str:
    table = "eo_energy_saving_cooling_base_configs"
    fields = MYSQL_TABLES[table]["key_fields"]
    lines = [
        "### 冷站配置知识",
        "",
        "回答“配置了哪些冷站”应查 MySQL 主表 "
        f"`{table}`，不要从设备名称/空间名称猜。",
        "",
        "| 字段 | 含义 |",
        "|---|---|",
    ]
    lines += [f"| `{name}` | {desc} |" for name, desc in fields.items()]
    lines += [
        "",
        "### 固定查询口径",
        "",
        "```sql",
        f"SELECT id, application_id, cooling_name, cold_site_id, space_code,",
        f"       calendar_code, cold_type, description, create_time",
        f"FROM <租户库>.{table}",
        "WHERE tenant_id = <当前租户ID>",
        "  AND project_id = <当前项目ID>",
        "  AND deleted = 0",
        "ORDER BY id",
        "LIMIT 100;",
        "```",
        "",
        "### 关联",
        "",
        "- `application_id` → `eo_applications.id`，对应冷站应用/菜单；",
        "- `application_id` → `eo_energy_saving_cooling_device_configs.application_id`，"
        "查冷机/水泵等设备参数；",
        "- `cold_site_id` → `eo_energy_saving_cooling_device_properties.site_id`，查点位配置；",
        "- `space_code` → `eo_spaces.code`，可能是逗号分隔路径；",
        "- `cold_type` 源表注释：1=冷源系统，2=集中供冷；其它值注释未覆盖。",
        "",
        "### 实测",
        "",
        "`nwdy`（南网能源，tenant_id=1026，project_id=1）在 "
        f"`{table}` 中当前为 **0 条**有效记录，因此回答“当前没有配置冷站”，"
        "不能推断为冷站停运或设备不存在。",
        "",
        "其他租户样例：双汇 `shuanghui` 有“股份三冷站”“总部大楼冷站”；"
        "中交城投 `zjct` 有“中交总部冷站”；秦龙电力 `qldl` 有“秦龙电力”。",
    ]
    return "\n".join(lines)


def knowledge_answer(question: str) -> str:
    """回答“查哪张表/字段/口径/代码在哪”，不返回业务数字。"""
    text = question.replace(" ", "")
    if _has(text, ("冷站", "冷源", "供冷")):
        return _cold_station_knowledge_answer()
    if _has(text.lower(), ("mysql", "业务库", "关系库")):
        return _mysql_knowledge_answer()
    if _has(text, ("代码", "接口", "sse", "流程", "架构", "调用链")):
        lines = [
            "### 代码调用链",
            "",
            "| 层 | 文件 | 作用 |",
            "|---|---|---|",
        ]
        lines += [f"| {item['layer']} | `{item['path']}` | {item['role']} |" for item in CODE_FLOW]
        lines += [
            "",
            "### 一句话流程",
            "",
            "浏览器 → `POST /anthropic/v1/messages` → `EnergyAgent.answer()` → "
            "`detect_intent()` → `parse_time_range()` → `resolve_tenant()` → "
            "已知指标 `_collect()` → `ArcheryProvider` 固定 SQL；"
            "开放问题 → `DataInvestigator` 只读 SQL → SQL Guard → Archery → 结论；"
            "固定模板再由 `_narrate()`/兜底 → SSE。",
            "",
            "已知指标由固定模板产生数字；开放调查由模型生成只读 SQL，必须经过"
            "tenant_id/时间/限行校验；所有数字都来自实际查询。",
        ]
        return "\n".join(lines)

    selected = route_tables(question)
    if not selected or _has(text, ("全部", "所有", "知识库", "关系", "总览")):
        lines = [
            "### 能效 Agent 库表总览",
            "",
            "| 表 | 层次 | 用途 |",
            "|---|---|---|",
        ]
        lines += [_table_line(name) for name in TABLES]
        lines += [
            "",
            "### 业务口径（怎么算 / 是什么 / 流程）",
            "",
        ]
        lines += [f"- {item['title']}" for item in CONCEPTS.values()]
        lines += [
            "",
            "直接问“XX 怎么算/是什么/区别”会走这层，不查数据。",
            "",
            "### 问题路由",
            "",
            "| 问题类型 | 意图 | 主表 | 返回 |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {route['question']} | `{intent}` | {', '.join('`' + t + '`' for t in route['tables'])} | {route['return']} |"
            for intent, route in INTENT_ROUTES.items()
            if intent not in ("knowledge",)
        ]
        lines += [
            "",
            "要查具体字段/过滤条件，可以问：“总用电量查哪张表、哪些字段？”",
        ]
        return "\n".join(lines)

    lines = [
        "### 路由结论",
        "",
        "这个问题在代码里会落到以下库表（只说明知识，不执行查询）：",
        "",
        "| 表 | 层次 | 用途 |",
        "|---|---|---|",
    ]
    lines += [_table_line(name) for name in selected]
    lines += [""]
    lines += [_table_detail(name) for name in selected]
    lines += [
        "",
        "### 相关代码",
        "",
        "- 意图识别：`server/agent.py` 的 `detect_intent()`",
        "- 数据查询：`server/providers.py` 的 `ArcheryProvider`",
        "- 时间解析：`server/agent.py` 的 `parse_time_range()`",
        "- 完整知识库：`docs/能效Agent知识库.md`",
    ]
    return "\n".join(lines)


def knowledge_payload() -> dict[str, Any]:
    """给 GET /knowledge 的结构化版本；不包含任何密钥。"""
    return {
        "version": KNOWLEDGE_VERSION,
        "current_tenant": {
            "tenant_id": CURRENT_TENANT_ID,
            "name": CURRENT_TENANT_NAME,
            "doris_db": CURRENT_TENANT_DB,
            "project_id": CURRENT_PROJECT_ID,
        },
        "tables": TABLES,
        "mysql_tables": MYSQL_TABLES,
        "metrics": metric_catalog(),
        "concepts": concept_catalog(),
        "intent_routes": INTENT_ROUTES,
        "relationships": RELATIONSHIPS,
        "mysql_relationships": MYSQL_RELATIONSHIPS,
        "unsupported": UNSUPPORTED,
        "code_flow": CODE_FLOW,
        "examples": EXAMPLES,
    }


__all__ = [
    "KNOWLEDGE_VERSION",
    "TABLES",
    "MYSQL_TABLES",
    "INTENT_ROUTES",
    "RELATIONSHIPS",
    "MYSQL_RELATIONSHIPS",
    "UNSUPPORTED",
    "CODE_FLOW",
    "EXAMPLES",
    "knowledge_answer",
    "knowledge_payload",
    "route_tables",
]
