# 能效 Agent 技术方案

> **实现状态（2026-11-19）**：运行态**不建立 Doris 直连**，全部数据查询通过
> `/archery` 技能的 `archery_api.py` 执行（`DATA_PROVIDER=archery`）。
> 本文中的“Doris 直连”仅作为架构备选保留，当前未启用；生产也不配置
> `DORIS_HOST` / `DORIS_USER` / `DORIS_PASSWORD`。

## 基于 Doris 直连与 Archery 只读审计

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-11-18 |
| 数据来源 | Archery `数智能效saas现网-doris`（实例 298），只读查询 |
| 样本租户 | `nwdy_dm`（南网能源-岭誉广场）、`zjct_dm`（中交城投总部大厦）、`xxzd_dm`（众德一厂）等 |
| 适用对象 | 能效 Agent 的产品、后端、数据、算法、运维 |
| 前置结论 | **可行，但不能做成“自然语言直接生成 SQL 查 Doris”**。必须走“指标语义层 + 受控 SQL 模板 + 数据可用性探测 + 确定性计算 + LLM 解释”的路线 |

---

## 0. 先给结论

### 0.1 三句话结论

1. **数据底座够用，但极不均匀。** 同一套约 190 张表（样本租户 187 或 192 张），`nwdy_dm` 有 6,567 万条设备点位、9.5 万条控制记录，但只有电、只有多联机内机、日粒度只有约 65 天；`zjct_dm` 有冷/电/费用/碳；`xxzd_dm` 有 5.2 万条告警；`algo_*` 在抽样的 saas 租户里全部是 0 行。
2. **能做的核心能力是“查询 + 诊断”，不是“节能收益承诺”。** 可以做总量、趋势、排名、对比、设备舒适度、控制记录、告警、数据质量；不能做同比、节能率/节能量、房间级能耗、全楼总能耗、天气归一、生产单耗。
3. **推荐架构是“Doris 只读直连跑指标，Archery 做建模期 profiling、临时查询和审计，必要时切换为 Archery 运行时网关”。** 不要让 LLM 自由生成 SQL 直接打生产；所有指标走参数化模板，所有查询可审计、可复现、可限流。

### 0.2 为什么不是“NL → SQL”

- Doris 里同名概念很多：`dim_eo_e_space` 只有 1 行，`dim_eo_z_space` 才有 114 个有效空间；`dim_eo_a_application` 是菜单树不是物理系统；`dim_eo_alarms` 名字叫 dim，实际是告警事实表。LLM 凭字段名猜，必然猜错。
- 数据填充率差异极大：`nwdy_dm` 电价表 0 行、基准值表 0 行、`energy_expense` 全 0；`zjct_dm` 电价 JSON 里有 0.72 元/kWh、费用 62.8 万元。自由 SQL 很容易把“没配置”算成“0 元/0 节能”。
- 空间表还有 4 倍重复：`dm_eo_s_space_di` 同一天同一个空间有 4 行相同电量，直接 `SUM` 会把总电量放大 4 倍。这类坑必须靠模板和语义层兜住，不能靠模型临场发挥。

### 0.3 建议的能力定位

| 层级 | 定位 | 是否建议首期做 |
|---|---|---|
| L0 数据问答 | 台账、总量、趋势、排名、设备点位、控制记录、告警统计 | ✅ 首期 |
| L1 诊断分析 | 用能异常、舒适度、非工作时段运行、控制失败、数据质量 | ✅ 首期 |
| L2 建议 | 基于规则的节能建议（温度/风速/时段），不承诺节能量 | ⚠️ 二期，必须标注“未量化” |
| L3 执行 | 下发控制、工单、审批 | ❌ 不做，保持只读 |

---

## 1. 调研范围与方法

### 1.1 调研对象

生产 Doris 实例：`数智能效saas现网-doris`（Archery 实例 ID 298），承载 15 个租户库：

`chujiang_dm`、`ctl_dm`、`hengtong_dm`、`jagk_dm`、`musi_dm`、`nwdy_dm`、`nxys_dm`、`qldl_dm`、`rongyao_dm`、`sgyy_dm`、`shby_dm`、`shuanghui_dm`、`xxzd_dm`、`zjark_1_dm`、`zjct_dm`。

抽样原则：

- `nwdy_dm`：数据量最大、点位最完整，用作“查询能力”主样本。
- `zjct_dm`：有冷量、电费、碳排、配额，用作“费用/碳/配额”样本。
- `xxzd_dm`：告警最多，用作“告警”样本。
- `rongyao_dm`、`shuanghui_dm`、`jagk_dm`：交叉验证租户差异。

### 1.2 调研方法

全部通过 `archery` 技能的 `archery_api.py` 执行，只读：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\2025\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$S  = "C:\Users\2025\.dsh\skills\archery\archery_api.py"

