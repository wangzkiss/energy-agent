# 能效 Agent 代码逻辑与库表知识库

> 版本：2026-11-19  
> 适用代码：`E:\Desktop\zuolin\energy-agent`  
> 数据探查：Doris `nwdy_dm` 与 MySQL `nwdy`（南网能源，tenant_id=1026，project_id=1）  
> 当前数据通道：`DATA_PROVIDER=archery`（Doris/MySQL 都通过 `/archery` 技能只读查询，不直连数据库）  
> 运行知识：`server/knowledge.py`；接口：`GET /knowledge`

这份知识库解决三个问题：

1. **别人问一个指标，应该查哪张表、哪些字段？**
2. **这个问题在代码里走哪条链路、对应哪个 intent？**
3. **哪些问题看起来能查，实际上不能查，为什么？**

核心原则：

- 已知指标走固定模板，模型只使用 `providers.py` 返回的 facts；
- 固定模板未覆盖的开放问题，才允许模型生成**只读 SQL**做多步调查；
- 模型 SQL 必须先过 SQL Guard：只读、单语句、当前库全限定名、`tenant_id` 强制、事实表时间过滤、最多 200 行；
- 所有查询仍通过 `/archery` 技能，进入 Archery 审计；写操作全部拒绝；
- 租户/项目/时间范围是硬过滤条件；
- 知识库只回答“查什么、在哪张表、什么口径”，不替代数据查询。

---

## 1. 一页架构

```text
浏览器 web/ (8090)
   │  GET  /models
   │  GET  /tenants
   │  POST /anthropic/v1/messages  (Anthropic SSE)
   ▼
BFF server/ (8088)
   ├─ app.py          鉴权 / SSE / 健康检查
   ├─ agent.py        意图、时间、租户、facts、答案、兜底
   ├─ investigator.py 开放问题的多源 Agent loop（Doris/MySQL）+ SQL Guard
   ├─ providers.py    ArcheryProvider（Doris）/ MySQLArcheryProvider（MySQL）/ MockProvider
   ├─ tenants.py      租户名称/编码 ↔ Doris + MySQL 库
   ├─ knowledge.py    代码+库表知识库
   └─ llm.py          DeepSeek 代理（语言组织 / SQL 调查）
```

本项目只通过 `/archery` 技能访问数据库：`ArcheryProvider` 和
`MySQLArcheryProvider` 导入技能的 `archery_api.py`，复用 Session 登录和 `/query/`
只读接口；MySQL 只切换 `db_name`。已知指标用代码里的固定模板；固定模板未覆盖的开放问题
交 `investigator.py`，由模型选择 Doris/MySQL、生成只读 SQL，再经 SQL Guard 和 Archery
执行。不建立 Doris/MySQL 直连，也不读取 `DORIS_HOST` / `DORIS_USER` /
`DORIS_PASSWORD` / `MYSQL_HOST` / `MYSQL_PASSWORD`。

数据侧：

```text
MySQL energyOperation
   └─ eo_tenant_data_sources      租户 → db_name / db_type

Doris {tenant}_dm
   ├─ dm_eo_e_meter_di            日用电事实（总用电/趋势/排名）
   ├─ dm_eo_e_meter_hi            小时用电事实（单日整点覆盖/缺数）
   ├─ dm_eo_abnormal_meter_reading_record  异常抄表记录
   ├─ dim_eo_d_device             设备维度
   ├─ dwi_eo_d_point_data          设备点位（室温/设定温度）
   ├─ eo_device_control_task      控制任务
   ├─ dim_eo_alarms               告警
   ├─ dm_eo_s_space_di            空间日表（有重复业务键）
   ├─ dwr_eo_energy_meter_wide    表计宽表（当前日快照）
   └─ dwi_eo_energy_usage         用能快照（当前时点）
```

当前会话：

```text
tenant_id = 1026
tenant    = 南网能源
db        = nwdy_dm
project   = 1
energy    = electric
```

---

## 2. 代码调用链

### 2.1 BFF 入口

| 接口 | 作用 |
|---|---|
| `GET /` | 列出接口 |
| `GET /health` | 数据源、模型、当前租户 |
| `GET /health/llm` | 模型代理探活 |
| `GET /models` | 前端模型列表 |
| `GET /tenants` | 15 个租户与 MySQL/Doris 库 |
| `GET /knowledge` | 本知识库的结构化版本（不含密钥） |
| `POST /anthropic/v1/messages` | 聊天/SSE 主入口 |
| `POST/DELETE /anthropic/v1/files` | 501，附件未启用 |

鉴权：

- `x-api-key: <BFF_TOKEN>` 或 `Authorization: Bearer <BFF_TOKEN>`；
- 浏览器不持有 DeepSeek Key，也不连接 Doris；
- `server/.env` 保存模型 appkey 与数据源配置，已 gitignore。

### 2.2 一次提问的完整流程

```text
latest_user_text()
  → ConversationContext.from_messages(history)   # 记忆：上一轮指标/时间/租户/楼层
  → detect_intent()（只有明显追问才继承上一轮数据 intent）
  → parse_time_range()（没有时间词时继承上一轮时间）
  → memory?  → _memory_answer()（直接回答会话历史）
  → knowledge?  → knowledge_answer()（直接返回，不查库）
  → data_quality?  → ArcheryProvider.hourly_completeness()（单日整点覆盖/缺数）
  → resolve_tenant()（没有租户名时继承上一轮租户）
  → 跨租户判断
  → 已知指标：_collect(intent, provider, room_keyword) 固定 SQL
  → 开放问题 / data_quality 模板失败：DataInvestigator（模型 SQL 多步调查）
  → _narrate(facts)（固定模板的模型润色，facts 里带 conversation）
  → LLM 失败则 _fallback_answer()
  → 追加确定性“限制”
  → SSE message_start / content_block_delta / message_delta / message_stop
```

### 2.3 意图顺序

`server/agent.py::detect_intent()` 的顺序会决定歧义问题的归属：

| 顺序 | intent | 典型问题 |
|---:|---|---|
| 1 | `memory` | 我刚才问了什么？上一轮你怎么回答的？ |
| 2 | `tenants` | 目前支持哪些租户的数据？ |
| 3 | `knowledge` | 昨天总用电查哪张表、哪些字段？ |
| 4 | `data_quality` | 小时表的数据今日有缺少的吗？ |
| 5 | `capability` | 你能做什么？边界是什么？ |
| 6 | `alarm` | 昨天有多少告警？ |
| 7 | `control` | 昨天控制成功率多少？ |
| 8 | `room_temp` | 36 层现在室温多少？ |
| 9 | `ranking` | 哪块表用电最多？ |
| 10 | `trend` | 近 30 天用电趋势？ |
| 11 | `total` | 昨天总用电多少？ |
| 12 | `unknown` | 没有识别出指标 |

顺序影响：

- “哪些表用电最多”含“哪些表”，但 `ranking` 的关键词先命中 `最多/排名`，不会走 `knowledge`；
- “租户与库的关系”先命中 `tenants`；
- “总用电量查哪张表”命中 `knowledge`，不会误查数据；
- “电费”已从 `total` 关键词移除；当前走能力边界，明确说明费用模板未接入（见第 7 节）；
- `表计` 本身不再命中 `ranking`；只有“排名/最多/Top/哪些表/哪块表”等排名信号才走排名模板，
  避免“有哪些异常表计”被误判成用电排名。

