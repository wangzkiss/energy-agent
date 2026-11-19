"""只读 SQL 校验与数据源描述。

这里是调查类工具共用的安全边界：
- 只允许 SELECT / WITH / SHOW / DESC；
- 单语句、禁止注释、禁止系统库、禁止写操作；
- 只允许访问当前租户的当前库；禁止引用其它租户库；
- 有 tenant_id 的数据源必须带 tenant_id = 当前租户；
- 事实表必须带时间过滤；
- 自动限行。

investigator.py 与 toolbox.py 都从这里导入，避免各地重复实现一套规则。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tenants import TENANT_CATALOG

ROW_LIMIT = 200
MAX_SQL_LENGTH = 5000

DEFAULT_TIME_COLUMNS = ("data_time", "create_time", "alarm_time", "reading_time")
DORIS_FACT_TABLES = {
    "dm_eo_e_meter_di",
    "dwi_eo_d_point_data",
    "eo_device_control_task",
    "dim_eo_alarms",
    "dm_eo_abnormal_meter_reading_record",
}
MYSQL_FACT_TABLES: dict[str, tuple[str, ...]] = {
    "eo_alarms": ("alarm_time", "create_time"),
    "eo_device_control_task": ("create_time",),
    "eo_device_control_log": ("exec_time", "create_time"),
    "eo_energy_meter_readings": ("reading_time", "create_time"),
    "eo_ticket": ("created_at", "start_time", "end_time", "deadline_time"),
    "eo_ai_strategy": ("create_time",),
    "eo_device_real_readings": ("data_time", "create_time"),
}
SYSTEM_DATABASES = {
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
    "energyOperation",
    "nacos",
    "airflow",
    "airflow_db",
    "keycloak",
    "xxl_job",
}
TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")

_FENCE = "\x60" * 3


class SQLGuardError(ValueError):
    pass


@dataclass(frozen=True)
class QuerySource:
    """一个可被调查 Agent 使用的只读数据源。"""

    key: str
    label: str
    database: str
    tenant_id: int
    project_id: int
    provider: Any
    schema_text: str
    fact_tables: dict[str, tuple[str, ...]] = field(default_factory=dict)
    require_tenant: bool = True
    forbidden_databases: frozenset[str] = frozenset()


def forbidden_databases(current: str) -> frozenset[str]:
    """除当前库之外，禁止模型显式引用的库名（含其它租户库与系统库）。"""
    names: set[str] = set(SYSTEM_DATABASES)
    for tenant in TENANT_CATALOG:
        names.add(tenant.mysql_db)
        names.add(tenant.doris_db)
    names.discard(current)
    return frozenset(names)


def validate_readonly_sql(
    sql: str,
    *,
    database: str,
    tenant_id: int,
    fact_tables: set[str] | dict[str, tuple[str, ...]] | None = None,
    require_tenant: bool = True,
    forbidden_databases: frozenset[str] | set[str] = frozenset(),
) -> str:
    """校验并规范化模型生成的只读 SQL；失败时抛 SQLGuardError。"""
    text = (sql or "").strip()
    if text.startswith(_FENCE):
        text = re.sub(r"^" + _FENCE + r"(?:sql)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*" + _FENCE + r"$", "", text).strip()
    if not text:
        raise SQLGuardError("SQL 为空")
    if len(text) > MAX_SQL_LENGTH:
        raise SQLGuardError("SQL 过长")
    if "%s" in text:
        raise SQLGuardError("不允许使用 %s 占位符")
    if ";" in text.rstrip(";"):
        raise SQLGuardError("只允许单条 SQL")
    text = text.rstrip(";").strip()
    if "--" in text or "/*" in text or "#" in text:
        raise SQLGuardError("不允许 SQL 注释")
    if re.search(r"\b(information_schema|mysql|sys|__internal_schema)\b", text, re.I):
        raise SQLGuardError("不允许访问系统库")

    first = re.match(r"^([A-Za-z]+)", text)
    verb = first.group(1).lower() if first else ""
    if verb not in {"select", "with", "show", "desc", "describe"}:
        raise SQLGuardError("只允许 SELECT / WITH / SHOW / DESC")
    if verb == "show" and not re.match(
        r"^show\s+(full\s+)?(tables|columns|create\s+table)\b", text, re.I
    ):
        raise SQLGuardError(
            "只允许 SHOW TABLES / SHOW COLUMNS / SHOW FULL COLUMNS / SHOW CREATE TABLE"
        )

    forbidden = re.search(
        r"\b(insert|update|delete|drop|alter|create|truncate|replace|merge|grant|"
        r"revoke|call|load|export|outfile|set|use|kill|analyze|optimize|refresh|"
        r"rename|lock|unlock|commit|rollback|union|or)\b",
        text,
        re.I,
    )
    if forbidden:
        raise SQLGuardError(f"不允许写操作或危险语句: {forbidden.group(1)}")

    if forbidden_databases and verb in {"select", "with"}:
        for name in forbidden_databases:
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*\.", text, re.I
            ):
                raise SQLGuardError(f"不允许跨库访问: {name}")

    if verb == "show":
        source_match = re.search(
            r"\b(?:from|in)\s+([A-Za-z0-9_]+)", text, re.I
        )
        if source_match and source_match.group(1) != database:
            raise SQLGuardError("SHOW 只允许查看当前库")
        # SHOW COLUMNS 不支持 LIMIT；模型偶尔会误加，直接去掉，避免无意义失败。
        return re.sub(r"\s+limit\s+\d+\s*$", "", text, flags=re.I)

    if verb in {"desc", "describe"}:
        source_match = re.search(
            r"^\s*desc(?:ribe)?\s+([A-Za-z0-9_]+)\s*\.", text, re.I
        )
        if source_match and source_match.group(1) != database:
            raise SQLGuardError("DESC 只允许查看当前库")
        return text

    if verb in {"select", "with"}:
        if f"{database}." not in text:
            raise SQLGuardError(f"表名必须使用当前库全限定名，例如 {database}.table_name")

        if require_tenant:
            tenant_filter = re.search(
                r"\btenant_id\s*=\s*" + re.escape(str(tenant_id)) + r"(?!\d)",
                text,
                re.I,
            )
            if not tenant_filter:
                raise SQLGuardError(f"SELECT 必须带 tenant_id = {tenant_id}")
        tenant_values = re.findall(r"\btenant_id\s*=\s*(\d+)", text, re.I)
        if any(value != str(tenant_id) for value in tenant_values):
            raise SQLGuardError("不允许在同一 SQL 中查询其它 tenant_id")
        if re.search(r"\btenant_id\s+(?:!=|<>|in)\b", text, re.I):
            raise SQLGuardError("tenant_id 只允许等值过滤")

        if fact_tables:
            if isinstance(fact_tables, dict):
                checks = fact_tables.items()
            else:
                checks = ((table, DEFAULT_TIME_COLUMNS) for table in fact_tables)
            for table, columns in checks:
                if f"{database}.{table}" in text:
                    if not any(
                        re.search(rf"\b{re.escape(column)}\b", text, re.I)
                        for column in columns
                    ):
                        raise SQLGuardError(
                            f"事实表 {table} 查询必须带时间过滤: {' / '.join(columns)}"
                        )

        limit_match = re.search(r"\blimit\s+(\d+)\b", text, re.I)
        if limit_match:
            if int(limit_match.group(1)) > ROW_LIMIT:
                raise SQLGuardError(f"LIMIT 不能超过 {ROW_LIMIT}")
        else:
            text = f"{text}\nLIMIT {ROW_LIMIT}"

    return text