& $py $S dbs   "数智能效saas现网-doris"
& $py $S tables "数智能效saas现网-doris" nwdy_dm
& $py $S desc  "数智能效saas现网-doris" nwdy_dm dm_eo_e_meter_di
& $py $S query "数智能效saas现网-doris" nwdy_dm "SELECT ..." --limit 500
```

纪律：

- 查陌生表先 `desc`，字段语义以 `COLUMN_COMMENT` 为准。
- 只允许 `SELECT/SHOW/DESC/EXPLAIN`，不做任何写操作。
- 所有统计带时间窗、租户/项目过滤和 `LIMIT`，不在生产拉全表。
- 本文数字是 2026-11-18 12:30 左右的快照，后续会随数据同步变化。

### 1.3 样本租户规模

| 租户库 | 项目 | 表数 | 表计日记录 | 小时记录 | 设备点位 | 控制记录 | 告警 |
|---|---|---:|---:|---:|---:|---:|---:|
| `nwdy_dm` | 南网能源-岭誉广场 | 187 | 49,527 | 1,365,463 | 65,667,882 | 95,520 | 0 |
| `zjct_dm` | 中交城投总部大厦 | 192 | 3,110 | — | — | — | 8 |
| `xxzd_dm` | 众德一厂 | 187 | 3,793 | — | — | — | 52,539 |
| `rongyao_dm` | 荣耀（样本） | 187 | 8,536 | — | — | — | 15,809 |
| `shuanghui_dm` | 双汇（样本） | 192 | 9,168 | — | — | — | 968 |
| `jagk_dm` | 机场（样本） | 192 | 7,195 | — | — | — | 959 |

> 说明：`—` 表示本次未逐表统计，不代表没有数据；表结构在所有租户库一致。

---

## 2. Doris 数据全景

### 2.1 分层与表数量

`nwdy_dm` 共 187 张表：

| 前缀 | 数量 | 含义 | 典型表 | 使用建议 |
|---|---:|---|---|---|
| `dm_` | 95 | 数据集市/汇总层，日/月/年粒度 | `dm_eo_e_meter_di`、`dm_eo_e_meter_hi`、`dm_eo_s_space_di` | **Agent 首选**，已预聚合 |
| `dim_` | 38 | 维度/字典/台账，个别表实际是事实 | `dim_eo_m_meter`、`dim_eo_z_space`、`dim_eo_alarms` | 实体解析、告警查询 |
| `algo_` | 22 | 冷站算法结果、预测、调度、模型 | `algo_overview_kpi`、`algo_load_forecast_day_ahead` | 有部署才有数据 |
| `dwr_` | 15 | 宽表，部分是按天重建的快照 | `dwr_eo_energy_meter_wide` | 慎用，先确认是否历史 |
| `dwi_` | 8 | 明细层，点位/用量明细 | `dwi_eo_d_point_data`、`dwi_eo_energy_point_data` | 大数据量，必须聚合 |
| `eo_` | 6 | 运行时/控制/抄表记录 | `eo_device_control_task`、`eo_device_run_record` | 控制与运行分析 |
| 其他 | 3 | `DATABASECHANGELOG`、`DATABASECHANGELOGLOCK`、`temp` | — | 忽略 |

### 2.2 数据域划分

| 数据域 | 核心表 | 能回答的问题 |
|---|---|---|
| 能耗 | `dm_eo_e_meter_di`、`dm_eo_e_meter_hi`、`dm_eo_s_space_di` | 用了多少电、趋势、排名、空间/表计对比 |
| 表计 | `dim_eo_m_meter`、`dim_eo_m_meter_measuring_space`、`dm_eo_meter_reading_record` | 有哪些表、装在哪、抄表记录、数据来源 |
| 空间 | `dim_eo_z_space`、`dim_eo_e_space`、`dim_eo_e_zone` | 空间树、楼层/房间、区域 |
| 设备 | `dim_eo_d_device`、`dim_eo_d_device_category`、`dwi_eo_d_point_data` | 有哪些设备、什么类型、实时点位 |
| 控制 | `eo_device_control_task`、`eo_device_run_record` | 谁在何时控制了什么、成功失败、运行段 |
| 告警 | `dim_eo_alarms`、`dm_eo_alarm_trigger_record` | 告警类型、级别、状态、设备、恢复时间 |
| 费用 | `dim_eo_energy_price`、`dm_eo_e_meter_di.energy_expense` | 电价、电费（视租户配置） |
| 定额 | `dim_eo_energy_quota_rules`、`dm_eo_alarm_trigger_record` | 定额规则、超限记录 |
| 碳排 | `dm_eo_c_measure_emission_di`、`dm_eo_c_measure_category_*` | 排放源、排放因子、排放量 |
| 算法 | `algo_overview_kpi`、`algo_load_forecast_day_ahead`、`algo_intraday_*`、`algo_day_ahead_schedule` | 冷站 KPI、负荷预测、控制效果、调度 |
| 环境 | `dm_eo_z_env_index_de`、`dwr_eo_z_zone_temp_value_de` | 温湿度、CO₂、舒适度 |
| 日历 | `dim_day` | 工作日/休息日/节假日 |

---

## 3. 核心表结构与数据量

### 3.1 `dm_eo_e_meter_di`：表计日用量（Agent 主表）

粒度：`tenant_id + project_id + meter_id + data_time + energy_type`，一行 = 一块表一天。

| 字段 | 类型 | 说明 |
|---|---|---|
| `tenant_id` / `tenant_name` | bigint / varchar | 租户 |
| `project_id` / `project_name` | bigint / varchar | 项目 |
| `application_id` | bigint | 应用/菜单 ID，不是物理系统 |
| `meter_id` / `meter_name` / `meter_number` | bigint / varchar | 表计 |
| `energy_type` | varchar | `electric`、`cold` 等 |
| `data_time` | date | 日期 |
| `energy_value` | decimal | 日用量（kWh 等） |
| `energy_expense` | decimal | 日费用，可能为 0 |
| `energy_price` | decimal | 单价，可能为 0 |
| `energy_tb` | decimal | 同比值，样本为空 |
| `chain_ratio` | decimal | 环比值，样本仅 3 行 |
| `energy_saving_consumption` | decimal | 节能量，样本全空 |
| `energy_saving_cost` | decimal | 节约费用，样本全空 |
| `meter_attribute` | tinyint | 1 自用、2 公摊、3 总分 |
| `space_code` / `space_path` / `space_name` | bigint / varchar | 计量空间 |
| `measuring_device_category_id` / `_name` | bigint / varchar | 计量设备分类 |
| `spec_enum` | varchar | 规格枚举，如 `DUO_LIAN_JI_NEI_JI_NENG_HAO` |
| `rate` | decimal | 倍率 |

`nwdy_dm` 实测：

- 49,527 行，761 块表，231 个自然日跨度，但**每块表实际只有约 65 天**（2026-09-15 至 2026-11-18）。
- 3 块表有 2026-04 至 2026-07 的零散数据（86 天），所以全局 `MIN(data_time)` 看起来是 2026-04-01。
- 全部 `energy_type = electric`，全部 `spec_enum = DUO_LIAN_JI_NEI_JI_NENG_HAO`。
- 全部 `meter_attribute = 1`，即自用表，没有公摊/总分表。
- 4 个 `space_code`：`10001` A1座（49,269 行）、`1000100001` 35层、`1000100002` 36层、`1000100003` 37层（各 86 行）。
- 总电量 376,627.10 kWh；`energy_expense = 0`；`energy_saving_consumption` 全空；`energy_tb` 全空；`chain_ratio` 仅 3 行非空。

**结论**：这张表是“多联机内机日电量”，不是“全楼总能耗”；不能拿它算同比、节能率、电费（在 `nwdy_dm` 里）。

### 3.2 `dm_eo_e_meter_hi`：表计小时用量

粒度：`meter_id + data_time`（小时）。

- 1,365,463 行，908 块表，163 天（2026-06-09 至 2026-11-18 12:00）。
- 24 个小时都有数据，但每块表平均每天只有约 9.2 条小时记录，存在明显缺口，不能假设 24 小时完整。
- 总电量 366,383.03 kWh；`energy_expense` 仅 725.20 元。
- 适合做小时曲线、峰谷、夜间运行分析；做总量时优先用日表。

### 3.3 `dm_eo_s_space_di`：空间日用量（有重复，慎用）

粒度名义上是 `space_code + data_time + energy_type`，但实测**同一天同一个空间有 4 行重复**。

2026-11-17 样本：

| space_code | space_name | parent_code | energy_value |
|---|---|---|---:|
| 10001 | A1座 | null | 4,679.53 |
| 10001 | A1座 | null | 4,679.53 |
| 10001 | 南网能源 | null | 4,679.53 |
| 10001 | A1座 | 0 | 4,679.53 |

同日 `dm_eo_e_meter_di` 合计也是 4,679.53 kWh。空间表 4 行重复，直接 `SUM` 会得到 18,718.12，放大 4 倍。

另外，`space_name` 也存在别名（同一条记录可能叫“A1座”或“南网能源”），**展示名以 `dim_eo_z_space.name` 为准**，`dm_eo_s_space_di.space_name` 只作参考；去重时按 `space_code, data_time, energy_type, spec_enum` 分组，取 `MAX(energy_value)`，不要按名称分组。

**处理**：按 `space_code, data_time, energy_type, spec_enum` 分组，`MAX(space_name)`、`MAX(energy_value)` 去重；或者 `SELECT DISTINCT` 后再聚合。空间表只适合做“空间维度参考”，总量以表计日表为准。

### 3.4 `dwi_eo_d_point_data`：设备点位明细（最大表）

粒度：`device_id + point_code + data_time`。

- 65,667,882 行，1,100 台设备，47 种点位，2026-03-31 14:05 至 2026-11-18 12:15。
- 主要点位（按行数）：

| point_code | 含义（推断） | 行数 | 设备数 | 均值 |
|---|---|---:|---:|---:|
| `ModeSp` | 模式设定 | 6,800,413 | 209 | 1.02 |
| `FanStartSwitchSp` | 风机启停/风速设定 | 6,800,413 | 209 | 90.76 |
| `RoomTemp` | 室内温度 | 6,800,413 | 209 | 25.07 |
| `RoomTempSp` | 室内温度设定 | 6,800,413 | 209 | 21.92 |
| `WindSpeedSp` | 风速设定 | 5,958,696 | 183 | 2.03 |
| `CoolTempLowSp` | 制冷低温设定 | 5,958,696 | 183 | 14.80 |
| `HotTempHighSp` | 制热高温设定 | 5,958,696 | 183 | 27.40 |
| `FaultAlarm` | 故障告警 | 5,871,766 | 179 | 0.05 |
| `CFKTenp` | 出风口温度 | 5,774,422 | 171 | 11.16 |
| `JNMSSD` | 节能模式设定 | 2,826,606 | 183 | 0.04 |
| `bodyNum` | 人数 | 1,100,742 | 34 | 0.25 |
| `peopleExist` | 是否有人 | 1,100,742 | 34 | 0.08 |
| `temp` / `humidity` / `brightness` | 温湿照度 | 356,111 各 | 11 | 26.72 / 67.98 / 275.95 |
| `PFSum`、`Ia/Ib/Ic`、`Va/Vb/Vc`、`ActivePowerSum` | 电气参数 | 108,574 各 | 3 | — |

**结论**：设备侧数据是这套系统最有价值的部分。可以做室温 vs 设定温度、过冷/过热、模式/风速、故障、 occupancy（34 台）、湿度/照度。但不能把点位行数当设备数，也不能假设每台设备都有全部点位。

### 3.5 `dim_eo_m_meter` 与空间映射

`dim_eo_m_meter`：908 块表，全部 `energy_type = electric`、`meter_attribute = 1`、`meter_status = 2`（正常）。

- 905 块表 `install_space_code` 为空；只有 3 块有值（35/36/37 层内机电表）。
- 只有 3 块表有 `measuring_device_id`，且指向的就是表计自身设备，不是空调面板。
- 因此**表计 → 房间**没有结构化映射；`dm_eo_e_meter_di.space_code` 只有 4 个值，房间级能耗不可得。

`dim_eo_m_meter_measuring_space`：909 行，905 块表映射到 `10001` A1座，3 块表映射到 35/36/37 层。

### 3.6 `dim_eo_z_space`：真正的空间树

- 225 行，`deleted=0` 114 行；`level` 1-4 层；字段 `name`、`code`、`parent_code`、`space_path`、`zone_id`。
- 示例：`A1座/35层`、`总经理办公室1`、`3709党建室`、`大会议室`。
- `dim_eo_e_space` 只有 1 行（A1座），不能当空间树用；`dim_eo_zones` 0 行；`dim_eo_e_zone` 1 行（A2座）。

**结论**：实体解析、空间筛选用 `dim_eo_z_space`；但事实表里的 `space_code` 只有 4 个，空间树和事实表没有完全打通。

### 3.7 `dim_eo_d_device`：设备台账

1,808 台（`deleted=0`）：

| 设备分类 | 数量 |
|---|---:|
| 华立电表 | 1,518 |
| 空调面板 | 183 |
| 智能开关 | 31 |
| 智能空调面板 | 26 |
| 空间感知决策 AI 节点 | 21 |
| 虚拟区域雷达 | 13 |
| 温湿照度传感器 | 11 |
| 电表 | 3 |
| 边缘网关等 | 2 |

- `device_status` 全部为 0，不能用来判断在线/离线。
- 183 台空调面板有房间级 `space_code`（如 `100010000200005` 会议室、`100010000300011` 3709党建室），这是房间级**舒适度/运行**分析的基础。
- 1,518 台电表设备 `space_code` 为空，与表计台账是两套视角。

### 3.8 `dim_eo_alarms`：告警事实表（名字叫 dim）

字段：`alarm_name`、`alarm_source_code`、`alarm_type_id`、`alarm_level`、`alarm_time`、`alarm_recovery_time`、`alarm_status`、`alarm_content`、`alarm_rule`、`alarm_param_context`、`device_id`、`space_code`、`work_order_code`、`deleted`。

字典（以注释为准）：

- `alarm_level`：0 紧急、1 严重、2 一般、3 警告。
- `alarm_status`：0 未处理、1 处理中、2 已处理、3 无需处理、4 误报。

`xxzd_dm` 实测：

- 52,539 条，10 台设备，1 个空间（`space_code` 多为 `?`），2026-06-25 至 2026-11-18。
- 级别 1：169 条；级别 2：52,370 条。
- 状态 0：1,878 条；状态 3：50,661 条。
- Top 告警：空压设备告警 48,163 条、压力过大告警 4,226 条、空压机告警 150 条。

**结论**：能做告警类型/级别/状态/时间/设备统计和恢复时长；空间归因在 `xxzd_dm` 不可用（`space_code = "?"`），设备归因可用（`device_id`）。`dwr_eo_alarm_all_wide`、`dm_eo_alarm_type_di`、`dwi_eo_d_alarm`、`dm_eo_d_alarm_di` 在这些租户都是 0 行，不要用。

### 3.9 `eo_device_control_task`：控制记录

字段：`task_id`、`tenant_id`、`project_id`、`device_id`、`device_name`、`property_code`、`property_value`、`task_type`、`status`、`priority`、`operator`、`create_time`、`update_time`、`error_msg`、`execute_type`、`batch_id`、`timeline`、`ack_message`、`description`、`event_type`、`deleted`。

`nwdy_dm` 实测：

- 95,520 条，2026-06-18 至 2026-11-17。
- `task_type`：0 实时 26 条；2 定时 95,494 条。
- `status`：2 成功 82,825 条；3 失败 12,669 条；实时任务 26 条成功。
- `property_code`：`FanStartSwitchSp` 83,958 条（183 台设备）、`RoomTempSp` 11,558 条（113 台）、`ModeSp` 2 条、`JNMSSD` 2 条、`CoolTempLowSp` 1 条。
- `operator` 多为“系统”，说明以策略/定时控制为主。

**结论**：控制成功率、按设备/属性/时间统计、失败原因（`error_msg`）、批次（`batch_id`）都可做。但没有“控制前/后”的实验对照，不能做因果节能归因。

### 3.10 `dim_eo_energy_price` 与 `dim_eo_energy_quota_rules`

`dim_eo_energy_price` 字段：`energy_type`、`power_direct`、`price`、`start_time`、`end_time`（HHmm）、`segment`（0 总/1 峰/2 谷/3 平/4 尖）、`prices`（JSON）、`use_prices`、`produce_prices`、`spec_enum`。

- `nwdy_dm`：0 行。
- `zjct_dm`：2 行，`price` 列为 0，但 `prices` JSON 为：

```json
[{"price":0.72,"endTime":2400,"priceType":1,"startTime":0}]
```

- 因此**不要只看 `price` 列**；要读 `prices` JSON，并在 Python 侧解析（Doris SQL 解析 JSON 数组较麻烦）。
- `zjct_dm` 电费 628,724.59 元，说明费用已在 ETL 里算好；`energy_expense` 可用。

`dim_eo_energy_quota_rules` 字段：`name`、`energy_type`、`quota_object_type`、`quota_object_name`、`quota_threshold_period`、`quota_threshold_type`、`quota_threshold_value`、`volume`。

- `zjct_dm` 4 条：如“1号冷机电表”日定额 10、“中交-冷源-冷却塔2号电表”日定额 13、“中交城投总部大厦-B1F”月定额 15,300、实际 39,184.90。
- 可做定额超限、实际 vs 定额；但不是所有租户都配置。

### 3.11 `algo_*`：冷站算法（样本为空）

表结构齐全，包括：

- `algo_overview_kpi`：干/湿球温度、当前负荷、在运主机数、系统 SCOP、今日基准/实际能耗、节能率、算法状态、模型数。
- `algo_load_forecast_day_ahead`：`target_date + hour_slot`，预测/实际负荷、MAE/MAPE/RMSE。
- `algo_intraday_opt_kpi`：日内累计能耗、节能率、预测 MAE/MAPE、COP 当前/基准/提升率、主机/辅机累计电耗。
- `algo_intraday_control_effect`：15 分钟控制效果、冷机组合、预测/实际负荷、主机/辅机预测/实际电耗、总误差、规则结果、状态。
- `algo_day_ahead_schedule`：日前调度、干湿球、冷负荷、总能耗、`confirm_status`（PENDING/CONFIRMED_ALGO/CONFIRMED_CUSTOM）、确认人/时间。
- `algo_model_registry`、`algo_model_update_record`：模型注册/更新。

但 `nwdy_dm`、`zjct_dm`、`xxzd_dm`、`rongyao_dm`、`shuanghui_dm`、`jagk_dm` 的 `algo_overview_kpi` 均为 0 行；`天河机关大院-doris`、`龙岗艺术中心-doris` 连 `algo_*` 表都没有。**结论：算法能力是“结构存在、部署后才有数据”，Agent 必须运行时探测，不能默认可用。**

### 3.12 `dm_eo_c_measure_emission_di`：碳排

`zjct_dm` 108 行，2026-08 至 2026-10，字段：`date`、`emission_name`、`measure_value`、`measure_unit`、`factor_name`、`factor_value`、`emission_value`、`unit`、`yoy_rate`、`yoy_value`、`day_key/month_key/year_key`。

样本：电力排放源，5,164.16 kWh × 0.4419 = 2,282.04 kgCO₂。可做碳排总量/趋势/因子拆解；但只有配置了碳排的租户才有。

### 3.13 `dm_eo_meter_reading_record`：抄表记录

15,078,806 行，2026-04-01 至 2026-11-18 12:20，908 块表，`data_source = 0`（自动）全部，操作人 1 个。字段包含 `reading_time`、`measuring_space_code/path`、`data_source`、`operator`、`remark`、`spec_enum` 等。

注意：这张表**没有读数**，只有抄表元数据；读数在 `dwi_eo_energy_point_data`（`point_code = bm`，累计表码）。做“抄表异常/人工抄表”分析用它；做用量分析用日/小时表。

---

## 4. 数据质量与关键陷阱

这一节是 Agent 设计的基础。每条都对应一个必须写进语义层/模板的规则。

### 4.1 空间日表 4 倍重复

- 现象：`dm_eo_s_space_di` 同一天同一空间 4 行相同电量，`space_name` 可能是“A1座”“南网能源”等不同别名。
- 证据：2026-11-17，4 行均为 4,679.53 kWh；同日表计日表合计 4,679.53 kWh。
- 规则：空间表必须按 `space_code, data_time, energy_type, spec_enum` 去重；总量以 `dm_eo_e_meter_di` 为准。

### 4.2 两个“宽表”是当日快照，不是历史

- `dwr_eo_energy_meter_wide`：758 行，只有 2026-11-18。
- `dwi_eo_energy_usage`：758 行，只有 2026-11-18。
- 规则：不要用它们做趋势/同期对比；历史用 `dm_eo_e_meter_di`（日）和 `dm_eo_e_meter_hi`（小时）。

### 4.3 日粒度只有约 65 天

- 757 块表只有 65 天（2026-09-15 至 2026-11-18），1 块 64 天，3 块 86 天（2026-04 至 2026-07 零散）。
- 规则：不能做同比；环比最多做近 2 个月；所有趋势结论必须带“数据窗口”说明。

### 4.4 同比/环比/节能字段基本没算

- `energy_tb` 0 行非空；`chain_ratio` 仅 3 行；`energy_saving_consumption` 0 行；`energy_saving_cost` 0 行。
- 规则：不要相信这些字段；要算就自己用历史数据算，算不了就明确说“数据不足”。

### 4.5 费用/电价因租户而异

- `nwdy_dm`：电价表 0 行，`energy_expense` 全 0，`energy_price` 全 0。
- `zjct_dm`：电价 JSON 0.72 元/kWh，电费 628,724.59 元。
- 规则：先探测 `dim_eo_energy_price` 行数、`energy_expense` 非零行数；没有就回答“未配置电价/费用”，不能返回 0 元。

### 4.6 基准值表为空，节能率不可算

- `dim_eo_energy_standard_value` 在所有抽样租户均为 0 行。
- `energy_saving_consumption` 全空。
- 规则：不做“节能量/节能率/节约电费”的量化承诺；只能做“同比/环比/单位面积”描述，且同比也没有历史。

### 4.7 `dim_eo_e_space` 不是空间树

- `dim_eo_e_space` 只有 1 行；`dim_eo_z_space` 225 行（114 有效）。
- 规则：实体解析用 `dim_eo_z_space`；事实表 `space_code` 只有 4 个，空间树和事实表未完全打通。

### 4.8 `dim_eo_a_application` 是菜单，不是物理系统

- 37 行，`app_type` 0 目录 / 1 菜单；`1205 = 表计管理`。
- 规则：不要把 `application_id` 解释为“空调系统/照明系统”；它主要用于数据权限/菜单归属。

### 4.9 设备表把电表也算设备

- 1,808 台设备里 1,518 台是“华立电表”，183 台空调面板。
- `device_status` 全 0，不能判断在线/离线。
- 规则：设备数要按 `device_category_name` 过滤；在线状态用点位新鲜度推断（最近 N 分钟有无数据）。

### 4.10 告警表选择错误

- `dwr_eo_alarm_all_wide`、`dm_eo_alarm_type_di`、`dwi_eo_d_alarm`、`dm_eo_d_alarm_di` 在样本租户均为 0 行。
- 真实告警在 `dim_eo_alarms`。
- 规则：告警查询用 `dim_eo_alarms`；空间字段可能是 `"?"`，设备字段可用。

### 4.11 运行段表为空

- `eo_device_run_record` 在 `nwdy_dm` 为 0 行。
- 规则：不要依赖运行段；用点位（`FanStartSwitchSp`/`ModeSp`）推断启停，或直接分析控制记录。

### 4.12 点位表不是“每台设备全点位”

- 47 种点位、1,100 台设备；室温/设定温度只有 209 台， occupancy 只有 34 台，温湿度只有 11 台。
- 规则：每次分析先 `COUNT(DISTINCT device_id)`，把“有数据的设备数”写进结论。

### 4.13 原始表码不是用量

- `dwi_eo_energy_point_data` 的 `point_code = bm` 是累计表码，`segment` 全 0，`power_direct` 全 1。
- 规则：不要直接 `SUM(point_value)` 当电量；用量用 `dm_eo_e_meter_di/hi`。

### 4.14 空间层级可能重复计算

- `A1座` 与 `35/36/37层` 同时存在；A1座行可能已含楼层，也可能不含，取决于 ETL 口径。
- 规则：空间聚合先选层级，不要“父 + 子”直接相加；默认按 `space_code` 去重后，只取目标层级。

### 4.15 时间新鲜度不一致

- 点位最新 2026-11-18 12:15；小时表 12:00；日表 2026-11-18；控制记录到 2026-11-17 22:04。
- 规则：每个回答标注 `data_time`/`max_data_time`；跨表分析时用最旧的新鲜度。

---

## 5. 能查询什么

### 5.1 查询能力清单

| # | 能力 | 依赖表 | 示例问题 | 可用性 |
|---|---|---|---|---|
| Q1 | 期间总能耗 | `dm_eo_e_meter_di` | 昨天用了多少电？ | ✅ `nwdy_dm` |
| Q2 | 日/小时趋势 | `dm_eo_e_meter_di` / `_hi` | 近 30 天用电趋势？ | ✅ 日 65 天、小时 163 天 |
| Q3 | 表计排名 | `dm_eo_e_meter_di` | 哪块表用电最多？ | ✅ |
| Q4 | 空间排名 | `dm_eo_s_space_di`（去重） | 哪个楼层用电最多？ | ⚠️ 仅 4 个空间 |
| Q5 | 分项/分类 | `spec_enum`、`energy_type` | 空调/照明各多少？ | ⚠️ 仅 1 类多联机内机 |
| Q6 | 表计台账 | `dim_eo_m_meter` | 有多少块表？哪些异常？ | ✅ |
| Q7 | 空间台账 | `dim_eo_z_space` | A1座有哪些房间？ | ✅ 114 个有效空间 |
| Q8 | 设备台账 | `dim_eo_d_device` | 有多少空调面板？ | ✅ 183 台 |
| Q9 | 设备最新点位 | `dwi_eo_d_point_data` | 36 层会议室现在室温多少？ | ✅ 209 台有室温 |
| Q10 | 室温 vs 设定 | `RoomTemp` / `RoomTempSp` | 哪些房间过冷？ | ✅ |
| Q11 | 控制记录 | `eo_device_control_task` | 昨天控制成功率？ | ✅ 9.5 万条 |
| Q12 | 告警统计 | `dim_eo_alarms` | 空压机告警多少？ | ⚠️ 视租户 |
| Q13 | 费用 | `energy_expense` / `prices` | 上月电费多少？ | ⚠️ 视租户 |
| Q14 | 定额 | `dim_eo_energy_quota_rules` | 哪些表超定额？ | ⚠️ 视租户 |
| Q15 | 碳排 | `dm_eo_c_measure_emission_di` | 本月碳排多少？ | ⚠️ 视租户 |
| Q16 | 算法 KPI | `algo_overview_kpi` 等 | 昨天节能率多少？ | ⚠️ 部署后可用 |
| Q17 | 数据质量 | 多表 | 哪些表缺数据？ | ✅ |

### 5.2 典型问题的 SQL 口径

**“昨天总用电”**

```sql
SELECT
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters
FROM nwdy_dm.dm_eo_e_meter_di
WHERE tenant_id = 1026
  AND project_id = 1
  AND data_time = '2026-11-17'
  AND energy_type = 'electric';