### 2.3.1 开放数据调查（固定模板未覆盖）

`server/investigator.py::DataInvestigator` + `server/toolbox.py` 用模型做多源、多步只读调查。
设计参考 `deepseek-harness` 的 agent loop：一次 turn 内按 step 循环
“分析问题 → 选工具 → 观察 → 修正 → 结论”，到上限强制收口，而不是一次 prompt 让模型直接猜答案。

```text
问题 + 当前租户/库 + Doris/MySQL 表字段 + 会话上下文（含上一轮回答）
  → 模型输出 {"action":"tool","tool":"metric|run_sql|find_tables|describe_table|knowledge|read_code|tenants","args":{...}}
    或 {"action":"final","answer":"..."}
  → toolbox 执行：metric 走服务端固定 SQL；run_sql 先经 sqlguard 只读校验
  → 结果/错误压缩后回填，可继续换工具、换表或换数据源
  → 最多 8 步；达到上限强制 final；失败再要一次纯 Markdown 结论
  → 仍失败时用已执行结果拼结构化回答，不退回泛泛的能力说明
```

触发条件：

- 语义层没有命中指标，但问题像数据问题（楼层/空间/设备/告警/明细等）；
- 需要固定模板没有的维度：分组、排名、两对象对比、类型分布；
- `intent=mysql` 或显式要求查 MySQL 业务库；
- 固定模板返回空/不可用（告警表为空、室温全 0、排名 0 行）：先换路径再下结论；
- `memory/tenants/knowledge/capability` 和明确命中口径的固定指标不走调查。
- 跨租户追问（如先问南网能源，再问“那荣耀呢”）会继承上一轮 intent/tenant，
  并同时切换 Doris `rongyao_dm` 和 MySQL `rongyao`。

SQL Guard 的硬规则（`validate_readonly_sql`）：

- 只允许 `SELECT` / `WITH` / `SHOW TABLES` / `SHOW FULL COLUMNS` /
  `SHOW CREATE TABLE` / `DESC`；单语句、禁止注释、禁止系统库、禁止任何写操作/DDL/权限语句；
- `SELECT` 必须出现当前数据源库全限定名（如 `nwdy_dm.dm_eo_e_meter_di` 或 `nwdy.eo_devices`）；
- 有 `tenant_id` 的数据源必须带 `tenant_id = 当前租户ID`；MySQL `eo_ticket` 没有
  `tenant_id`，按租户独立库隔离，允许 `require_tenant=False`；
- 禁止引用其它租户库（Doris `rongyao_dm.` 或 MySQL `rongyao.` 都会被拒绝）；
- 事实表必须带时间列过滤：Doris `data_time/create_time/alarm_time/reading_time`；
  MySQL `alarm_time/create_time/reading_time/exec_time/created_at`；
- 没有 `LIMIT` 时自动追加 `LIMIT 200`，显式 LIMIT 大于 200 会被拒绝；
- 单次工具 60 秒超时；工具调用和 SQL 会展示在答案末尾的“查询记录”中，便于复核。

注意：Archery 当前对 `DESC` 支持不稳定，实际表结构探查用
`SHOW FULL COLUMNS FROM db.table`；SHOW 语句不要加 `LIMIT`。
MySQL 业务库有很多 `deleted` 软删除字段，模型会被提示优先加 `deleted=0`，但最终口径仍需业务确认。

### 2.4 时间解析

`parse_time_range(question, today)` 的优先级：

1. 显式日期：`2026-11-17` / `2026/11/17`；
2. `前天`；
3. `昨天/昨日`；
4. `今天/今日`；
5. `上月/上个月`；
6. `本月/这个月`；
7. `本周/这周`；
8. `近30天/近14天/近7天/近90天`；
9. 默认：最近 7 天（含今天）。

状态码/时间列：

- 控制任务用 `create_time`；
- 告警用 `alarm_time`；
- 总用电/趋势用 `data_time`（date）；
- 室温点位用 `data_time`（datetime），限制最近 2 小时。

### 2.5 会话记忆与追问

前端 `web/src/services/chat/reply.ts::toRequestMessages()` 会把同一会话的历史 messages
一起发给 BFF。`server/agent.py::ConversationContext` 从中提取：

| 字段 | 来源 | 用途 |
|---|---|---|
| `intent` | 最近一个有数据的用户问题 | “那今天呢”继承 `total` |
| `start/end/range_label` | 最近一个显式时间表达 | “那荣耀呢”继承上一轮时间 |
| `tenant` | 最近一个可识别的租户名 | “那今天呢”继续查荣耀 |
| `room_keyword` | 最近一个楼层/房间 | “那 37 层呢”继承 `room_temp` |
| `user_turns` | 历史用户问题 | “我刚才问了什么” |

规则：

- 真正的时间词优先；没有时间词才继承；
- 真正的租户名优先；没有租户名才继承；
- 继承时间只对 `total/trend/ranking/room_temp/control/alarm` 生效，不污染知识/能力问题；
- `unknown` 不再无条件继承上一轮指标，只有 `looks_like_followup()` 识别为“那…呢/换成…/今天呢”等追问时才继承；
  新问题如“小时表的数据今日有缺少的吗”不会被套成上一轮的 `ranking`；
- 历史只用于解析上下文，不把上一轮数字当成本轮结果；本轮数字必须重新查库；
- `memory` intent 直接读会话历史，不查库；
- 记忆只覆盖当前会话，不做跨会话用户画像。

### 2.6 租户路由

`server/tenants.py`：

- 静态目录 15 个租户；
- 名称、code、`租户+ID` 可识别；
- 当前租户默认 `1026 / nwdy_dm / MySQL nwdy`；
- `ALLOW_TENANT_QUERY=true`（默认）时，按问题里的租户名同时切换到对应
  Doris 库和 MySQL 库（如荣耀 → `rongyao_dm` + `rongyao`）；
  设为 `false` 时，跨租户问题只回答“已识别但不允许跨库”；
- 开启后，`get_tenant_provider()` 用 `dataclasses.replace()` 把 `doris_db` /
  `doris_tenant_id` 换成目标租户；`get_mysql_provider()` 用租户 `mysql_db` 切 MySQL；
- 开关不是鉴权；生产必须由网关/SSO 完成租户身份校验。

---

## 3. 问题 → 意图 → 库表 路由表

| 问题类型 | intent | 主表 | 关键字段 | 返回 |
|---|---|---|---|---|
| 昨天/今天/本月总用电 | `total` | `dm_eo_e_meter_di` | `tenant_id`, `project_id`, `data_time`, `energy_type`, `energy_value`, `meter_id` | `SUM(energy_value)` kWh、有效表数 |
| 近 N 天趋势 | `trend` | `dm_eo_e_meter_di` | 同上 + `GROUP BY data_time` | 每日 kWh，最多 400 天 |
| 哪块表用电最多 | `ranking` | `dm_eo_e_meter_di` | `meter_id`, `meter_name`, `SUM(energy_value)` | Top 10 |
| 室温/设定温度 | `room_temp` | `dim_eo_d_device` + `dwi_eo_d_point_data` | `name`, `deleted`, `device_id`, `point_code`, `data_time` | 设备名、室温、设定、最近上报 |
| 控制成功率 | `control` | `eo_device_control_task` | `status`, `create_time`, `deleted` | 总数/成功/失败/未知/成功率 |
| 告警数量 | `alarm` | `dim_eo_alarms` | `alarm_level`, `alarm_status`, `alarm_time`, `deleted` | 按级别/状态计数；0 行=不可用 |
| MySQL 业务库/工单/设备台账 | `mysql` | MySQL 当前租户库 | 动态发现；见 4.10 | 多步只读分析，结论+查询记录 |
| 租户与库 | `tenants` | `eo_tenant_data_sources`（MySQL） | `tenant_id`, `db_type`, `db_name` | 名称/code/ID/MySQL/Doris |
| 查哪张表/字段/口径 | `knowledge` | 知识库本身 | — | 表、字段、过滤、代码位置 |
| 同比/节能/房间级/整栋 | `unsupported` | — | — | 明确说明不能查 |
| 电费 | `unknown`（当前） | `dm_eo_e_meter_di` | `energy_expense` 存在但未接入 | 明确说明不能查，不返回 kWh 冒充费用 |

