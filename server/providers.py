"""能效数据提供者。

- MockProvider：离线演示数据，只在显式设置 DATA_PROVIDER=mock 时使用；
- ArcheryProvider：通过 Archery 技能的 `archery_api.py` 只读查询 Doris，
  SQL 全部是固定模板，不接受模型生成的 SQL。

本实现不建立 Doris 直连，不读取 DORIS_HOST / DORIS_USER / DORIS_PASSWORD；
数据库访问统一走 Archery 审计通道。
"""
from __future__ import annotations

import asyncio
import math
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from config import Settings, settings
from tenants import Tenant


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def day_range(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _fmt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _sql_literal(value: Any) -> str:
    """把固定模板的参数安全地转成 SQL 字面量（仅用于 Archery 本地桥）。"""
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return "'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(value, date):
        return "'" + value.strftime("%Y-%m-%d") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return "'" + text + "'"


def _bind_sql(sql: str, params: tuple[Any, ...]) -> str:
    """把固定 SQL 里的 %s 替换为字面量；参数数量必须与模板一致。"""
    if not params:
        # 没有参数时不能按 %s 切分，否则 LIKE '%s...' 会被误判成占位符。
        return sql
    parts = sql.split("%s")
    if len(parts) - 1 != len(params):
        raise ValueError(f"SQL 参数数量不匹配: {len(parts) - 1} != {len(params)}")
    out = parts[0]
    for value, tail in zip(params, parts[1:]):
        out += _sql_literal(value) + tail
    return out


class EnergyProvider(ABC):
    mode: str = "base"
    dataset: str = "unknown"

    @abstractmethod
    async def energy_total(self, start: date, end: date) -> dict[str, Any]: ...

    @abstractmethod
    async def energy_trend(self, start: date, end: date) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def meter_ranking(self, start: date, end: date, limit: int = 10) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def room_temp(self, keyword: str = "") -> dict[str, Any]: ...

    @abstractmethod
    async def control_stats(self, start: date, end: date) -> dict[str, Any]: ...

    @abstractmethod
    async def alarm_stats(self, start: date, end: date) -> dict[str, Any]: ...

    def capability(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "mode": self.mode,
            "available": [
                "期间总用电量（日粒度）",
                "日用电趋势",
                "表计用电排名",
                "设备室温与设定温度（有室温点位的设备）",
                "控制任务成功/失败统计",
                "冷站 SCOP/系统能效比（按冷量计与电表名称归集，电表缺失会标记不可用）",
                "业务口径：冷站 COP、分项计量、最大需量、峰谷平电价、单位面积能耗、"
                "碳排折标、告警等级、工单闭环、倍率、数据同步链路（定义+公式+本项目数据情况）",
                "能力边界与数据口径说明",
            ],
            "unavailable": [
                "同比/环比（历史不足两年）",
                "节能率/节能量（没有基线）",
                "房间级能耗（表计与房间无结构化映射）",
                "整栋建筑总能耗（只有租户/项目级汇总）",
                "气象归一化",
                "工单闭环率数值（eo_ticket 0 行，只有公式）",
                "电费数值（eo_energy_prices 0 行、energy_expense 全 0）",
                "分项电量（meter_category_id 全空）",
                "最大需量（无 15 分钟功率/需量寄存器）",
                "单位面积能耗（area 全是占位值 1）",
                "碳排/折标煤数值（库内没有系数）",
                "倍率换算核对（rate 全为 1，无法验证是否已乘）",
                "实时控制指令下发",
            ],
            "notes": [
                "总电量以 dm_eo_e_meter_di 为准；空间表存在重复行，不能直接 SUM。",
                "dwr_eo_energy_meter_wide / dwi_eo_energy_usage 是当日快照，不能做趋势。",
                "告警是否可用取决于租户是否接入 dim_eo_alarms。",
            ],
        }


class MockProvider(EnergyProvider):
    mode = "mock"
    dataset = "nwdy_dm（演示数据，量级与真实库一致）"

    def __init__(self, config: Settings = settings) -> None:
        self.today = parse_day(config.demo_today) or date.today()

    def _daily_value(self, day: date) -> float:
        # 最近 7 天使用 nwdy_dm 的真实日汇总，便于和 Doris 结果对齐
        known = {
            date(2026, 11, 12): 2741.45,
            date(2026, 11, 13): 2923.31,
            date(2026, 11, 14): 3050.59,
            date(2026, 11, 15): 2233.59,
            date(2026, 11, 16): 4428.67,
            date(2026, 11, 17): 4679.53,
            date(2026, 11, 18): 3372.53,
        }
        if day in known:
            return known[day]
        weekday = day.weekday()
        seasonal = 150 * math.sin(day.toordinal() / 3.7)
        weekend_drop = -120 if weekday >= 5 else 30
        return round(3550 + seasonal + weekend_drop, 2)

    async def energy_total(self, start: date, end: date) -> dict[str, Any]:
        days = day_range(start, end)
        total = round(sum(self._daily_value(day) for day in days), 2)
        return {
            "metric": "总用电量",
            "unit": "kWh",
            "value": total,
            "start": _fmt(days[0]),
            "end": _fmt(days[-1]),
            "days": len(days),
            "daily_average": round(total / len(days), 2),
            "active_meters": 758,
            "total_meters": 761,
            "source": "nwdy_dm.dm_eo_e_meter_di",
        }

    async def energy_trend(self, start: date, end: date) -> list[dict[str, Any]]:
        days = day_range(start, end)
        return [
            {
                "date": _fmt(day),
                "energy_value": self._daily_value(day),
                "active_meters": 758 if day != self.today else 758,
            }
            for day in days
        ]

    async def meter_ranking(self, start: date, end: date, limit: int = 10) -> list[dict[str, Any]]:
        rows = [
            (118, "19层空调KW-19-4（8-5", 973.45),
            (43, "A1栋32层4-1", 459.41),
            (112, "19层空调KW-19-3主（6-", 435.27),
            (45, "A1栋32层4-1", 397.81),
            (50, "A1栋32层4-1-2", 374.06),
            (240, "A1#3层空调外机组用电1-15", 322.54),
            (111, "19层空调KW-19-3从1（6", 314.21),
            (40, "A1栋32层3-2 33-1", 292.18),
            (197, "A1栋 11楼北面", 272.76),
            (201, "A1栋11层 32F北面", 272.36),
        ]
        return [
            {
                "rank": index + 1,
                "meter_id": meter_id,
                "meter_name": name,
                "energy_value": value,
                "share": None,
            }
            for index, (meter_id, name, value) in enumerate(rows[: max(1, min(limit, 20))])
        ]

    async def room_temp(self, keyword: str = "") -> dict[str, Any]:
        rows = [
            {
                "device_id": "DEV-3601",
                "device_name": "36层-电梯厅1空调面板",
                "room_temp": 25.8,
                "setpoint": 26.0,
                "last_seen": "2026-11-18 09:45:00",
                "status": "在线",
            },
            {
                "device_id": "DEV-3602",
                "device_name": "36层-电梯厅2空调面板",
                "room_temp": 26.0,
                "setpoint": 26.0,
                "last_seen": "2026-11-18 09:45:00",
                "status": "在线",
            },
            {
                "device_id": "DEV-3603",
                "device_name": "36层-男厕所空调面板",
                "room_temp": 26.2,
                "setpoint": 26.0,
                "last_seen": "2026-11-18 09:44:00",
                "status": "在线",
            },
            {
                "device_id": "DEV-3604",
                "device_name": "36层-女厕所空调面板",
                "room_temp": 25.9,
                "setpoint": 26.0,
                "last_seen": "2026-11-18 09:44:00",
                "status": "在线",
            },
        ]
        if keyword:
            rows = [row for row in rows if keyword in row["device_name"]]
        return {
            "available": bool(rows),
            "devices": rows,
            "note": "演示数据；真实库需先按设备名解析 device_id，再取最近点位的 RoomTemp/RoomTempSp。",
            "source": "nwdy_dm.dwi_eo_d_point_data",
        }

    async def control_stats(self, start: date, end: date) -> dict[str, Any]:
        return {
            "available": True,
            "total": 95810,
            "success": 83141,
            "failed": 12669,
            "unknown": 0,
            "success_rate": 86.8,
            "window": f"{_fmt(start)} ~ {_fmt(end)}",
            "note": "控制任务表是累计快照，当前演示值按 2026-11-17 统计；真实库按 create_time 过滤。",
            "source": "nwdy_dm.eo_device_control_task",
        }

    async def alarm_stats(self, start: date, end: date) -> dict[str, Any]:
        return {
            "available": False,
            "count": 0,
            "window": f"{_fmt(start)} ~ {_fmt(end)}",
            "reason": "当前 nwdy_dm 的 dim_eo_alarms 无数据；可切换 xxzd_dm 验证告警能力。",
            "source": "nwdy_dm.dim_eo_alarms",
        }


class ArcheryProvider(EnergyProvider):
    """通过 Archery 技能执行的只读数据提供者。

    不连接 Doris，也不保存数据库密码；运行时导入技能目录下的
    `archery_api.py`，复用其登录、会话缓存和 `/query/` 接口。
    """

    mode = "archery"
    dataset = "archery"

    def __init__(self, config: Settings = settings) -> None:
        self.config = config
        if not re.fullmatch(r"[A-Za-z0-9_]+", config.doris_db):
            raise ValueError("DORIS_DB 只能包含字母、数字和下划线")
        self.dataset = f"{config.doris_db}（Archery 只读）"
        self._client: Any = None

    def _archery(self):
        if self._client is None:
            helper_dir = self.config.archery_helper_dir
            if helper_dir and helper_dir not in sys.path:
                sys.path.insert(0, helper_dir)
            try:
                from archery_api import ArcheryClient
            except ImportError as exc:
                raise RuntimeError(
                    f"找不到 Archery 技能助手: {helper_dir}；"
                    "请把 ARCHERY_HELPER_DIR 指向技能目录"
                ) from exc
            self._client = ArcheryClient()
        return self._client

    def _query(
        self,
        sql: str,
        params: tuple[Any, ...],
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        final_sql = _bind_sql(sql, params)
        response = self._archery().post(
            "/query/",
            {
                "instance_name": self.config.archery_instance,
                "db_name": self.config.doris_db,
                "sql_content": final_sql,
                "limit_num": str(max(1, min(int(limit), 500))),
            },
        )
        if response.get("status") != 0:
            raise RuntimeError(f"Archery 查询失败: {response.get('msg')}")
        data = response.get("data") or {}
        if data.get("error"):
            raise RuntimeError(f"Archery SQL 错误: {data['error']}")
        columns = data.get("column_list") or []
        rows = data.get("rows") or []
        return [
            {str(column): value for column, value in zip(columns, row)}
            for row in rows
        ]

    async def _run(
        self,
        sql: str,
        params: tuple[Any, ...],
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._query, sql, params, limit)

    async def ad_hoc_query(self, sql: str, limit: int = 200) -> list[dict[str, Any]]:
        """执行经过 SQL Guard 校验的只读查询；不接收未校验的模型 SQL。"""
        return await self._run(sql, (), limit=limit)

    def _base_params(self) -> tuple[Any, ...]:
        return (
            self.config.doris_tenant_id,
            self.config.doris_project_id,
            self.config.doris_energy_type,
        )

    async def energy_total(self, start: date, end: date) -> dict[str, Any]:
        sql = f"""
            SELECT
              SUM(energy_value) AS energy_value,
              COUNT(DISTINCT meter_id) AS active_meters,
              MIN(data_time) AS start_date,
              MAX(data_time) AS end_date
            FROM {self.config.doris_db}.dm_eo_e_meter_di
            WHERE tenant_id = %s
              AND project_id = %s
              AND data_time BETWEEN %s AND %s
              AND energy_type = %s
        """
        rows = await self._run(
            sql,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                start,
                end,
                self.config.doris_energy_type,
            ),
        )
        row = rows[0] if rows else {}
        return {
            "metric": "总用电量",
            "unit": "kWh",
            "value": round(float(row.get("energy_value") or 0), 2),
            "start": _fmt(row.get("start_date") or start),
            "end": _fmt(row.get("end_date") or end),
            "days": (end - start).days + 1,
            "active_meters": int(row.get("active_meters") or 0),
            "source": f"{self.config.doris_db}.dm_eo_e_meter_di",
        }

    async def energy_by_meters(
        self, meter_ids: list[int], start: date, end: date
    ) -> list[dict[str, Any]]:
        """按指定表计 ID 汇总用电量；用于冷站等跨表计聚合。"""
        ids = sorted({int(item) for item in meter_ids if int(item) > 0})
        if not ids:
            return []
        placeholders = ", ".join(["%s"] * len(ids))
        sql = f"""
            SELECT
              meter_id,
              SUM(energy_value) AS energy_value,
              COUNT(DISTINCT data_time) AS days
            FROM {self.config.doris_db}.dm_eo_e_meter_di
            WHERE tenant_id = %s
              AND project_id = %s
              AND energy_type = %s
              AND data_time BETWEEN %s AND %s
              AND meter_id IN ({placeholders})
            GROUP BY meter_id
        """
        rows = await self._run(
            sql,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                self.config.doris_energy_type,
                start,
                end,
                *ids,
            ),
            limit=max(200, len(ids) * 2),
        )
        return [
            {
                "meter_id": int(row.get("meter_id") or 0),
                "energy_value": round(float(row.get("energy_value") or 0), 2),
                "days": int(row.get("days") or 0),
            }
            for row in rows
        ]

    async def hourly_completeness(self, start: date, end: date) -> dict[str, Any]:
        """小时表按小时点统计覆盖情况；用行数和表计数判断缺数。"""
        if end < start:
            return {"available": False, "reason": "结束日期早于开始日期", "rows": []}
        sql = f"""
            SELECT
              data_time,
              COUNT(*) AS row_count,
              COUNT(DISTINCT meter_id) AS meter_count
            FROM {self.config.doris_db}.dm_eo_e_meter_hi
            WHERE tenant_id = %s
              AND project_id = %s
              AND energy_type = %s
              AND data_time >= %s
              AND data_time < %s
            GROUP BY data_time
            ORDER BY data_time
            LIMIT 400
        """
        rows = await self._run(
            sql,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                self.config.doris_energy_type,
                start,
                end + timedelta(days=1),
            ),
        )
        return {
            "available": True,
            "rows": rows,
            "source": f"{self.config.doris_db}.dm_eo_e_meter_hi",
        }

    async def energy_trend(self, start: date, end: date) -> list[dict[str, Any]]:
        sql = f"""
            SELECT
              data_time,
              SUM(energy_value) AS energy_value,
              COUNT(DISTINCT meter_id) AS active_meters
            FROM {self.config.doris_db}.dm_eo_e_meter_di
            WHERE tenant_id = %s
              AND project_id = %s
              AND data_time BETWEEN %s AND %s
              AND energy_type = %s
            GROUP BY data_time
            ORDER BY data_time
            LIMIT 400
        """
        rows = await self._run(
            sql,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                start,
                end,
                self.config.doris_energy_type,
            ),
        )
        return [
            {
                "date": _fmt(row.get("data_time")),
                "energy_value": round(float(row.get("energy_value") or 0), 2),
                "active_meters": int(row.get("active_meters") or 0),
            }
            for row in rows
        ]

    async def meter_ranking(self, start: date, end: date, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 20))
        sql = f"""
            SELECT
              meter_id,
              meter_name,
              SUM(energy_value) AS energy_value,
              COUNT(DISTINCT data_time) AS days
            FROM {self.config.doris_db}.dm_eo_e_meter_di
            WHERE tenant_id = %s
              AND project_id = %s
              AND data_time BETWEEN %s AND %s
              AND energy_type = %s
            GROUP BY meter_id, meter_name
            ORDER BY energy_value DESC
            LIMIT %s
        """
        params = (
            self.config.doris_tenant_id,
            self.config.doris_project_id,
            start,
            end,
            self.config.doris_energy_type,
            safe_limit,
        )
        try:
            rows = await self._run(sql, params)
        except Exception:
            # 个别租户的宽表没有 meter_name，退回 meter_id 展示
            sql = sql.replace("meter_id,\n              meter_name,", "meter_id,\n              meter_id AS meter_name,")
            sql = sql.replace("GROUP BY meter_id, meter_name", "GROUP BY meter_id")
            rows = await self._run(sql, params)
        return [
            {
                "rank": index + 1,
                "meter_id": row.get("meter_id"),
                "meter_name": row.get("meter_name") or row.get("meter_id"),
                "energy_value": round(float(row.get("energy_value") or 0), 2),
                "days": int(row.get("days") or 0),
            }
            for index, row in enumerate(rows)
        ]

    async def energy_by_space(
        self, start: date, end: date, limit: int = 30
    ) -> dict[str, Any]:
        """按 space_name/space_code 聚合用电，用于楼层/空间排名与覆盖率检查。"""
        safe_limit = max(1, min(int(limit), 100))
        sql = f"""
            SELECT
              COALESCE(NULLIF(space_name, ''), '未标注空间') AS space_name,
              COALESCE(NULLIF(space_code, ''), '-') AS space_code,
              ROUND(SUM(energy_value), 2) AS energy_value,
              COUNT(DISTINCT meter_id) AS meters
            FROM {self.config.doris_db}.dm_eo_e_meter_di
            WHERE tenant_id = %s
              AND project_id = %s
              AND data_time BETWEEN %s AND %s
              AND energy_type = %s
            GROUP BY space_name, space_code
            ORDER BY energy_value DESC
            LIMIT {safe_limit}
        """
        rows = await self._run(
            sql,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                start,
                end,
                self.config.doris_energy_type,
            ),
        )
        spaces = [
            {
                "space_name": row.get("space_name"),
                "space_code": row.get("space_code"),
                "energy_value": round(float(row.get("energy_value") or 0), 2),
                "meters": int(row.get("meters") or 0),
            }
            for row in rows
        ]
        return {
            "available": bool(spaces),
            "spaces": spaces,
            "space_count": len(spaces),
            "total": round(sum(item["energy_value"] for item in spaces), 2),
            "note": (
                "space_name 是表计计量范围，不是严格楼层映射；"
                "如果分组数很少（如只有 A1座），说明楼层维度未拆分，不能当作楼层排名。"
            ),
            "source": f"{self.config.doris_db}.dm_eo_e_meter_di",
        }

    async def device_readings(
        self,
        keyword: str = "",
        point_codes: tuple[str, ...] = (),
        hours: int = 24,
        limit: int = 400,
    ) -> dict[str, Any]:
        """按设备名称/空间匹配设备，汇总最近窗口内每个点位的统计值。

        点位表本身没有 tenant_id，这里先用设备台账按租户筛出 device_id 再关联，
        因此模型不需要、也不应该自己写点位表 SQL。
        """
        safe_hours = max(1, min(int(hours), 24 * 30))
        safe_limit = max(1, min(int(limit), 800))
        clauses = [
            "d.tenant_id = %s",
            "d.project_id = %s",
            "d.deleted = 0",
            "p.data_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)",
        ]
        params: list[Any] = [
            self.config.doris_tenant_id,
            self.config.doris_project_id,
            safe_hours,
        ]
        if keyword:
            clauses.append("(d.name LIKE %s OR d.space_name LIKE %s)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        codes = tuple(
            code for code in point_codes if re.fullmatch(r"[A-Za-z0-9_]{1,64}", str(code))
        )
        if codes:
            placeholders = ",".join(["%s"] * len(codes))
            clauses.append(f"p.point_code IN ({placeholders})")
            params.extend(codes)
        sql = f"""
            SELECT
              p.device_id,
              d.name AS device_name,
              d.space_name,
              p.point_code,
              ROUND(AVG(p.point_value), 2) AS avg_value,
              ROUND(AVG(CASE WHEN p.point_value > 0 THEN p.point_value END), 2) AS avg_positive,
              ROUND(MIN(p.point_value), 2) AS min_value,
              ROUND(MAX(p.point_value), 2) AS max_value,
              SUM(CASE WHEN p.point_value = 0 THEN 1 ELSE 0 END) AS zero_samples,
              COUNT(*) AS samples,
              COUNT(DISTINCT DATE_FORMAT(p.data_time, '%Y-%m-%d %H')) AS active_hours,
              MAX(p.data_time) AS last_time
            FROM {self.config.doris_db}.dwi_eo_d_point_data p
            JOIN {self.config.doris_db}.dim_eo_d_device d ON d.id = p.device_id
            WHERE {" AND ".join(clauses)}
            GROUP BY p.device_id, d.name, d.space_name, p.point_code
            ORDER BY p.device_id, p.point_code
            LIMIT {safe_limit}
        """
        try:
            rows = await self._run(sql, tuple(params), limit=safe_limit)
        except Exception as exc:  # noqa: BLE001 - 让模型看到失败原因并修正
            return {
                "available": False,
                "rows": [],
                "reason": str(exc),
                "keyword": keyword,
                "window_hours": safe_hours,
            }
        readings = [
            {
                "device_id": row.get("device_id"),
                "device_name": row.get("device_name"),
                "space_name": row.get("space_name"),
                "point_code": row.get("point_code"),
                "avg": row.get("avg_value"),
                "avg_positive": row.get("avg_positive"),
                "min": row.get("min_value"),
                "max": row.get("max_value"),
                "zero_samples": row.get("zero_samples"),
                "samples": row.get("samples"),
                "active_hours": row.get("active_hours"),
                "last_time": _fmt(row.get("last_time")),
            }
            for row in rows
        ]
        return {
            "available": bool(readings),
            "rows": readings,
            "count": len(readings),
            "keyword": keyword,
            "point_codes": list(codes),
            "window_hours": safe_hours,
            "note": (
                "avg_positive 已剔除 0 值，适合温度/压力等物理量；"
                "zero_samples 是 0 值条数，0 值可能是采集异常；"
                "active_hours 是窗口内出现过该点位的不同小时数。"
            ),
            "source": f"{self.config.doris_db}.dwi_eo_d_point_data",
        }

    async def energy_by_floor(
        self, start: date, end: date, limit: int = 50
    ) -> dict[str, Any]:
        """按表计名称解析楼层并聚合用电。

        dm_eo_e_meter_di 没有结构化楼层字段，楼层号只能从 meter_name 前缀
        （如 “19层空调KW-19-4”“A2栋36-8”）解析；因此必须同时返回覆盖率，
        不能把解析结果当严格楼层口径。
        """
        safe_limit = max(1, min(int(limit), 100))
        params = (
            self.config.doris_tenant_id,
            self.config.doris_project_id,
            start,
            end,
            self.config.doris_energy_type,
        )
        floors_sql = f"""
            SELECT building, floor_no,
                   ROUND(SUM(energy_value), 2) AS energy_value,
                   COUNT(DISTINCT meter_id) AS meters
            FROM (
              SELECT meter_id, energy_value,
                     COALESCE(
                       NULLIF(regexp_extract(meter_name, '^([A-Za-z0-9]+)栋', 1), ''),
                       '-'
                     ) AS building,
                     COALESCE(
                       NULLIF(regexp_extract(meter_name, '([0-9]+)[层楼]', 1), ''),
                       NULLIF(regexp_extract(meter_name, '栋([0-9]+)-', 1), '')
                     ) AS floor_no
              FROM {self.config.doris_db}.dm_eo_e_meter_di
              WHERE tenant_id = %s
                AND project_id = %s
                AND data_time BETWEEN %s AND %s
                AND energy_type = %s
            ) t
            WHERE floor_no IS NOT NULL AND floor_no != ''
            GROUP BY building, floor_no
            ORDER BY energy_value DESC
            LIMIT {safe_limit}
        """
        totals_sql = f"""
            SELECT ROUND(SUM(energy_value), 2) AS total,
                   COUNT(DISTINCT meter_id) AS meters
            FROM {self.config.doris_db}.dm_eo_e_meter_di
            WHERE tenant_id = %s
              AND project_id = %s
              AND data_time BETWEEN %s AND %s
              AND energy_type = %s
        """
        rows = await self._run(floors_sql, params, limit=safe_limit)
        totals = await self._run(totals_sql, params, limit=1)
        total = float((totals[0].get("total") if totals else 0) or 0)
        meters_total = int((totals[0].get("meters") if totals else 0) or 0)
        floors = [
            {
                "building": str(row.get("building") or "-"),
                "floor": str(row.get("floor_no") or ""),
                "energy_value": round(float(row.get("energy_value") or 0), 2),
                "meters": int(row.get("meters") or 0),
            }
            for row in rows
        ]
        covered = round(sum(item["energy_value"] for item in floors), 2)
        coverage = round(covered / total * 100, 1) if total else 0.0
        return {
            "available": bool(floors),
            "floors": floors,
            "floor_count": len(floors),
            "total": round(total, 2),
            "covered": covered,
            "unmatched": round(max(total - covered, 0), 2),
            "coverage_pct": coverage,
            "meters": meters_total,
            "note": (
                "楼层号从 meter_name 解析：先匹配 ([0-9]+)[层楼]（如 19层空调），"
                "再匹配 栋([0-9]+)-（如 A2栋36-8）；不是结构化字段。"
                "同一楼层号按栋拆分（building=’-’ 表示名称里没有栋前缀）；"
                "coverage_pct 是能解析到楼层的电量占比，结论必须带上覆盖率。"
            ),
            "source": f"{self.config.doris_db}.dm_eo_e_meter_di",
        }

    async def running_devices(
        self,
        hours: int = 168,
        min_ratio: float = 0.8,
        limit: int = 50,
    ) -> dict[str, Any]:
        """找出窗口内持续运行/持续打开的空调设备。

        判据：FanStartSwitchSp / ModeSp / ActivePowerSum 任一 > 0 当作有运行信号；
        设备在该小时内出现运行信号，即记为该小时活跃。按活跃小时占比排序。
        """
        safe_hours = max(1, min(int(hours), 24 * 30))
        ratio = max(0.1, min(float(min_ratio), 1.0))
        safe_limit = max(1, min(int(limit), 200))
        sql = f"""
            SELECT
              d.id AS device_id,
              d.name AS device_name,
              d.space_name,
              COUNT(DISTINCT CASE
                WHEN p.point_value > 0 THEN DATE_FORMAT(p.data_time, '%Y-%m-%d %H')
              END) AS active_hours,
              COUNT(DISTINCT DATE_FORMAT(p.data_time, '%Y-%m-%d %H')) AS total_hours,
              ROUND(MAX(CASE WHEN p.point_code = 'ActivePowerSum' THEN p.point_value END), 2) AS max_power,
              ROUND(AVG(CASE WHEN p.point_code = 'ActivePowerSum' AND p.point_value > 0
                             THEN p.point_value END), 2) AS avg_power,
              ROUND(MAX(CASE WHEN p.point_code = 'FanStartSwitchSp' THEN p.point_value END), 2) AS max_fan_speed,
              MAX(p.data_time) AS last_time
            FROM {self.config.doris_db}.dwi_eo_d_point_data p
            JOIN {self.config.doris_db}.dim_eo_d_device d ON d.id = p.device_id
            WHERE d.tenant_id = %s
              AND d.project_id = %s
              AND d.deleted = 0
              AND p.point_code IN ('FanStartSwitchSp', 'ModeSp', 'ActivePowerSum')
              AND p.data_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)
            GROUP BY d.id, d.name, d.space_name
            ORDER BY active_hours DESC
            LIMIT 1000
        """
        rows = await self._run(
            sql,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                safe_hours,
            ),
            limit=1000,
        )
        checked = len(rows)
        matched = []
        for row in rows:
            total = int(row.get("total_hours") or 0)
            active = int(row.get("active_hours") or 0)
            if total <= 0 or active <= 0:
                continue
            share = round(active / total, 3)
            if share < ratio:
                continue
            matched.append(
                {
                    "device_id": row.get("device_id"),
                    "device_name": row.get("device_name"),
                    "space_name": row.get("space_name"),
                    "active_hours": active,
                    "total_hours": total,
                    "active_ratio": share,
                    "avg_power": row.get("avg_power"),
                    "max_power": row.get("max_power"),
                    "max_fan_speed": row.get("max_fan_speed"),
                    "last_time": _fmt(row.get("last_time")),
                }
            )
        matched.sort(
            key=lambda item: (
                -float(item["active_ratio"]),
                -float(item["avg_power"] or 0),
            )
        )
        return {
            "available": bool(matched),
            "rows": matched[:safe_limit],
            "checked_devices": checked,
            "matched_devices": len(matched),
            "window_hours": safe_hours,
            "min_ratio": ratio,
            "note": (
                "active_ratio = 有运行信号的小时数 / 有点位数据的小时数；"
                "这是“持续运行”的统计判据，不等于设备一定在制冷，也不代表舒适度。"
            ),
            "source": f"{self.config.doris_db}.dwi_eo_d_point_data",
        }

    async def room_temp(self, keyword: str = "") -> dict[str, Any]:
        pattern = f"%{keyword}%" if keyword else "%空调%"
        devices = await self._run(
            f"""
            SELECT id, name
            FROM {self.config.doris_db}.dim_eo_d_device
            WHERE tenant_id = %s
              AND project_id = %s
              AND deleted = 0
              AND name LIKE %s
            ORDER BY name
            LIMIT 30
            """,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                pattern,
            ),
        )
        if not devices:
            return {
                "available": False,
                "devices": [],
                "reason": f"没有找到名称匹配“{keyword or '空调'}”的设备",
                "source": f"{self.config.doris_db}.dim_eo_d_device",
            }

        ids = [int(item["id"]) for item in devices if item.get("id") is not None]
        if not ids:
            return {"available": False, "devices": [], "reason": "设备缺少 id"}
        placeholders = ",".join(["%s"] * len(ids))
        rows = await self._run(
            f"""
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
              FROM {self.config.doris_db}.dwi_eo_d_point_data
              WHERE device_id IN ({placeholders})
                AND point_code IN ('RoomTemp', 'RoomTempSp')
                AND data_time >= DATE_SUB(NOW(), INTERVAL 2 HOUR)
            ) t
            WHERE rn = 1
            ORDER BY device_id, point_code
            LIMIT 50
            """,
            tuple(ids),
        )
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            device_id = int(row["device_id"])
            latest.setdefault(device_id, {"device_id": device_id, "room_temp": None, "setpoint": None})
            if row["point_code"] == "RoomTemp":
                latest[device_id]["room_temp"] = float(row["point_value"])
            elif row["point_code"] == "RoomTempSp":
                latest[device_id]["setpoint"] = float(row["point_value"])
            latest[device_id]["last_seen"] = _fmt(row["data_time"])

        valid: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for device in devices:
            device_id = int(device["id"])
            item = latest.get(device_id, {})
            room_temp = item.get("room_temp")
            setpoint = item.get("setpoint")
            if room_temp is None and setpoint is None:
                continue
            # 0℃ / 负值通常是设备复位或无有效采集，不能当真实室温回答。
            room_temp_valid = room_temp if room_temp is not None and room_temp > 0 else None
            setpoint_valid = setpoint if setpoint is not None and setpoint > 0 else None
            record = {
                "device_id": device_id,
                "device_name": device.get("name") or str(device_id),
                "room_temp": room_temp_valid,
                "setpoint": setpoint_valid,
                "raw_room_temp": room_temp,
                "last_seen": item.get("last_seen"),
                "status": "最近上报",
            }
            if room_temp_valid is None and setpoint_valid is None:
                invalid.append(record)
            else:
                valid.append(record)

        reason = ""
        if not valid and invalid:
            reason = (
                f"最近 2 小时有 {len(invalid)} 块设备上报，但室温/设定值都是 0 或负值，"
                "疑似设备复位或采集无效，不能当作真实室温"
            )
        return {
            "available": bool(valid),
            "devices": valid,
            "invalid_devices": invalid,
            "reason": reason,
            "note": "只统计最近 2 小时有点位数据的设备；0℃/负值已按无效值剔除。",
            "source": f"{self.config.doris_db}.dwi_eo_d_point_data",
        }

    async def control_stats(self, start: date, end: date) -> dict[str, Any]:
        rows = await self._run(
            f"""
            SELECT status, COUNT(*) AS cnt
            FROM {self.config.doris_db}.eo_device_control_task
            WHERE tenant_id = %s
              AND project_id = %s
              AND deleted = 0
              AND create_time BETWEEN %s AND %s
            GROUP BY status
            """,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                f"{start} 00:00:00",
                f"{end} 23:59:59",
            ),
        )
        by_status = {str(row.get("status")): int(row.get("cnt") or 0) for row in rows}
        total = sum(by_status.values())
        success = by_status.get("2", 0)
        failed = by_status.get("3", 0)
        return {
            "available": total > 0,
            "total": total,
            "success": success,
            "failed": failed,
            "unknown": total - success - failed,
            "success_rate": round(success / total * 100, 1) if total else None,
            "by_status": by_status,
            "window": f"{_fmt(start)} ~ {_fmt(end)}",
            "source": f"{self.config.doris_db}.eo_device_control_task",
        }

    async def alarm_stats(self, start: date, end: date) -> dict[str, Any]:
        rows = await self._run(
            f"""
            SELECT alarm_level, alarm_status, COUNT(*) AS cnt
            FROM {self.config.doris_db}.dim_eo_alarms
            WHERE tenant_id = %s
              AND project_id = %s
              AND deleted = 0
              AND alarm_time BETWEEN %s AND %s
            GROUP BY alarm_level, alarm_status
            """,
            (
                self.config.doris_tenant_id,
                self.config.doris_project_id,
                f"{start} 00:00:00",
                f"{end} 23:59:59",
            ),
        )
        count = sum(int(row.get("cnt") or 0) for row in rows)
        if count == 0:
            return {
                "available": False,
                "count": 0,
                "window": f"{_fmt(start)} ~ {_fmt(end)}",
                "reason": "该租户 dim_eo_alarms 在时间窗内无数据",
                "source": f"{self.config.doris_db}.dim_eo_alarms",
            }
        return {
            "available": True,
            "count": count,
            "by_level_status": [
                {
                    "alarm_level": row.get("alarm_level"),
                    "alarm_status": row.get("alarm_status"),
                    "count": int(row.get("cnt") or 0),
                }
                for row in rows
            ],
            "window": f"{_fmt(start)} ~ {_fmt(end)}",
            "source": f"{self.config.doris_db}.dim_eo_alarms",
        }