```

预期：4,679.53 kWh，758 块表。

**“近 30 天日趋势”**

```sql
SELECT
  data_time,
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters
FROM nwdy_dm.dm_eo_e_meter_di
WHERE tenant_id = 1026
  AND project_id = 1
  AND data_time BETWEEN '2026-10-20' AND '2026-11-18'
  AND energy_type = 'electric'
GROUP BY data_time
ORDER BY data_time;
```

**“表计排名 Top 10”**

```sql
SELECT
  meter_id,
  meter_name,
  SUM(energy_value) AS energy_value
FROM nwdy_dm.dm_eo_e_meter_di
WHERE tenant_id = 1026
  AND project_id = 1
  AND data_time BETWEEN '2026-11-01' AND '2026-11-18'
  AND energy_type = 'electric'
GROUP BY meter_id, meter_name
ORDER BY energy_value DESC
LIMIT 10;
```

**“36 层会议室当前室温/设定温度”**

```sql
SELECT
  t.device_id,
  MAX(CASE WHEN t.point_code = 'RoomTemp'   THEN t.point_value END) AS room_temp,
  MAX(CASE WHEN t.point_code = 'RoomTempSp' THEN t.point_value END) AS room_temp_sp,
  MAX(t.data_time) AS data_time
FROM nwdy_dm.dwi_eo_d_point_data t
WHERE t.device_id IN (/* 设备 ID 列表 */)
  AND t.data_time >= '2026-11-18 11:00:00'
  AND t.point_code IN ('RoomTemp', 'RoomTempSp')