### 3.1 指标语义层（`server/semantic.py`）

从 2026-11-19 起，部分高频问题先经过指标语义层，再进入固定模板或开放调查。它把问题拆成：

```text
metric（指标） + entity（实体） + analysis（分析类型） + time（时间）
```

| 问法 | metric | 执行方式 | 关键边界 |
|---|---|---|---|
| 冷站今天用电多少 | `cold_station_electricity` | MySQL 冷站/设备/电表映射 → Doris 表计汇总 | 冷量计不能当电表；无映射不能回答 0 |
| 冷站冷量多少 | `cold_station_cooling` | 目前只做口径边界 | 读数可能是累计值，不能直接 SUM |
| 配置了哪些冷站 | `cold_station_config` | MySQL 冷站配置固定 SQL | 配置≠运行≠有数据 |
| 今天/昨天用电异常，为什么 | `energy_anomaly` | Doris 当前区间 + 前 7 天日均基线 | 只判断统计偏差，不归因；当天不完整时拒绝判断 |
| MySQL 有多少设备 | `device_count` | `eo_devices` 固定 COUNT | 档案数量≠在线数量 |
| MySQL 有多少表计 | `meter_count` | `eo_energy_meters` 固定 COUNT | 表计≠设备 |
| 有多少设备、多少表计 | `asset_counts` | 两表分别 COUNT | 两个数量分别返回 |
| 控制成功率 | `control_success` | 固定模板 | 只统计 status=2/3 等已结束状态 |
| 告警统计 | `alarm_stats` | 固定模板 | 空表=未接入，不是 0 告警 |
| 小时表缺数 | `data_quality` | 固定模板 | 只检查已结束整点 |

原则：**指标定义负责“问什么、用哪张表、什么口径”；模型不再直接决定已知指标的 SQL。**

---

## 4. 核心库表

### 4.1 `dm_eo_e_meter_di`（日用电事实表）

**用途**：总用电、日趋势、表计排名的唯一主数据源。

**粒度**：

```text
tenant_id + project_id + meter_id + data_time + energy_type
```

**关键字段**：

| 字段 | 含义 |
|---|---|
| `tenant_id` | 租户 ID |
| `project_id` | 项目 ID |
| `meter_id` | 表计 ID |
| `meter_name` | 表计名称 |
| `meter_number` | 表计编号 |
| `meter_attribute` | 1 自用表 / 2 公摊表 / 3 总分表 |
| `data_time` | 日期，日粒度 |
| `energy_type` | 能源类型；电为 `electric` |
| `energy_value` | 能耗值，kWh |
| `energy_expense` | 能耗费用（当前代码未返回） |
| `energy_tb` / `chain_ratio` | 同比/环比（当前未启用） |
| `space_code` / `space_name` | 计量范围空间 |
| `space_path` | 空间路径 |

**固定过滤**：

```sql
WHERE tenant_id = 1026
  AND project_id = 1
  AND data_time BETWEEN :start AND :end
  AND energy_type = 'electric'
```

**聚合**：

```sql
-- total
SUM(energy_value), COUNT(DISTINCT meter_id)

-- trend
GROUP BY data_time, SUM(energy_value), LIMIT 400

-- ranking
GROUP BY meter_id, meter_name, SUM(energy_value) DESC, LIMIT 10
```

**代码**：

- `providers.ArcheryProvider.energy_total`
- `providers.ArcheryProvider.energy_trend`
- `providers.ArcheryProvider.meter_ranking`

**数据画像（nwdy_dm，2026-11-18）**：

- 日期范围 2026-04-01 ~ 2026-11-18；
- 49,527 行；
- 761 块表；
- 1 个项目；
- 按 `meter_id + data_time + energy_type` 无重复键；
- 2026-11-18 当日 758 块表有数据。

**不能用于**：

- 不能把 `energy_expense` 当成当前已返回的电费；当前模板只返回 `energy_value`；
- 不能直接下钻到房间；`space_code` 是计量范围，不是经过验证的房间映射；
- 不能做同比/环比结论，虽然字段存在但代码未启用。

### 4.2 `dwi_eo_d_point_data`（设备点位表）

**用途**：室温、设定温度等设备点位。

**粒度**：

```text
device_id + point_code + data_time
```

**关键字段**：

| 字段 | 含义 |
|---|---|
| `device_id` | 设备 ID |
| `point_code` | 点位编码 |
| `data_time` | 采集时间 |
| `point_value` | 点位值 |

**室温点位**：

```text
RoomTemp     室温
RoomTempSp   设定温度
```

**取数逻辑**：

```sql
WHERE device_id IN (设备维度筛出的 ID)
  AND point_code IN ('RoomTemp', 'RoomTempSp')
  AND data_time >= DATE_SUB(NOW(), INTERVAL 2 HOUR)
ROW_NUMBER() OVER (
  PARTITION BY device_id, point_code
  ORDER BY data_time DESC
) = 1
```

**代码**：`providers.ArcheryProvider.room_temp`

**注意**：

- 点位表**没有** `tenant_id` / `project_id`，租户隔离依赖 `dim_eo_d_device`；
- 只代表最近一次上报，不代表全天舒适度；
- 不能拿点位表直接算房间/楼层总能耗。

**数据画像（nwdy_dm）**：

- 2026-03-31 ~ 2026-11-18；
- 65,807,622 行；
- 1,100 个设备；
- 点位包含 `RoomTemp`、`RoomTempSp`、`ModeSp`、`FanStartSwitchSp`、`temp`、`humidity` 等。

### 4.3 `dim_eo_d_device`（设备维度表）

**用途**：按设备名称找设备，提供租户/项目/空间归属。

**关键字段**：

| 字段 | 含义 |
|---|---|
| `id` | 设备 ID |
| `name` | 设备名称 |
| `code` | 设备编码 |
| `tenant_id` | 租户 ID |
| `project_id` | 项目 ID |
| `space_code` / `space_name` | 空间 |
| `device_category_name` | 设备分类 |
| `device_status` | 设备状态 |
| `deleted` | 0 未删除 |

**过滤**：

```sql
WHERE tenant_id = 1026
  AND project_id = 1
  AND deleted = 0
  AND name LIKE '%36层%'
LIMIT 5
```

**代码**：`providers.ArcheryProvider.room_temp`

**注意**：

- `device_status` 不是“点位在线状态”；
- 代码返回的“在线”只表示最近 2 小时有 `RoomTemp/RoomTempSp` 点位；
- 设备名匹配先取楼层（`\d+层`），避免“36层会议室”匹配不到。