class MySQLArcheryProvider:
    """线上 MySQL 业务库的只读查询通道。

    与 Doris 使用同一个 Archery 实例，只切换 db_name；不直连 MySQL，
    不接受未经 investigator SQL Guard 校验的模型 SQL。
    """

    mode = "mysql_archery"

    def __init__(
        self,
        config: Settings = settings,
        database: str | None = None,
        tenant_id: int | None = None,
    ) -> None:
        self.config = config
        self.database = database or config.mysql_db
        self.tenant_id = tenant_id if tenant_id is not None else config.doris_tenant_id
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.database):
            raise ValueError("MySQL 库名只能包含字母、数字和下划线")
        self.dataset = f"{self.database}（MySQL / Archery 只读）"
        self._client: Any = None

    def _archery(self):
        if self._client is None:
            helper_dir = self.config.archery_helper_dir
            if helper_dir and helper_dir not in sys.path:
                sys.path.insert(0, helper_dir)
            try:
                from archery_api import ArcheryClient
            except ImportError as exc:
                raise RuntimeError(
                    f"找不到 Archery 技能助手: {helper_dir}；"
                    "请把 ARCHERY_HELPER_DIR 指向技能目录"
                ) from exc
            self._client = ArcheryClient()
        return self._client

    def _query(self, sql: str, limit: int = 200) -> list[dict[str, Any]]:
        response = self._archery().post(
            "/query/",
            {
                "instance_name": self.config.mysql_archery_instance,
                "db_name": self.database,
                "sql_content": sql,
                "limit_num": str(max(1, min(int(limit), 500))),
            },
        )
        if response.get("status") != 0:
            raise RuntimeError(f"MySQL Archery 查询失败: {response.get('msg')}")
        data = response.get("data") or {}
        if data.get("error"):
            raise RuntimeError(f"MySQL SQL 错误: {data['error']}")
        columns = data.get("column_list") or []
        rows = data.get("rows") or []
        return [
            {str(column): value for column, value in zip(columns, row)}
            for row in rows
        ]

    async def ad_hoc_query(self, sql: str, limit: int = 200) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._query, sql, limit)

    async def cooling_stations(self) -> list[dict[str, Any]]:
        """冷站配置固定模板；库名/租户过滤均由服务端拼接，不接受模型 SQL。"""
        sql = f"""
            SELECT
              id,
              application_id,
              cooling_name,
              cold_site_id,
              space_code,
              calendar_code,
              cold_type,
              description,
              create_time
            FROM {self.database}.eo_energy_saving_cooling_base_configs
            WHERE tenant_id = {int(self.tenant_id)}
              AND project_id = {int(self.config.doris_project_id)}
              AND deleted = 0
            ORDER BY id
            LIMIT 100
        """
        return await self.ad_hoc_query(sql, limit=100)

    async def cold_station_meter_mapping(self) -> list[dict[str, Any]]:
        """冷站 -> 设备 -> 电表映射；没有电表映射时返回 0，不能当成 0 kWh。"""
        sql = f"""
            SELECT
              b.id AS station_id,
              b.cooling_name,
              b.application_id,
              b.cold_site_id,
              COUNT(DISTINCT dc.device_id) AS device_count,
              COUNT(DISTINCT m.id) AS electric_meter_count,
              GROUP_CONCAT(DISTINCT m.id ORDER BY m.id) AS meter_ids
            FROM {self.database}.eo_energy_saving_cooling_base_configs b
            LEFT JOIN {self.database}.eo_energy_saving_cooling_device_configs dc
              ON dc.application_id = b.application_id
             AND dc.deleted = 0
            LEFT JOIN {self.database}.eo_energy_meters m
              ON m.device_id = dc.device_id
             AND m.deleted = 0
             AND m.energy_type = 'electric'
             AND m.meter_status = 2
            WHERE b.tenant_id = {int(self.tenant_id)}
              AND b.project_id = {int(self.config.doris_project_id)}
              AND b.deleted = 0
            GROUP BY b.id, b.cooling_name, b.application_id, b.cold_site_id
            ORDER BY b.id
            LIMIT 100
        """
        return await self.ad_hoc_query(sql, limit=100)

    async def count_devices(self) -> int:
        sql = f"""
            SELECT COUNT(*) AS cnt
            FROM {self.database}.eo_devices
            WHERE tenant_id = {int(self.tenant_id)}
              AND project_id = {int(self.config.doris_project_id)}
              AND deleted = 0
        """
        rows = await self.ad_hoc_query(sql, limit=1)
        return int((rows[0] if rows else {}).get("cnt") or 0)

    async def count_meters(self) -> int:
        sql = f"""
            SELECT COUNT(*) AS cnt
            FROM {self.database}.eo_energy_meters
            WHERE tenant_id = {int(self.tenant_id)}
              AND project_id = {int(self.config.doris_project_id)}
              AND deleted = 0
        """
        rows = await self.ad_hoc_query(sql, limit=1)
        return int((rows[0] if rows else {}).get("cnt") or 0)