GROUP BY t.device_id;
```

**“近 7 天控制成功率”**

```sql
SELECT
  status,
  COUNT(*) AS cnt,
  ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) AS pct
FROM nwdy_dm.eo_device_control_task
WHERE tenant_id = 1026
  AND project_id = 1
  AND create_time >= '2026-11-11 00:00:00'
GROUP BY status;
```

**“告警按级别/状态”**

```sql
SELECT
  alarm_level,
  alarm_status,
  COUNT(*) AS cnt
FROM xxzd_dm.dim_eo_alarms
WHERE tenant_id = 1025
  AND project_id = 1
  AND deleted = 0
  AND alarm_time >= '2026-11-01 00:00:00'
GROUP BY alarm_level, alarm_status
ORDER BY alarm_level, alarm_status;
```

---

## 6. 能分析什么

### 6.1 可做的分析

| # | 分析 | 方法 | 数据要求 | 结论强度 |
|---|---|---|---|---|
| A1 | 趋势 | 日/小时序列、移动平均 | ≥14 天 | 描述性 |
| A2 | 环比 | 本期 vs 上期，按表/空间/时段 | 两期完整 | 描述性 |
| A3 | 排名/贡献度 | Top N、帕累托 | 表计/空间 | 描述性 |
| A4 | 用能结构 | 按 `spec_enum`/`energy_type` | 多类别 | 描述性（样本仅 1 类） |
| A5 | 异常检测 | 同比/环比 z-score、IQR | ≥14 天，≥10 个样本 | 统计异常，不是故障 |
| A6 | 舒适度 | `RoomTemp - RoomTempSp`，过冷/过热时长 | 室温+设定 | 描述性 |
| A7 | 非工作时段运行 | 结合 `dim_day.day_type` + 小时点位/控制 | 小时数据 | 描述性 |
| A8 | 控制效果 | 控制前/后 30 分钟室温/设定对比 | 控制记录+点位 | 前后对比，非因果 |
| A9 | 控制失败分析 | `status=3`、`error_msg`、设备/属性/批次 | 控制记录 | 描述性 |
| A10 | 告警分析 | 类型/级别/状态/恢复时长 | `dim_eo_alarms` | 描述性 |
| A11 | 数据质量 | 缺表日、零值、重复、点位陈旧 | 多表 | 确定性 |
| A12 | 碳排核算 | `measure_value × factor_value` | 碳排表 | 描述性 |
| A13 | 算法效果 | `algo_*` KPI/预测误差 | 算法表有数据 | 描述性 |

### 6.2 不能做的分析（硬边界）

| 分析 | 为什么不能 | 需要补什么 |
|---|---|---|
| 同比 | 日表只有约 65 天，2025 年无数据 | 历史归档/数据湖 |
| 节能率/节能量 | 基准值表 0 行，`energy_saving_consumption` 全空 | 基准模型/基线表 |
| 节约电费 | 多数租户电价 0 或未配置 | 电价配置+基线 |
| 房间级能耗 | 表计只映射到 4 个空间，905 块表在 A1座 | 表计-房间映射 |
| 全楼总能耗 | 只有多联机内机表，没有总表/其他分项 | 总表/分项计量 |
| 峰谷电量 | 小时表有，但电价时段配置不完整；`segment` 不可靠 | 完整电价时段 |
| 天气归一 | 无独立天气表；算法表才有干湿球 | 天气数据源 |
| 生产单耗 | 无产量/产值数据 | 生产系统对接 |
| 建筑 occupancy 归一 | 仅 34 台设备有 `peopleExist` | 全楼层 occupancy |
| 工单闭环 | 告警只有 `work_order_code`，无工单明细 | 工单系统 |
| 设备在线率 | `device_status` 全 0 | 点位新鲜度规则 |
| 实时控制 | 只读，不写 | 控制审批链路 |

### 6.3 分析示例：过冷/过热

口径：

- 仅对同时有 `RoomTemp` 和 `RoomTempSp` 的设备。
- 过冷：`RoomTemp < RoomTempSp - 1.5°C`，持续 ≥30 分钟。
- 过热：`RoomTemp > RoomTempSp + 1.5°C`，持续 ≥30 分钟。
- 统计“设备-日”维度的过冷/过热时长占比。

这不是“节能”，只是“舒适度/设定合理性”诊断；不要直接换算成节能量。

### 6.4 分析示例：非工作时段运行

- 用 `dim_day` 判断 `day_type`（0 节假日、1 工作日、2 休息日）。
- 工作日 19:00-07:00、休息日全天，若 `FanStartSwitchSp` 为开/风速 > 0，记为“非工作时段运行”。
- 结合控制记录判断是“策略允许”还是“异常常开”；没有策略表，只能标注“疑似”。

---

## 7. 能力边界

### 7.1 能力矩阵

图例：✅ 大多数租户可用；⚠️ 依赖租户配置/数据填充；❌ 当前数据不支持。

| 能力 | 状态 | 证据/原因 | Agent 应该怎么答 |
|---|---|---|---|
| 总能耗/期间能耗 | ✅ | `nwdy_dm` 758 表×65 天 | 给数值+表数+时间窗 |
| 日趋势 | ✅ | 65 天 | 给趋势+窗口 |
| 小时趋势 | ⚠️ | 163 天，但每表每天约 9.2 条 | 给曲线+“有缺口” |
| 环比 | ⚠️ | 周环比可用；9 月只有下半月，月环比不完整 | 给两期+变化率+窗口 |
| 同比 | ❌ | 无 2025 数据 | 明确拒答 |
| 表计排名 | ✅ | 761 块表 | 给 Top N |
| 空间排名 | ⚠️ | 仅 4 个空间，空间表重复 | 先去重，标注粒度 |
| 分项能耗 | ⚠️ | 仅 `DUO_LIAN_JI_NEI_JI_NENG_HAO` 一类 | 说明只有多联机内机 |
| 房间级能耗 | ❌ | 905 块表映射到 A1座，无房间映射 | 明确拒答 |
| 全楼总能耗 | ❌ | 无总表/照明/插座分项 | 说明口径 |
| 设备台账 | ✅ | 1,808 台 | 按分类给 |
| 设备在线状态 | ❌ | `device_status` 全 0 | 用点位新鲜度替代 |
| 室温/设定温度 | ✅ | 209 台 | 给设备数+时间 |
|  occupancy | ⚠️ | 34 台有 `peopleExist/bodyNum` | 只覆盖部分区域 |
| 控制记录/成功率 | ✅ | 95,520 条 | 给成功率+失败原因 |
| 控制效果 | ⚠️ | 可做前后对比，无对照 | 标注“非因果” |
| 告警统计 | ⚠️ | 部分租户有 `dim_eo_alarms` | 先探测行数 |
| 费用 | ⚠️ | `nwdy_dm` 0，`zjct_dm` 62.8 万 | 先探测电价/费用 |
| 定额 | ⚠️ | `zjct_dm` 4 条 | 先探测 |
| 碳排 | ⚠️ | `zjct_dm` 108 行 | 先探测 |
| 算法 KPI | ⚠️ | 样本 0 行 | 探测 `algo_overview_kpi` |
| 天气 | ❌ | 无独立天气表 | 明确拒答 |
| 生产单耗 | ❌ | 无产量 | 明确拒答 |
| 工单闭环 | ❌ | 无工单明细 | 明确拒答 |
| 节能率/节能量 | ❌ | 无基准值 | 明确拒答 |
| 实时控制 | ❌ | 只读 | 引导到控制台 |

### 7.2 边界话术

- **无数据**：“当前租户未配置电价（`dim_eo_energy_price` 0 行），无法计算电费；可以给电量。”
- **数据不足**：“日粒度只有 2026-09-15 至 2026-11-18，无法做同比；可以做近 30 天环比。”
- **口径不符**：“系统计量的是多联机内机电量，不是全楼总电量；全楼总能耗需要总表数据。”
- **不可归因**：“控制前后室温有变化，但没有对照组，不能归因为控制策略的节能效果。”
- **超范围**：“该问题需要天气/产量数据，当前 Doris 中没有，建议接入后再分析。”

### 7.3 租户能力探测

Agent 每次会话/每个问题前，跑一次轻量 `capability_probe`：

```sql
SELECT 'meter_di' AS t, COUNT(*) AS c, MAX(data_time) AS max_dt FROM dm_eo_e_meter_di
UNION ALL SELECT 'meter_hi', COUNT(*), MAX(data_time) FROM dm_eo_e_meter_hi
UNION ALL SELECT 'point', COUNT(*), MAX(data_time) FROM dwi_eo_d_point_data
UNION ALL SELECT 'alarm', COUNT(*), MAX(alarm_time) FROM dim_eo_alarms
UNION ALL SELECT 'control', COUNT(*), MAX(create_time) FROM eo_device_control_task
UNION ALL SELECT 'price', COUNT(*), NULL FROM dim_eo_energy_price
UNION ALL SELECT 'quota', COUNT(*), NULL FROM dim_eo_energy_quota_rules
UNION ALL SELECT 'carbon', COUNT(*), MAX(date) FROM dm_eo_c_measure_emission_di
UNION ALL SELECT 'algo', COUNT(*), MAX(data_time) FROM algo_overview_kpi;
```

结果缓存 10 分钟，作为 `capabilities` 传给规划器。

---

## 8. Agent 总体架构

### 8.1 架构图

```text
┌────────────────────────────────────────────────────────────────────┐
│  Web / IM / 大屏                                                   │
│  POST /agent/chat  (SSE)                                           │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │  FastAPI Gateway      │
                    │  - 鉴权/租户上下文     │
                    │  - 限流/审计           │
                    └───────────┬───────────┘
                                │
          ┌─────────────────────▼─────────────────────┐
          │  Agent Orchestrator                        │
          │  1. Intent + Slot (DeepSeek flash)         │
          │  2. Entity Resolver (空间/设备/表计)        │
          │  3. Capability Probe (数据可用性)           │
          │  4. Query Planner (模板选择)                │
          │  5. Analysis Engine (确定性计算)            │
          │  6. Narrator (DeepSeek)                    │
          │  7. Guard ( grounding / 边界 )              │
          └───────┬───────────────────────┬────────────┘
                  │                       │
      ┌───────────▼───────────┐   ┌───────▼────────────┐
      │ Doris ReadOnly Gateway│   │ Archery Gateway     │
      │ - pymysql/连接池       │   │ - 建模期 profiling  │
      │ - 只读账号             │   │ - 临时查询/审计      │
      │ - 超时/行数限制        │   │ - 可选运行时        │
      └───────────┬───────────┘   └───────┬────────────┘
                  │                       │
          ┌───────▼───────────────────────▼────────────┐
          │ Doris {tenant}_dm                          │
          │ dm_eo_e_meter_di / _hi / dwi_point / ...   │
          └────────────────────────────────────────────┘

  旁路任务：
  - CatalogSyncJob：Archery → catalog.yaml
  - ProfileJob：行数/填充率/重复/新鲜度 → data_quality
  - EvalJob：60 题回归 → 准确率报告