**数据画像（nwdy_dm）**：1,808 行，全部 `deleted=0`。

### 4.4 `eo_device_control_task`（控制任务表）

**用途**：控制任务成功/失败统计。

**粒度**：`task_id`。

**关键字段**：

| 字段 | 含义 |
|---|---|
| `task_id` | 任务 ID |
| `device_id` | 设备 ID（varchar） |
| `tenant_id` | 租户 ID |
| `project_id` | 项目 ID |
| `status` | 状态字典 |
| `create_time` | 创建时间（业务查询列） |
| `deleted` | 0 未删除 |
| `error_msg` | 错误信息 |
| `device_name` | 设备名称 |

**状态字典**：

| status | 含义 |
|---:|---|
| 0 | PENDING 待执行 |
| 1 | EXECUTING 执行中 |
| 2 | SUCCESS 成功 |
| 3 | FAIL 失败 |
| 4 | CANCELLED 取消 |
| 5 | PREEMPTED 抢占 |

**代码口径**：

```text
成功 = status 2
失败 = status 3
unknown = 总数 - 成功 - 失败
成功率 = 成功 / 总数
```

**过滤**：

```sql
WHERE tenant_id = 1026
  AND project_id = 1
  AND deleted = 0
  AND create_time BETWEEN :start AND :end
```

**代码**：`providers.ArcheryProvider.control_stats`

**不能用于**：

- 不能回答“设备现在开没开”；
- 不能把待执行/执行中/取消/抢占算成成功或失败。

**数据画像（nwdy_dm）**：

- 2026-06-18 ~ 2026-11-18；
- 96,174 行；
- 样本 status 只有 2/3（83,503 成功、12,671 失败）。

### 4.5 `dim_eo_alarms`（告警表）

**名称有迷惑性**：前缀是 `dim_`，但实际按事实表使用。

**粒度**：`id`（告警记录）。

**关键字段**：

| 字段 | 含义 |
|---|---|
| `id` | 告警记录 ID |
| `tenant_id` | 租户 ID |
| `project_id` | 项目 ID |
| `alarm_time` | 告警采集时间 |
| `alarm_level` | 0 紧急 / 1 严重 / 2 一般 / 3 警告 |
| `alarm_status` | 0 未处理 / 1 处理中 / 2 已处理 / 3 无需处理 / 4 误报 |
| `alarm_name` | 告警名称 |
| `device_id` | 设备 ID |
| `work_order_code` | 工单编号 |
| `deleted` | 0 未删除 |

**代码口径**：

```sql
WHERE tenant_id = 1026
  AND project_id = 1
  AND deleted = 0
  AND alarm_time BETWEEN :start AND :end
GROUP BY alarm_level, alarm_status
```

**空结果处理**：

```text
0 行 → available=false
回答“该租户/时间窗没有接入告警数据”，不能回答“0 条告警”
```

**代码**：`providers.ArcheryProvider.alarm_stats`

**数据画像（nwdy_dm）**：0 行，当前不可用。

### 4.6 `dm_eo_s_space_di`（空间日表）

**用途**：空间/楼层维度的能耗，可作为未来房间级分析候选。

**关键字段**：

| 字段 | 含义 |
|---|---|
| `space_code` | 空间编码 |
| `space_name` | 空间名称 |
| `parent_code` | 父空间编码 |
| `data_time` | 日期 |
| `energy_type` | 能源类型 |
| `energy_value` | 能耗值 |
| `energy_expense` | 能耗费用 |

**为什么当前不用**：

- 同一 `space_code + data_time + energy_type` 最多有 4 行；
- 直接 `SUM` 会重复计算；
- 没有经过验证的表计→房间结构化映射。

**数据画像（nwdy_dm）**：776 行；重复键最多 4 行。

**结论**：当前代码不用于总用电、趋势、排名；也不能回答房间级能耗。

### 4.7 `dwr_eo_energy_meter_wide`（表计宽表，当前日快照）

**用途**：当前日的表计-设备-空间宽表，含分时段。

**关键字段**：

| 字段 | 含义 |
|---|---|
| `meter_id` / `meter_name` | 表计 |
| `device_id` / `device_name` | 设备 |
| `space_code` / `space_name` | 空间 |
| `energy_value` / `energy_expense` | 能耗/费用 |
| `segment` | 0 总 / 1 峰 / 2 谷 / 3 平 / 4 尖 |
| `power_direct` | 1 正向耗能 / -1 反向产能 |
| `data_time` | 日期 |

**限制**：

- 只有当前日快照；`nwdy_dm` 2026-11-18 单日 758 行；
- 不能做历史趋势；
- 当前代码不使用；峰谷/需量分析尚未接入。

### 4.8 `dwi_eo_energy_usage`（用能快照表）

**用途**：当前用能快照/手动抄表点位。

**关键字段**：

| 字段 | 含义 |
|---|---|
| `meter_id` | 表计 ID |
| `report_time` | 上报时间 |
| `point_code` | 点位代码 |
| `segment` | 0 总 / 1 峰 / 2 谷 / 3 平 / 4 尖 |
| `energy_usage` | 能耗使用值 |
| `energy_expense` | 能耗费用 |
| `energy_saving` | 节能量 |

**限制**：

- 只有当前时点快照；`nwdy_dm` 2026-11-18 00:00 单时点 758 行；
- 不能做趋势；
- 当前代码不使用。

### 4.9 `eo_tenant_data_sources`（租户数据源配置表）

**所在库**：MySQL `energyOperation`，不是 Doris 业务库。

**用途**：租户与 MySQL/Doris 库名的映射来源。

**只使用字段**：

| 字段 | 含义 |
|---|---|
| `tenant_id` | 租户 ID |
| `db_type` | 数据库类型（MySQL/Doris 两类） |
| `db_name` | 库名前缀 |
| `deleted` | 删除标识 |

**安全边界**：

- `url` / `username` / `password` / `slave_*` 绝不返回给前端或模型；
- 知识库/接口只暴露 `tenant_id`、`db_type`、`db_name`；
- 静态目录不会实时反映新增租户，需更新 `server/tenants.py`。

**当前映射**：

```text
15 个租户；Doris 库名 = db_name + "_dm"
1026 / 南网能源 / nwdy / nwdy_dm / 当前
```

### 4.10 线上 MySQL 业务库（2026-11-19 实查）

**所在实例**：Archery `数智能效saas现网`（MySQL master），不是 Doris 实例。  
**库名**：每个租户一个库，库名 = 租户编码，例如 `nwdy`、`rongyao`。  
**访问方式**：`MySQLArcheryProvider` 通过 Archery `/query/` 只读执行；不直连 MySQL。

高价值表（完整 219 张表用 `SHOW TABLES LIKE` 动态发现）：