def get_mysql_provider(
    tenant: Tenant | None = None,
    config: Settings = settings,
) -> MySQLArcheryProvider | None:
    """按租户切 MySQL 库；mock 模式或显式关闭时不提供。"""
    if config.data_provider != "archery" or not config.mysql_enabled:
        return None
    database = tenant.mysql_db if tenant else config.mysql_db
    tenant_id = tenant.tenant_id if tenant else config.doris_tenant_id
    try:
        return MySQLArcheryProvider(config, database=database, tenant_id=tenant_id)
    except ValueError:
        return None


def get_provider(config: Settings = settings) -> EnergyProvider:
    if config.data_provider == "archery":
        return ArcheryProvider(config)
    if config.data_provider == "mock":
        return MockProvider(config)
    raise ValueError(
        f"未知 DATA_PROVIDER={config.data_provider!r}，只支持 archery / mock；"
        "本实现不提供 Doris 直连"
    )


def get_tenant_provider(
    tenant: Tenant, config: Settings = settings
) -> EnergyProvider | None:
    """按租户目录切换 Archery 查询的 Doris 库；mock 模式没有其他租户数据。"""
    if config.data_provider != "archery":
        return None
    tenant_config = replace(
        config,
        doris_db=tenant.doris_db,
        doris_tenant_id=tenant.tenant_id,
    )
    return ArcheryProvider(tenant_config)