```

### 8.2 组件职责

| 组件 | 职责 | 技术 |
|---|---|---|
| API Gateway | SSE、鉴权、租户上下文、限流 | FastAPI + `sse-starlette` |
| Intent/Slot | 识别指标、实体、时间、维度、比较 | DeepSeek flash，JSON 输出 |
| Entity Resolver | 空间/设备/表计名称 → ID | `dim_eo_z_space` / `dim_eo_d_device` / `dim_eo_m_meter` |
| Capability Probe | 表/字段/行数/新鲜度探测 | SQL + 缓存 |
| Query Planner | 指标 → 模板 + 参数 | 规则 + 少量 LLM |
| Query Gateway | 执行 SQL、超时、限流、审计 | Doris / Archery |
| Analysis Engine | 趋势、环比、排名、舒适度、控制、告警 | Python/pandas |
| Narrator | 把结构化结果写成中文结论 | DeepSeek flash / v4-pro |
| Guard | 数值 grounding、边界、PII、SQL 白名单 | 规则 + 校验 |
| Catalog/Profile Job | 同步表结构、数据质量 | Archery skill + 定时任务 |

### 8.3 请求处理流程

1. **鉴权**：从 `X-CONTEXT-TOKEN` 解析 `tenantId/projectId/userId`（沿用网关现有上下文；Agent 不重新做登录）。
2. **租户映射**：`tenantId → tenant_code → {tenant_code}_dm`，只允许配置表里的库名。
3. **意图解析**：DeepSeek flash 输出 JSON：

```json
{
  "intent": "energy_query",
  "metric": "energy_total",
  "time": {"type": "day", "value": "2026-11-17"},
  "entity": {"type": "project", "value": "current"},
  "dimensions": [],
  "compare": null
}
```

4. **实体解析**：如果用户说“36 层会议室”，先查 `dim_eo_z_space` / `dim_eo_d_device`，得到 ID 列表；多个候选就追问。
5. **能力探测**：检查依赖表是否有数据、时间窗是否覆盖；不满足就走边界话术。
6. **模板选择**：`metric + dimensions + compare` → 模板 ID；只允许注册过的模板。
7. **执行**：Doris 只读连接，参数化，超时 10s，行数上限 500；结果转 DataFrame。
8. **分析**：确定性函数计算环比、贡献度、舒适度、成功率等。
9. **叙述**：LLM 只使用结构化结果和证据，不重新算数。
10. **校验**：数值 grounding（回答里的数字必须在结果里）、边界校验、PII 脱敏。
11. **返回**：`answer + evidence + cards + dataGaps + confidence`。

### 8.4 为什么用 Python/FastAPI

- 团队不想用 Java；Python 与 Doris MySQL 协议、DeepSeek SDK、pandas 分析、Archery 脚本都最顺。
- FastAPI 的 SSE、Pydantic 校验、异步 IO 适合流式回答。
- 单服务即可 PoC；后续可拆 `agent-api`、`query-service`、`catalog-job`。
- 不需要引入向量库；指标定义/模板是结构化配置，不是文档 RAG。

### 8.5 技术栈

| 层 | 选型 |
|---|---|
| Web | FastAPI + Uvicorn + `sse-starlette` |
| 模型 | DeepSeek `deepseek-flash`（意图/叙述）、`deepseek-v4-pro`（复杂诊断） |
| Doris | `pymysql` + `DBUtils` 连接池（或 `aiomysql`） |
| 分析 | pandas + numpy |
| 配置 | Pydantic Settings + YAML |
| 缓存 | Redis（能力探测/结果缓存/会话） |
| 可观测 | structlog + OpenTelemetry + Prometheus |
| 测试 | pytest + httpx + promptfoo |
| 部署 | Docker + K8s（复用现有集群） |

---

## 9. 语义层与 SQL 模板

### 9.1 目录结构

```text
agent/
  catalog/
    instance.yaml          # 实例、租户库白名单
    tables.yaml            # 表/字段/注释/可用性
    metrics.yaml           # 指标定义、模板、单位、边界
    entities.yaml          # 空间/设备/表计同义词
  templates/
    energy.sql
    device.sql
    control.sql
    alarm.sql
    quality.sql
  app/
    main.py
    planner.py
    executor.py
    analysis.py
    narrator.py
    guard.py