| 表 | 用途 | 关键字段 |
|---|---|---|
| `eo_devices` | 设备台账 | `tenant_id`, `project_id`, `device_category_name`, `space_code`, `device_status`, `alarm_status`, `deleted` |
| `eo_energy_meters` | 表计档案 | `tenant_id`, `project_id`, `meter_number`, `energy_type`, `meter_attribute`, `device_id`, `deleted` |
| `eo_spaces` | 空间主数据 | `code`, `parent_code`, `level`, `tenant_id`, `project_id`, `deleted` |
| `eo_energy_meter_readings` | 抄表读数 | `meter_id`, `reading_value`, `reading_time`, `pvf`, `tenant_id`, `project_id`, `deleted` |
| `eo_alarms` | 告警 | `alarm_name`, `alarm_level`, `alarm_status`, `alarm_time`, `device_id`, `work_order_code`, `deleted` |
| `eo_device_control_task` | 控制任务 | `task_id`, `device_id`, `task_type`, `status`, `error_msg`, `create_time`, `deleted` |
| `eo_device_control_log` | 控制执行日志 | `device_id`, `exec_result`, `exec_time`, `control_type`, `deleted` |
| `eo_ticket` | 工单 | `ticket_no`, `title`, `status`, `alarm_id`, `created_at`, `start_time`, `end_time`（**没有 tenant_id**） |
| `eo_energy_prices` | 电价方案 | `price`, `pvf`, `start_time`, `end_time`, `months`, `tenant_id`, `deleted` |
| `eo_ai_strategy` / `eo_ai_strategy_execute_log` | AI 温控策略/执行 | `zone_id`, `space_id`, `execute_time`, `zone_avg_temp`, `ai_adjust_temp`, `weather`, `executed` |
| `eo_energy_saving_cooling_base_configs` | **冷站主配置** | `cooling_name`, `cold_site_id`, `application_id`, `space_code`, `calendar_code`, `cold_type`, `deleted` |
| `eo_energy_saving_cooling_device_configs` | 冷站设备参数 | `application_id`, `device_id`, `device_category_id`, `category_code`, `cop`, `power`, `cooling_capacity` |
| `eo_energy_saving_cooling_device_properties` | 冷站点位配置 | `application_id`, `site_id`, `device_id`, `metrics_type`, `point_code`, `point_name` |
| `eo_energy_saving_cooling_system_configs` | 冷站系统系数 | `application_id`, `config_type`, `config_code`, `config_value` |
| `eo_energy_saving_cooling_alarm_configs` | 冷站告警阈值 | `application_id`, `alarm_type_id`, `quota_threshold_period`, `threshold_*` |
| `eo_cold_algo_config` / `eo_cold_algo_schedule` | 冷站算法开关/排期 | `application_id`, `algo_type`, `schedule_date`, `cooling_load_*` |
| `eo_applications` | 冷站应用/菜单 | `id`, `name`, `parent_id`, `module_code` |

**逻辑关系（无外键）**：

- `eo_devices.space_code → eo_spaces.code`
- `eo_energy_meters.device_id → eo_devices.id`
- `eo_energy_meter_readings.meter_id → eo_energy_meters.id`
- `eo_alarms.device_id → eo_devices.id`；`eo_alarms.work_order_code → eo_ticket.ticket_no`
- `eo_device_control_task.device_id → eo_devices.id`（varchar，按实际值比较）
- `eo_ai_strategy.zone_id / space_id → 分区/空间`

**实测口径（nwdy，2026-11-19）**：

```text
eo_devices (tenant_id=1026, deleted=0)          = 1,808 台
eo_energy_meters (tenant_id=1026, deleted=0)    = 908 个
eo_device_control_task (deleted=0)              = 4,664 条
  status=2 成功 4,660；status=3 失败 4；成功率 99.91%
```

**SQL Guard 与 MySQL 差异**：

- 有 `tenant_id` 的表必须带当前租户等值过滤；
- `eo_ticket` 无 `tenant_id`，按租户独立库隔离，Guard 允许 `require_tenant=False`；
- 事实表时间列：`eo_alarms.alarm_time`、`eo_device_control_task.create_time`、
  `eo_energy_meter_readings.reading_time`、`eo_ticket.created_at`；
- 禁止跨库引用其它租户（如 `nwdy` 查询中出现 `rongyao.` 直接拒绝）；
- 多步调查最多 8 步工具；优先 `SHOW FULL COLUMNS`，不要用 `DESC`，SHOW 不加 `LIMIT`。

### 4.11 冷站配置（MySQL，2026-11-19 实查）

**核心结论**：问题“南网配置了哪些冷站？”应查 `eo_energy_saving_cooling_base_configs`，
而不是从设备名称/空间名称猜。

**主表字段**：

| 字段 | 类型/说明 |
|---|---|
| `cooling_name` | 冷站名称 |
| `cold_site_id` | 冷站业务 ID（字符串） |
| `application_id` | 冷站应用 ID，关联 `eo_applications.id` |
| `space_code` | 所属空间编码，可能是逗号分隔路径 |
| `calendar_code` | 日历编码；样例值 1，不能直接等同 `eo_calendar.calendar_id` |
| `cold_type` | 源表注释：1=冷源系统，2=集中供冷；其它值（如 3）注释未覆盖 |
| `deleted` | 逻辑删除，0=有效 |

**固定查询模板**：

```sql
SELECT
  id, application_id, cooling_name, cold_site_id, space_code,
  calendar_code, cold_type, description, create_time
FROM {db}.eo_energy_saving_cooling_base_configs
WHERE tenant_id = {tenant_id}
  AND project_id = {project_id}
  AND deleted = 0
ORDER BY id
LIMIT 100;
```

**实测数据**：

```text
nwdy / 南网能源 / tenant_id=1026 / project_id=1:
  eo_energy_saving_cooling_base_configs = 0 条
  → 当前没有配置冷站；不能推断为“冷站停运”或“设备不存在”。

其它租户样例（用于验证字段语义）：
  shuanghui / 双汇 / tenant_id=2:
    股份三冷站   cold_site_id=197861af6faec9ca741bc119  cold_type=1
    总部大楼冷站 cold_site_id=1989d304a311b6be0848fc41  cold_type=1
  zjct / 中交城投 / tenant_id=4:
    中交总部冷站 cold_site_id=19b1185e0de48e4451b704d6  cold_type=3（注释未覆盖）
  qldl / 秦龙电力 / tenant_id=5:
    秦龙电力     cold_site_id=1967b420b42b58656166f7a9  cold_type=1
```

**关联表**：

| 关系 | 说明 |
|---|---|
| `base_configs.application_id → eo_applications.id` | 冷站应用；名称通常与 `cooling_name` 一致 |
| `base_configs.application_id → cooling_device_configs.application_id` | 查冷站下的冷机/水泵等设备参数 |
| `cooling_device_configs.device_id → eo_devices.id` | 设备台账 |
| `cooling_device_configs.device_category_id → eo_device_categories.id` | 设备分类（如冷机、冷冻水泵） |
| `base_configs.cold_site_id → cooling_device_properties.site_id` | 点位配置；样例存在 `undefined`，用前判空 |
| `base_configs.application_id → cooling_system_configs / cooling_alarm_configs / cold_algo_config.application_id` | 系统系数、告警阈值、算法配置 |
| `base_configs.space_code → eo_spaces.code` | 逗号分隔路径，需拆分或用 `FIND_IN_SET` |
| `base_configs.calendar_code → eo_calendar` | 业务日历；样例值 1，需业务确认映射 |

**实现方式**：

- 固定意图 `cold_station`：`agent._cold_station_answer()` 直接查主表，不依赖模型；
- 追问“这个冷站配置了哪些设备/点位？”会走 MySQL 多步调查，继续关联
  `cooling_device_configs` / `cooling_device_properties`；
- 没有记录时回答“0 条配置”，不能回答“冷站停运/设备不存在”。

---

## 5. 表关系

没有数据库外键，以下都是**逻辑关系**：

| 从 | 到 | 关系 | 用途 |
|---|---|---|---|
| `dim_eo_d_device.id` | `dwi_eo_d_point_data.device_id` | 1:N | 室温：先筛设备，再取点位 |
| `dim_eo_d_device.id` | `dwr_eo_energy_meter_wide.device_id` | 1:N | 宽表设备关联；当前未用 |
| `dm_eo_e_meter_di.meter_id` | `dwr_eo_energy_meter_wide.meter_id` | 1:N | 宽表表计关联；当前未用 |
| `dm_eo_e_meter_di.space_code` | `dm_eo_s_space_di.space_code` | N:1 | 空间桥接；空间表有重复键 |
| `eo_device_control_task.device_id` | `dim_eo_d_device.id` | N:1（需 CAST） | 控制任务可关联设备；当前不 JOIN |
| `dim_eo_alarms.device_id` | `dim_eo_d_device.id` | N:1 | 告警可关联设备；当前不 JOIN |
| `eo_tenant_data_sources.tenant_id` | 各 Doris 库 `tenant_id` | 1:N | 租户目录/动态 Provider |

### 5.1 固定 SQL 模板（只读）

以下模板对应 `providers.py`；`{db}` 是租户 Doris 库，参数全部由代码传入，不接受模型拼接。

```sql
-- total：期间总用电
SELECT
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters,
  MIN(data_time) AS start_date,
  MAX(data_time) AS end_date
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %s
  AND project_id = %s
  AND data_time BETWEEN %s AND %s
  AND energy_type = %s;
```

```sql
-- trend：日趋势
SELECT
  data_time,
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT meter_id) AS active_meters
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %s
  AND project_id = %s
  AND data_time BETWEEN %s AND %s
  AND energy_type = %s
GROUP BY data_time
ORDER BY data_time
LIMIT 400;
```

```sql
-- ranking：表计排名
SELECT
  meter_id,
  meter_name,
  SUM(energy_value) AS energy_value,
  COUNT(DISTINCT data_time) AS days
FROM {db}.dm_eo_e_meter_di
WHERE tenant_id = %s
  AND project_id = %s
  AND data_time BETWEEN %s AND %s
  AND energy_type = %s
GROUP BY meter_id, meter_name
ORDER BY energy_value DESC
LIMIT %s;
```

```sql
-- data_quality：小时表按小时点覆盖（单日）
SELECT
  data_time,
  COUNT(*) AS row_count,
  COUNT(DISTINCT meter_id) AS meter_count
FROM {db}.dm_eo_e_meter_hi
WHERE tenant_id = %s
  AND project_id = %s
  AND energy_type = %s
  AND data_time >= %s
  AND data_time < %s
GROUP BY data_time
ORDER BY data_time
LIMIT 400;
```

```sql
-- room_temp：先筛设备，再取最近 2 小时最新点位
SELECT id, name
FROM {db}.dim_eo_d_device
WHERE tenant_id = %s
  AND project_id = %s
  AND deleted = 0
  AND name LIKE %s
ORDER BY name
LIMIT 5;

SELECT device_id, point_code, point_value, data_time
FROM (
  SELECT
    device_id, point_code, point_value, data_time,
    ROW_NUMBER() OVER (
      PARTITION BY device_id, point_code
      ORDER BY data_time DESC
    ) AS rn
  FROM {db}.dwi_eo_d_point_data
  WHERE device_id IN (%s, ...)
    AND point_code IN ('RoomTemp', 'RoomTempSp')
    AND data_time >= DATE_SUB(NOW(), INTERVAL 2 HOUR)
) t
WHERE rn = 1
ORDER BY device_id, point_code
LIMIT 50;
```

```sql
-- control：控制任务状态分布
SELECT status, COUNT(*) AS cnt
FROM {db}.eo_device_control_task
WHERE tenant_id = %s
  AND project_id = %s
  AND deleted = 0
  AND create_time BETWEEN %s AND %s
GROUP BY status;
```

```sql
-- alarm：告警级别/状态分布
SELECT alarm_level, alarm_status, COUNT(*) AS cnt
FROM {db}.dim_eo_alarms
WHERE tenant_id = %s
  AND project_id = %s
  AND deleted = 0
  AND alarm_time BETWEEN %s AND %s
GROUP BY alarm_level, alarm_status;
```

开放问题不在这里穷举模板：`DataInvestigator` 会让模型先 `SHOW TABLES`/
`SHOW COLUMNS` 或直接基于 `knowledge.TABLES` 生成 SELECT，再交给 SQL Guard；
具体规则见 2.3.1。固定模板仍是已知指标的优先路径，避免模型自由发挥。

---

## 6. 代码常量与限制

| 项 | 值 | 位置 |
|---|---|---|
| 当前租户 | 1026 / 南网能源 / `nwdy_dm` | `config.py` / `.env` |
| 当前项目 | 1 | `DORIS_PROJECT_ID` |
| 能源类型 | `electric` | `DORIS_ENERGY_TYPE` |
| 演示日期 | 2026-11-18 | `DEMO_TODAY` |
| 趋势上限 | 400 天 | `energy_trend` |
| 排名上限 | 10（SQL 允许最多 20） | `meter_ranking` |
| 室温设备 | 最多 30 个 | `room_temp` |
| 室温窗口 | 最近 2 小时 | `room_temp` |
| 跨租户开关 | 默认 true | `ALLOW_TENANT_QUERY` |
| MySQL 数据源 | 默认启用，库=租户 code | `MYSQL_ENABLED` / `MYSQL_DB` |
| 自主调查开关 | 默认 true | `AGENT_TOOL_LOOP` |
| 自主调查上限 | 8 步工具 / 200 行 / 单次 60 秒 | `AGENT_MAX_STEPS` / `investigator.py` |
| 调查工具 | metric / run_sql / find_tables / describe_table / knowledge / read_code / tenants | `toolbox.py` |
| SSE 思考 | 真实工具步骤逐条推送 | `app.py` |
| SSE 回答 | 按行推送，默认 8 秒铺完（25~180ms/行） | `ANSWER_STREAM_SECONDS` / `app.py` |
| 模型 | 默认 `deepseek-flash` | `.env` / 设置页 |
| 模型协议 | `anthropic`（/v1/messages）或 `openai`（/v1/chat/completions） | `LLM_PROTOCOL` / 设置页 |
| 模型配置落盘 | `server/model_config.json`（优先于 .env，Key 只上行） | `model_config.py` / `GET/PUT /model/config` |

---

## 7. 当前不支持 / 容易误答