```

### 9.2 指标定义示例

```yaml
- id: energy_total
  name: 期间总能耗
  unit: kWh
  source: dm_eo_e_meter_di
  grain: day
  template: energy_total.sql
  required_filters: [tenant_id, project_id, data_time]
  availability:
    table: dm_eo_e_meter_di
    min_rows: 1
    required_columns: [energy_value, meter_id, data_time]
  caveats:
    - 仅统计有数据的表计，需同时返回 active_meters
    - 不等同于全楼总能耗
```

### 9.3 模板契约

每个模板：

- 只含一个 `SELECT`，不允许 `;`、注释、DDL/DML。
- 参数用 `%(name)s`，由驱动绑定，不做字符串拼接。
- 必须包含 `tenant_id`、`project_id`（有则加）、时间范围、`LIMIT`。
- 必须返回 `data_time` 或聚合窗口，便于证据。
- 结果列名稳定，便于前端卡片。

### 9.4 核心模板

#### T01 期间总能耗

```sql
SELECT
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters,
  MIN(data_time) AS start_date,
  MAX(data_time) AS end_date
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_date)s AND %(end_date)s
  AND energy_type = %(energy_type)s
  AND (%(space_codes)s IS NULL OR space_code IN (%(space_codes)s));
```

#### T02 日趋势

```sql
SELECT
  data_time,
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_date)s AND %(end_date)s
  AND energy_type = %(energy_type)s
GROUP BY data_time
ORDER BY data_time
LIMIT 400;
```

#### T03 表计排名

```sql
SELECT
  meter_id,
  meter_name,
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT data_time) AS days
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_date)s AND %(end_date)s
  AND energy_type = %(energy_type)s
GROUP BY meter_id, meter_name
ORDER BY energy_value DESC
LIMIT %(top_n)s;
```

#### T04 空间日用量（去重）

```sql
SELECT
  space_code,
  data_time,
  MAX(space_name) AS space_name,
  MAX(energy_value) AS energy_value,
  MAX(energy_expense) AS energy_expense
FROM {db}.dm_eo_s_space_di
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_date)s AND %(end_date)s
  AND energy_type = %(energy_type)s
GROUP BY space_code, data_time, energy_type, spec_enum
ORDER BY data_time, space_code
LIMIT 1000;
```

> 注意：`spec_enum` 参与分组是为了对齐重复行口径；如果同一空间同一天有多个 `spec_enum`，需要先确认业务含义，不能直接相加。`MAX(space_name)` 可能取到别名（如“南网能源”），前端展示名应优先用 `dim_eo_z_space.name` 校正。

#### T05 小时曲线

```sql
SELECT
  HOUR(data_time) AS hour,
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters
FROM {db}.dm_eo_e_meter_hi
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_time)s AND %(end_time)s
  AND energy_type = %(energy_type)s
GROUP BY HOUR(data_time)
ORDER BY hour;
```

#### T06 设备最新点位

```sql
SELECT device_id, point_code, point_value, data_time
FROM (
  SELECT
    device_id,
    point_code,
    point_value,
    data_time,
    ROW_NUMBER() OVER (
      PARTITION BY device_id, point_code
      ORDER BY data_time DESC
    ) AS rn
  FROM {db}.dwi_eo_d_point_data
  WHERE device_id IN (%(device_ids)s)
    AND data_time >= %(since)s
    AND point_code IN (%(point_codes)s)
) t
WHERE rn = 1
ORDER BY device_id, point_code
LIMIT 500;
```

#### T07 室温 vs 设定温度（期间聚合）

```sql
SELECT
  device_id,
  AVG(CASE WHEN point_code = 'RoomTemp'   THEN point_value END) AS avg_room_temp,
  AVG(CASE WHEN point_code = 'RoomTempSp' THEN point_value END) AS avg_room_temp_sp,
  MAX(CASE WHEN point_code = 'RoomTemp'   THEN point_value END) AS max_room_temp,
  MIN(CASE WHEN point_code = 'RoomTemp'   THEN point_value END) AS min_room_temp,
  COUNT(DISTINCT DATE(data_time)) AS days
FROM {db}.dwi_eo_d_point_data
WHERE device_id IN (%(device_ids)s)
  AND data_time BETWEEN %(start_time)s AND %(end_time)s
  AND point_code IN ('RoomTemp', 'RoomTempSp')
GROUP BY device_id
LIMIT 500;
```

#### T08 控制统计

```sql
SELECT
  task_type,
  status,
  COUNT(*) AS cnt,
  COUNT(DISTINCT device_id) AS devices
FROM {db}.eo_device_control_task
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND create_time BETWEEN %(start_time)s AND %(end_time)s
GROUP BY task_type, status
ORDER BY task_type, status;
```

#### T09 控制失败 Top

```sql
SELECT
  device_name,
  property_code,
  COUNT(*) AS fail_cnt,
  MAX(error_msg) AS last_error
FROM {db}.eo_device_control_task
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND create_time BETWEEN %(start_time)s AND %(end_time)s
  AND status = 3
GROUP BY device_name, property_code
ORDER BY fail_cnt DESC
LIMIT 20;
```

#### T10 告警统计

```sql
SELECT
  alarm_level,
  alarm_status,
  COUNT(*) AS cnt
FROM {db}.dim_eo_alarms
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND deleted = 0
  AND alarm_time BETWEEN %(start_time)s AND %(end_time)s
GROUP BY alarm_level, alarm_status
ORDER BY alarm_level, alarm_status;
```

#### T11 告警 Top 类型

```sql
SELECT
  alarm_name,
  alarm_level,
  COUNT(*) AS cnt,
  COUNT(DISTINCT device_id) AS devices,
  MAX(alarm_time) AS last_time
FROM {db}.dim_eo_alarms
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND deleted = 0
  AND alarm_time BETWEEN %(start_time)s AND %(end_time)s
GROUP BY alarm_name, alarm_level
ORDER BY cnt DESC
LIMIT 20;
```

#### T12 数据质量

```sql
SELECT
  data_time,
  COUNT(DISTINCT meter_id) AS active_meters,
  SUM(CASE WHEN energy_value IS NULL OR energy_value = 0 THEN 1 ELSE 0 END) AS zero_rows,
  SUM(energy_value) AS energy_value
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_date)s AND %(end_date)s
GROUP BY data_time
ORDER BY data_time;
```

#### T13 电价（供 Python 解析 JSON）

```sql
SELECT
  id,
  energy_type,
  power_direct,
  price,
  start_time,
  end_time,
  segment,
  spec_enum,
  prices