| 问题 | 现状 | 原因 |
|---|---|---|
| 同比/环比 | 不支持 | 历史不足两年；字段未启用 |
| 节能率/节能量 | 不支持 | 没有可靠基线；字段未验证 |
| 房间级能耗 | 不支持 | 表计→房间无验证映射；空间表有重复 |
| 整栋建筑总能耗 | 不支持 | 只有租户/项目级汇总 |
| 气象归一化 | 不支持 | 没有气象数据 |
| 工单闭环率/SLA 数值 | 只能给公式 | `eo_ticket` 在租户 1026 下 0 行，没有分母；`status` 终态枚举无法用数据验证 |
| 实时控制 | 不支持 | 只读，不能下发指令 |
| 峰谷/需量 | 只能给公式 | 宽表只有 `segment=0` 快照；没有需量表，`ActivePowerSum` 仅 3 台设备且全为 0 |
| 电费 | 只能给公式 | `eo_energy_prices` 租户 1026 下 0 行；`energy_expense` 全为 0（未回填） |
| 分项计量 | 只能讲口径 | `meter_category_id` 全空、`meter_attribute` 全为 1；按名称近似归类仅覆盖约 25% 电量 |
| 单位面积能耗 | 只能给公式 | `eo_projects.area` 与 `eo_spaces.area` 全是占位值 1（114 个空间都是 1） |
| 碳排/折标煤 | 只能给参考值 | 库内没有排放因子/折标煤系数，不能给核算结果 |
| 倍率核对 | 无法判定 | `eo_energy_meters.rate` 全为 1，抄表明细 0 行，无法判断日表是否已乘倍率 |
| 采集→日表完整链路 | 部分可证 | 只能证明 `dm_eo_e_meter_hi` → `dm_eo_e_meter_di`（2026-11-18 两边都是 4102.44 kWh）；上游采集/调度不在本项目 |
| 跨日补数/采集值准确性 | 不支持 | 已支持单日整点覆盖；补数状态、采集值准确性和采集任务状态未接入 |
| 严格楼层/房间排名 | 不完整 | `space_name` 只有 `A1座`；只能从 `meter_name` 解析楼层，覆盖约 44% 电量，答案必须带覆盖率说明 |
| 告警类型分布 | 不可用 | `dim_eo_alarms` / `eo_alarms` 在租户 1026 下为空（未接入），不是 0 条告警 |
| 空压机压力 | 不可用 | 设备台账没有空压机类设备，“资金监控室-空压面板”是空调面板 |
| 设备“一直运行” | 统计判据 | 用 `running_devices`：活跃小时占比 ≥ 0.8；是统计推断，不等于实际制冷 |

### 7.1 业务口径知识层（`server/concepts.py`）

2026-11-19 用 12 个业务问题做了盲测：定义类问题（怎么算/是什么/流程/区别）此前会被
当成数据查询，跑 27～57 秒后回答“没有数据”，甚至答非所问（冷站 COP 只回了“没有冷站配置”）。
现在新增独立的业务口径层：

| 主题 | 关键结论 |
|---|---|
| 冷站制冷量与 COP | COP = 冷量 ÷ 冷站用电；查询顺序 algo_overview_kpi.system_scop → 冷量计/电表 → algo 排期；南网配置 0 条，亨通空调站实测 4.85 |
| 冷站用电 | 必须经 配置→设备→电表 映射；没有映射不能回答 0 kWh |
| 分项计量 | `meter_category_id` 全空，只能按名称近似，不能出分项报表 |
| 最大需量 | 15 分钟平均功率取最大；无 15 分钟功率/需量寄存器，只能回答不可用 |
| 峰谷平电价/电费 | `eo_energy_prices` 0 行、`energy_expense` 全 0，只能给公式 |
| 单位面积能耗 | `area` 全为占位值 1，不能算 EUI |
| 碳排/折标煤 | 库内无系数，只能给公开参考值并注明来源 |
| 告警等级/闭环 | 0 紧急/1 严重/2 一般/3 警告；0 未处理…4 误报；告警表为空（未接入≠0） |
| 工单闭环率 | 公式完整，但 `eo_ticket` 0 行，不能给数值 |
| 表计倍率 | `rate` 字段存在但全为 1，无法验证是否已乘 |
| 数据同步链路 | 小时表→日表可逐位核对；采集上游不在本项目 |
| 设备类型 | `device_category_name`；档案 1,808 台，空调面板 209 台 |

冷站 SCOP 另走专项指标 `cold_station_scop`（`server/toolbox.py`）：先查
`algo_overview_kpi.system_scop`，再用 `dm_eo_e_meter_di` 的 `energy_type='cold'` 冷量计
除以按名称归集的冷站电表，输出每个冷站的冷量/用电/SCOP/状态；电表≈0 会标记
“电量存疑”，不会把 0 当能效；SCOP>8 也会标记（正常冷站上限约 6~7）。
默认按最近一个完整日计算，用户明确说区间才用区间。亨通 2026-11-18 实测：空调冷源
系统 SCOP=4.85、工艺系统电表仅 6.4 kWh 不可用、综合冷机为汇总口径 4.72。

### 7.2 自主发现能力（2026-11-19 新增）

| 能力 | 作用 | 位置 |
|---|---|---|
| `search_columns` | 在真实 `information_schema.COLUMNS` 按字段名/注释检索，弥补本地知识库只覆盖 38/397 张表的盲区 | `server/toolbox.py` |
| `table_profile` | 一次返回行数、时间范围、租户数；0 行明确标“空表（未接入）”，避免把空表当 0 值 | `server/toolbox.py` |
| `find_tables` 真实库兜底 | 精选目录不命中时，自动按字段名/注释回查 information_schema，并标注“精选知识库未收录” | `server/toolbox.py` |
| `cold_station_scop` | 冷站 SCOP 专项指标：冷站配置 + 冷量计 + 电表名称归集，输出状态标记 | `server/toolbox.py` |
| `cold_station_devices` | 按冷站统计设备分类台数（冷机/水泵/冷却塔/总管） | `server/toolbox.py` |
| 调查协议 | 术语展开 → 找落点 → 判可用 → 验映射 → 取数 → 交叉校验 → 分层结论，写进 investigator 系统提示 | `server/investigator.py` |
| schema 快照缓存 | 首次检索实时返回，后台把表清单+字段名/类型/注释落盘（6h TTL）；之后字段检索 0 秒命中 | `server/schema_cache.py` |
| 自适应步数 | 常规问题 8 步收口；只有用过 search_columns/table_profile 的发现类才允许到 12 步 | `server/investigator.py` |
| 黄金回归集 | 12 道业务问题（SCOP/室温/告警/租户/口径），输出 PASS/FAIL 与耗时 | `logs/golden_questions.py` |
| 单测 | `test_columns.py` / `test_profile.py` / `test_scop.py` / `test_schema_cache.py` 覆盖新工具 | `server/` |

能力覆盖（2026-11-19，亨通库为实测样本）：

| 维度 | 改前 | 现在 |
|---|---|---|
| 可感知表 | 精选 34 张 | 精选 38 张 + 真实库 397 张（字段检索兜底） |
| 工具数 | 7 | 9（+search_columns +table_profile） |
| 字段级检索 | 无 | search_columns（MySQL/Doris 双源） |
| 空表判断 | 无 | table_profile（行数/时间范围/租户数） |
| 单次步数 | 8 | 自适应 8~12（发现类 12） |
| 冷站 SCOP | 无 | cold_station_scop（含数据质量状态） |
| 元数据检索 | 每次实时查 information_schema | schema 快照（6h TTL），冷启动后台构建，命中 0s |
| 回归集 | 无 | 12 题黄金问题（2026-11-19 实测 12/12，平均 17.8s）+ 71 个单测 |

快照命中实测（亨通，2026-11-19）：首次 `search_columns` 2.2s（实时查 + 后台建
229KB/233KB 快照），第二次 0.0s；快照覆盖 Doris 187 张、MySQL 210 张表。快照过期后
第一次检索会回退实时查询，并在后台重建，不阻塞当前回答。

使用约束：`search_columns` 只返回元数据（表名/字段名/类型/注释），不返回数据；
返回 0 行只说明字段名/注释没有命中，不能推断“没有这张表/没有这个指标”。

路由规则：问题同时命中业务关键词和“怎么算/公式/是什么/流程/区别/口径”等定义标记时，
直接由 `concept_answer()` 返回“结论 / 计算口径 / 本项目数据情况 / 边界 / 相关表与代码”，
不再进入 SQL 循环；带“昨天/今天/本月/排名/趋势”的数据问题仍走原数据链路。
盲测 12 题中 10 题由概念层 0.0s 返回，另外 2 题（总用电、楼层室温）仍走数据链路。

特别提醒：

```text
“昨天电费多少” 当前不会返回 kWh 冒充费用，而是走能力边界。
要支持电费，需要新增 cost intent 和 SUM(energy_expense) 固定模板。

“小时表的数据今日有缺少的吗” 现在走 data_quality 模板：
查 dm_eo_e_meter_hi，按小时统计行数/去重表计数；只把已结束整点算作应到，
当前未结束的小时不判缺失；不会拿日表 Top 10 排名冒充完整性结果。
跨日补数、采集值准确性、采集任务状态仍不支持。
```

---

## 8. 典型问题示例

### 8.1 昨天总用电多少？

```text
intent: total
表: nwdy_dm.dm_eo_e_meter_di
过滤: tenant_id=1026, project_id=1, data_time='2026-11-18', energy_type='electric'
聚合: SUM(energy_value), COUNT(DISTINCT meter_id)
代码: agent._collect -> ArcheryProvider.energy_total（复用 ArcheryProvider 模板）
返回: 4,102.44 kWh（2026-11-18 现网日汇总，Archery 只读）
```

### 8.2 36 层现在室温多少？

```text
intent: room_temp
表: nwdy_dm.dim_eo_d_device + nwdy_dm.dwi_eo_d_point_data
过滤: name LIKE '%36层%', deleted=0；点位 RoomTemp/RoomTempSp，最近 2 小时
代码: agent._collect -> ArcheryProvider.room_temp
返回: 设备名、室温、设定温度、最近上报时间
```

### 8.3 昨天控制成功率多少？

```text
intent: control
表: nwdy_dm.eo_device_control_task
过滤: tenant_id=1026, project_id=1, deleted=0, create_time 昨天
口径: status=2 成功，status=3 失败
代码: agent._collect -> ArcheryProvider.control_stats
```

### 8.4 查询租户荣耀的今日用电数据

```text
intent: total
识别: 荣耀 / 1033 / rongyao_dm
生产默认: ALLOW_TENANT_QUERY=true，按荣耀库查询
表: rongyao_dm.dm_eo_e_meter_di
过滤: tenant_id=1033, project_id=1, data_time='2026-11-19', energy_type='electric'
实际只读查询: 早间约 961 kWh（11-19 07:40，当日未完整，数值会继续增长），47 块表
昨日完整日汇总: 15,411.75 kWh（11-18）
```

> `ALLOW_TENANT_QUERY` 只是功能开关，不是鉴权；生产仍应由网关/SSO
> 注入租户身份后再决定是否允许切库。

### 8.5 总用电量查哪张表、哪些字段？

```text
intent: knowledge
表: dm_eo_e_meter_di
字段: tenant_id, project_id, meter_id, data_time, energy_type, energy_value
过滤: tenant_id + project_id + data_time + energy_type='electric'
聚合: SUM(energy_value), COUNT(DISTINCT meter_id)
代码: providers.ArcheryProvider.energy_total
```

### 8.6 南网能源 MySQL 里有多少设备、多少表计？

```text
intent: mysql
数据源: MySQL nwdy（tenant_id=1026, project_id=1）
SQL: SELECT COUNT(*) FROM nwdy.eo_devices WHERE tenant_id=1026 AND deleted=0
     + SELECT COUNT(*) FROM nwdy.eo_energy_meters WHERE tenant_id=1026 AND deleted=0
实测: 1,808 台设备 / 908 个表计（2026-11-19）
```

追问“那荣耀呢？”会继承上一轮 MySQL 意图，切到 `rongyao` 库并带上
`tenant_id=1033`；上一轮回答通过 `ConversationContext.previous_answer` 提供给模型，
用于理解“那荣耀呢”的指代，但本轮数字仍重新查库。

### 8.7 昨天用电最多的楼层是哪个？

```text
intent: ranking
路由: plan=energy_ranking + “楼层” → 不走表计排名，进入自主调查
tool: metric=floor_energy（先从 meter_name 解析楼层并返回覆盖率）
      + metric=space_energy（看结构化空间覆盖）
解析规则: 先 ([0-9]+)[层楼]，再 栋([0-9]+)-，两种都不匹配算未识别
结论必须带: 楼层口径、覆盖率、无法归层的电量
```

### 8.8 最近一周哪些设备一直在运行？

```text
tool: metric=running_devices（hours=168, min_ratio=0.8）
判据: FanStartSwitchSp / ModeSp / ActivePowerSum 任一 > 0，记为该小时活跃
实测: 212 台有运行点位的设备中 183 台活跃占比 ≥ 0.8
注意: 这是统计判据，不等于设备实际在制冷
```

---

## 9. 如何扩展

新增一个指标时，按顺序改：

1. **数据层**：已知指标在 `providers.py` 增加固定 SQL 方法；开放问题交给
   `investigator.py`，不要为每个开放问题硬编码表名；
2. **意图**：在 `agent.py::detect_intent()` 增加关键词，注意顺序和宽泛词误命中；
3. **收集**：在 `_collect()` 增加分支；
4. **答案**：在 `_fallback_answer()` 增加确定性模板，模型只做润色；
5. **知识库**：在 `knowledge.py` 增加表/字段/路由，`_schema_text` 会把它带给开放调查；
6. **本文件**：同步更新路由表、表说明、限制；
7. **验证**：`py_compile`、SQL Guard 单测、`/knowledge`、聊天接口、前端构建。

不要做的事：

- 固定模板不要根据用户问题拼接表名或 SQL；开放调查必须经过
  `investigator.validate_readonly_sql()`；
- 不要把 `eo_tenant_data_sources.url/username/password` 放进知识库；
- 不要把 `dwr_eo_energy_meter_wide` / `dwi_eo_energy_usage` 当历史趋势；
- 不要直接 `SUM(dm_eo_s_space_di.energy_value)`；
- 不要把空告警回答成“0 条告警”；
- 不要把当前未结束的小时算成小时表缺失。

---

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `server/app.py` | BFF 接口、SSE、鉴权 |
| `server/agent.py` | 意图、时间、租户、答案 |
| `server/investigator.py` | 开放问题多步只读调查 + SQL Guard |
| `server/providers.py` | Mock / ArcheryProvider（Doris）/ MySQLArcheryProvider（MySQL）固定 SQL + 小时表覆盖 |
| `server/tenants.py` | 租户 ↔ 库 |
| `server/knowledge.py` | 结构化知识库 + `GET /knowledge` |
| `server/llm.py` | 模型代理 |
| `server/semantic.py` | 指标语义层：指标定义、问题计划、歧义检测 |
| `server/config.py` | 环境变量 |
| `web/src/services/chat/store.ts` | 前端 SSE |
| `README.md` | 启动与部署 |