FROM {db}.dim_eo_energy_price
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND deleted = 0
LIMIT 100;
```

#### T14 碳排

```sql
SELECT
  date,
  emission_name,
  measure_value,
  measure_unit,
  factor_name,
  factor_value,
  emission_value,
  unit
FROM {db}.dm_eo_c_measure_emission_di
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND date BETWEEN %(start_date)s AND %(end_date)s
ORDER BY date, emission_name
LIMIT 1000;
```

#### T15 算法 KPI

```sql
SELECT
  data_time,
  dry_bulb_temp,
  wet_bulb_temp,
  current_load,
  running_chiller_cnt,
  system_scop,
  today_baseline_kwh,
  today_actual_kwh,
  energy_save_pct,
  algorithm_status
FROM {db}.algo_overview_kpi
WHERE tenant_id = %(tenant_id)s
  AND project_id = %(project_id)s
  AND data_time BETWEEN %(start_time)s AND %(end_time)s
ORDER BY data_time
LIMIT 1000;
```

### 9.5 SQL Guard

- 只允许 `SELECT`；拒绝 `;`、`--`、`/*`、`INTO OUTFILE`、`LOAD DATA`、`SET`、`CALL`。
- `{db}` 必须来自 `tenant_db_map` 白名单，正则 `^[a-z0-9_]+_dm$`。
- `tenant_id/project_id` 由上下文注入，不能由用户文本控制。
- 时间范围必须有上限（默认 366 天）。
- 行数上限 500（图表 5,000，需单独审批）。
- 查询超时 10s；超时返回“查询过重，请缩小范围”。
- 所有 SQL 记录 `sql_hash`，不记录完整 SQL 到普通日志（防泄漏）。

---

## 10. Archery 结合方式

### 10.1 三种模式

| 模式 | 运行时查询 | Archery 角色 | 优点 | 缺点 | 建议 |
|---|---|---|---|---|---|
| A | Doris 只读直连 | 建模/巡检/审计/临时查询 | 低延迟、易扩展 | Agent SQL 不进 Archery 审计 | ✅ 默认 |
| B | Archery `/query/` | 所有查询都走 Archery | 全量审计、权限统一 | 延迟高、100 行限制、会话耦合 | 合规优先时 |
| C | 混合 | 标准指标走 Doris，临时/探索 SQL 走 Archery | 平衡 | 两套通道要维护 | ✅ 推荐落地 |

### 10.2 Archery 在方案中的四个用途

1. **Catalog 同步**：`dbs`/`tables`/`desc` 拉表结构、注释，生成 `catalog.yaml`。
2. **数据 Profiling**：行数、时间范围、填充率、重复、Top 值；生成 `data_quality` 快照。
3. **临时查询/验收**：产品/数据同学用 Archery 复核 Agent 的数字；所有 SQL 留痕。
4. **可选运行时网关**：如果公司要求“所有 SQL 必须经 Archery”，把 `QueryGateway` 切到 `ArcheryGateway`。

### 10.3 Archery 运行时网关的约束

- `archery_api.py` 走网页内部接口，session cookie 认证；并发/会话过期要处理。
- 默认 `limit_num=100`；Agent 需要 500 时要显式传，且 Archery 可能限制。
- 返回 `{columns, rows}`，没有流式；大结果集会慢。
- 所有查询进 Archery 审计，但审计里只有 Archery 账号，没有终端用户；Agent 要自己记录 `user_id + request_id`，把两者关联。
- 建议：**运行时不要把所有问题都打到 Archery**；标准指标走 Doris，只有“非模板/探索性”查询走 Archery，并加人工审批。

### 10.4 推荐落地

```text
标准指标（90%）：Agent → DorisReadOnlyGateway → {tenant}_dm
临时/探索（10%）：Agent → ArcheryGateway → Archery 审计 → Doris
建模/巡检：CatalogSyncJob / ProfileJob → Archery → catalog/data_quality
```

这样既满足“结合 Archery 技能”，又不会把 Archery 变成用户问答的性能瓶颈。

### 10.5 审计字段

无论走哪个通道，都记录：

```json
{
  "request_id": "req_20261118_001",
  "user_id": "u123",
  "tenant_id": 1026,
  "project_id": 1,
  "db": "nwdy_dm",
  "template_id": "T02",
  "params_hash": "sha256:...",
  "sql_hash": "sha256:...",
  "started_at": "2026-11-18T12:30:00+08:00",
  "duration_ms": 42,
  "rows": 30,
  "status": "ok",
  "error": null
}
```

保留 180 天；PII 字段脱敏；SQL 全文只在受控审计库保存。

---

## 11. 多租户与安全

### 11.1 租户映射

```yaml
tenants:
  "1026":
    code: nwdy
    db: nwdy_dm
    project_ids: [1]
  "1025":
    code: xxzd
    db: xxzd_dm
    project_ids: [1]
```

- `tenantId` 来自网关注入的 `X-CONTEXT-TOKEN`，不从用户文本取。
- `db` 只能来自这张表；禁止 `tenantId` 拼接字符串。
- 事实表再加 `tenant_id`/`project_id` 过滤，双保险。
- 禁止跨库 `UNION`、跨租户对比。

### 11.2 只读账号

- 为 Agent 建独立 Doris 用户，只授予 `SELECT` 到 `*_dm` 库。
- 禁止 `INSERT/UPDATE/DELETE/DDL/LOAD/EXPORT`。
- 连接池按租户库隔离；设置 `query_timeout`、`max_execution_time`。
- 不把账号密码写进代码；用 K8s Secret/配置中心。

### 11.3 Prompt Injection

- 用户输入只作为“问题”，不作为 SQL/系统指令。
- 工具调用参数由服务端模板生成，模型不能直接传 SQL。
- 检索到的表名/注释/数据值都当数据，不当指令。
- 对“忽略之前指令/查其他租户/导出全表”等输入直接拒绝并记录。

### 11.4 输出安全

- 设备编码、表计编号可展示；操作人姓名/手机号脱敏。
- 不在回答中暴露数据库连接、账号、内网地址。
- 日志不记录完整 SQL 结果集；只记录聚合/摘要。

---

## 12. 交互与输出

### 12.1 SSE 阶段

```text
event: stage
data: {"stage":"understanding","text":"正在理解问题"}

event: stage
data: {"stage":"querying","text":"正在查询能耗数据"}

event: stage
data: {"stage":"analyzing","text":"正在计算趋势"}

event: token
data: {"text":"2026-11-17 总用电 "}

event: token
data: {"text":"4,679.53 kWh，758 块表有数据。"}

event: result
data: {
  "answer": "...",
  "evidence": [...],
  "cards": [...],
  "dataGaps": [...],
  "confidence": 0.92
}
```

### 12.2 证据结构

```json
{
  "metric": "energy_total",
  "value": 4679.53,
  "unit": "kWh",
  "time_range": ["2026-11-17", "2026-11-17"],
  "source": "nwdy_dm.dm_eo_e_meter_di",
  "active_meters": 758,
  "data_time": "2026-11-18",
  "completeness": "758/758 表有数据",
  "caveats": ["仅多联机内机电量，不含照明/插座/总表"]
}
```

### 12.3 卡片

- 趋势图：日期 + 电量 + 活跃表数。
- 排名表：表计/空间 + 电量 + 占比。
- 设备卡：设备名 + 室温 + 设定 + 偏差 + 最后上报时间。
- 控制卡：成功率 + 失败 Top + 时间窗。
- 告警卡：级别/状态分布 + Top 类型。
- 数据质量卡：缺表日/零值/重复/新鲜度。

### 12.4 回答模板

```text
结论：2026-11-17 总用电 4,679.53 kWh，758 块表有数据。

口径：多联机内机电量，不含照明、插座和总表；空间仅到 A1座/35/36/37 层。
证据：nwdy_dm.dm_eo_e_meter_di，data_time=2026-11-18，758/758 表有数据。
限制：日粒度仅 65 天，无法同比；电价未配置，无法计算电费。
```

### 12.5 追问策略

- 实体歧义：“36 层会议室”有 2 个，问“是 3601 还是 3602？”
- 时间歧义：“最近”默认 7 天，回答里说明。
- 指标歧义：“能耗”默认电量；如果有冷量，问“看电还是冷量？”
- 能力不足：直接给边界 + 可选替代分析。

---

## 13. 评测

### 13.1 题库

60 题，6 类：

| 类别 | 数量 | 示例 |
|---|---:|---|
| 总量/趋势 | 12 | 昨天用了多少电？近 30 天趋势？ |
| 排名/对比 | 10 | 哪块表用电最多？本周 vs 上周？ |
| 设备/舒适度 | 12 | 36 层会议室室温多少？哪些房间过冷？ |
| 控制/告警 | 10 | 昨天控制成功率？空压机告警多少？ |
| 费用/碳/定额 | 6 | 上月电费多少？本月碳排多少？ |
| 边界/拒答 | 10 | 同比去年？节能量多少？全楼总能耗？ |

### 13.2 指标

- 模板选择准确率 ≥ 95%。
- SQL 执行成功率 ≥ 99%（非数据缺失场景）。
- 数值准确率 ≥ 98%（与 Archery 黄金 SQL 对比）。
- 数据缺失识别率 ≥ 90%。
- 边界拒答率 100%（不能编造同比/节能率）。
- P95 延迟 ≤ 5s（Doris 直连）。
- 跨租户查询 0 次。

### 13.3 黄金答案

- 每道题配一条 Archery 黄金 SQL，结果存快照。
- 回归时跑 Agent 与黄金 SQL，比较数值/口径/边界话术。
- 数据更新后，黄金快照按周刷新。

---

## 14. 实施计划

### 14.1 阶段

| 阶段 | 周期 | 交付 |
|---|---|---|
| P0 数据验证 | 3 天 | 本文 + `catalog.yaml` + `metrics.yaml` + 10 条模板 |
| P1 PoC | 2 周 | FastAPI + DeepSeek + Doris 只读 + 20 题 + 趋势/排名/设备卡 |
| P2 MVP | 4 周 | 实体解析、能力探测、控制/告警/数据质量、50 题、SSE、审计 |
| P3 试点 | 4 周 | 2-3 个租户、权限、缓存、限流、监控、评测报告 |
| P4 推广 | 4 周 | 多租户、Archery 网关切换、运维手册、培训 |

### 14.2 人员

- 后端 1 人（FastAPI/Doris/DeepSeek）。
- 数据 1 人（SQL/指标/质量）。
- 前端 0.5 人（卡片/SSE）。
- 运维 0.5 人（K8s/监控/账号）。

### 14.3 首期范围（建议）

- 租户：`nwdy_dm` + `zjct_dm` + `xxzd_dm`。
- 指标：总能耗、日趋势、表计排名、设备室温/设定、控制成功率、告警统计、数据质量。
- 不做：同比、节能率、费用（除 `zjct_dm`）、碳（除 `zjct_dm`）、算法 KPI（除有数据租户）。

---

## 15. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 数据填充率低 | 回答“0”或空 | 能力探测 + 边界话术 |
| 空间表重复 | 总量放大 4 倍 | 模板去重 + 单测 |
| 日表仅 65 天 | 趋势/同比误判 | 回答带窗口 + 拒答同比 |
| 租户差异大 | 同一问题不同结果 | 租户级 capability 配置 |
| 表结构漂移 | 模板失效 | CatalogSyncJob + 模板版本 |
| LLM 编数 | 可信度下降 | 数值 grounding + 模板 |
| Archery 性能/会话 | 延迟/失败 | 默认 Doris，Archery 只做旁路 |
| SQL 注入/越权 | 数据泄漏 | 模板 + 白名单 + 只读账号 |
| 费用/碳口径 | 误导 | 只对有配置租户开放 |

---

## 16. 附录

### 16.1 表分类清单（按域）

**能耗/表计**

`dm_eo_e_meter_di`、`dm_eo_e_meter_hi`、`dm_eo_e_meter_mi`、`dm_eo_e_meter_yi`、`dm_eo_s_space_di`、`dm_eo_s_space_hi`、`dm_eo_s_space_mi`、`dm_eo_s_space_yi`、`dm_eo_meter_reading_record`、`dm_eo_abnormal_meter_reading_record`、`dwr_eo_energy_meter_wide`、`dwi_eo_energy_usage`、`dwi_eo_energy_point_data`。

**空间/组织**

`dim_eo_z_space`、`dim_eo_e_space`、`dim_eo_e_zone`、`dim_eo_zones`、`dim_eo_a_application`、`dim_eo_p_project`、`dim_eo_t_tenant`。

**设备/点位**

`dim_eo_d_device`、`dim_eo_d_device_category`、`dim_eo_d_spec_properties`、`dim_eo_e_device`、`dwi_eo_d_point_data`、`eo_device_config`、`eo_device_his_readings`。

**控制/运行**

`eo_device_control_task`、`eo_device_run_record`、`eo_report_config`、`temp_update_data`。

**告警/定额**

`dim_eo_alarms`、`dm_eo_alarm_trigger_record`、`dm_eo_alarm_type_di`、`dm_eo_d_alarm_di`、`dwr_eo_alarm_all_wide`、`dim_eo_energy_quota_rules`、`dim_eo_energy_quota_rule_configs`。

**费用/碳/环境**

`dim_eo_energy_price`、`dim_eo_energy_standard_value`、`dm_eo_c_measure_emission_di`、`dm_eo_c_measure_category_*`、`dm_eo_c_space_*`、`dm_eo_p_carbon_*`、`dm_eo_z_env_index_de`、`dwr_eo_z_zone_temp_value_de`、`dwr_eo_z_zone_humidity_value_de`、`dwr_eo_z_zone_co2_value_de`、`dwr_eo_z_zone_arofene_value_de`。

**算法**

`algo_overview_kpi`、`algo_overview_running_card`、`algo_overview_running_summary`、`algo_overview_load_curve`、`algo_overview_energy_curve`、`algo_overview_control_effect`、`algo_overview_chiller_timeline`、`algo_load_forecast_day_ahead`、`algo_load_forecast_intraday`、`algo_load_forecast_feature_rank`、`algo_intraday_opt_kpi`、`algo_intraday_control_effect`、`algo_intraday_cop_compare`、`algo_intraday_energy_structure`、`algo_intraday_prediction_tracking`、`algo_intraday_temp_trimming`、`algo_day_ahead_schedule`、`algo_day_ahead_schedule_hourly`、`algo_schedule_confirm_log`、`algo_schedule_dispatch_log`、`algo_model_registry`、`algo_model_update_record`。

**日历/索引**

`dim_day`、`dwr_index`。

### 16.2 关键字段速查

| 表 | 关键字段 | 备注 |
|---|---|---|
| `dm_eo_e_meter_di` | `tenant_id, project_id, meter_id, data_time, energy_value, energy_expense, space_code, spec_enum` | 日粒度主表 |
| `dm_eo_e_meter_hi` | `meter_id, data_time, energy_value` | 小时粒度，有缺口 |
| `dm_eo_s_space_di` | `space_code, data_time, energy_value, space_name` | 有 4 倍重复，必须去重 |
| `dwi_eo_d_point_data` | `device_id, point_code, point_value, data_time` | 65.7M 行，必须聚合/限设备 |
| `dim_eo_m_meter` | `id, name, meter_number, energy_type, meter_status, install_space_code, spec_enum` | 908 块表 |
| `dim_eo_z_space` | `code, name, parent_code, space_path, level, deleted` | 114 有效空间 |
| `dim_eo_d_device` | `id, name, code, device_category_name, space_code, space_name` | 1,808 台 |
| `dim_eo_alarms` | `alarm_name, alarm_level, alarm_status, alarm_time, alarm_recovery_time, device_id, space_code, deleted` | 告警事实 |
| `eo_device_control_task` | `task_id, device_id, property_code, property_value, task_type, status, operator, create_time, error_msg` | 9.5 万条 |
| `dim_eo_energy_price` | `energy_type, price, start_time, end_time, segment, prices` | `prices` JSON 才是真价格 |
| `dim_eo_energy_quota_rules` | `name, quota_object_name, quota_threshold_period, quota_threshold_value, volume` | 视租户 |
| `algo_overview_kpi` | `data_time, current_load, system_scop, today_baseline_kwh, today_actual_kwh, energy_save_pct` | 样本 0 行 |
| `dm_eo_c_measure_emission_di` | `date, emission_name, measure_value, factor_value, emission_value, unit` | 视租户 |

### 16.3 30 个示例问题

**可答**

1. 昨天总用电多少？
2. 近 30 天用电趋势？
3. 本周比上周多用多少？
4. 哪 10 块表用电最多？
5. 35/36/37 层各用多少电？
6. 本月至今累计电量？
7. 每天有多少块表有数据？
8. 哪些表最近 7 天没有数据？
9. 36 层会议室现在室温多少？
10. 36 层会议室设定温度多少？
11. 哪些房间室温高于设定 2°C？
12. 哪些房间室温低于设定 2°C？
13. 有多少台空调面板在线？
14. 昨天控制任务成功率？
15. 哪些设备控制失败最多？
16. 控制失败的原因是什么？
17. 昨天有多少条告警？
18. 告警级别分布？
19. 哪些设备告警最多？
20. 告警平均恢复时长？
21. 本月碳排多少？（`zjct_dm`）
22. 上月电费多少？（`zjct_dm`）
23. 哪些表超定额？（`zjct_dm`）
24. 昨天算法节能率？（有算法数据时）
25. 负荷预测准确率？（有算法数据时）

**应拒答/转替代**

26. 同比去年用电？（无 2025 数据）
27. 节能量是多少？（无基准）
28. 全楼总能耗？（只有多联机内机）
29. 每个房间用了多少电？（无表计-房间映射）
30. 昨天天气如何？（无天气表）

---

## 17. 一句话总结

**这套系统的数据足以做一个“能查、能看、能诊断”的能效 Agent，但不足以做一个“承诺节能率、算同比、算房间电费”的 Agent。** 方案的核心不是模型多强，而是把“哪些数据有、哪些没有、口径是什么、边界在哪”做成可执行的语义层和模板，让 DeepSeek 在边界内回答，让 Archery 守住只读和审计。
