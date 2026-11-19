"""租户与数据库关系。

映射来源：`energyOperation.eo_tenant_data_sources`
（`tenant_id` + `db_name`，MySQL/Doris 两类 `db_type`）。
Doris 实际库名为 `db_name + "_dm"`。

这里保留一份静态目录，避免 BFF 为了回答“支持哪些租户”再去连 MySQL 配置库；
生产接入新租户时，同步维护本文件或在部署时替换。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from config import Settings, settings


@dataclass(frozen=True)
class Tenant:
    tenant_id: int
    code: str
    name: str | None = None

    @property
    def mysql_db(self) -> str:
        return self.code

    @property
    def doris_db(self) -> str:
        return f"{self.code}_dm"


# 与现网 eo_tenant_data_sources 的 db_name 去重结果一致（15 个租户）。
# 租户名称由业务方提供；未提供名称的租户保留 None，不猜测客户名称。
TENANT_CATALOG: tuple[Tenant, ...] = (
    Tenant(1, "zjark_1", "中集安瑞科"),
    Tenant(2, "shuanghui", "双汇"),
    Tenant(3, "jagk", "晶安高科"),
    Tenant(4, "zjct", "中交城投"),
    Tenant(5, "qldl", "秦龙电力"),
    Tenant(1024, "ctl", "楚天龙"),
    Tenant(1025, "xxzd", "西峡众德"),
    Tenant(1026, "nwdy", "南网能源"),
    Tenant(1027, "shby"),
    Tenant(1028, "musi"),
    Tenant(1029, "sgyy"),
    Tenant(1033, "rongyao", "荣耀"),
    Tenant(1041, "hengtong", "亨通"),
    Tenant(1055, "chujiang", "楚江"),
    Tenant(10000, "nxys"),
)


def resolve_tenant(text: str) -> Tenant | None:
    """从问题中识别租户，支持名称、code、以及“租户+ID”。"""
    normalized = text.strip()
    lowered = normalized.lower()

    # code 优先：避免“荣耀”与英文单词里的偶然子串冲突
    for tenant in TENANT_CATALOG:
        pattern = rf"(?<![a-z0-9_]){re.escape(tenant.code.lower())}(?![a-z0-9_])"
        if re.search(pattern, lowered):
            return tenant

    # 名称按长度倒序，避免短名称先命中长名称的一部分
    named = sorted(
        (tenant for tenant in TENANT_CATALOG if tenant.name),
        key=lambda tenant: len(tenant.name or ""),
        reverse=True,
    )
    for tenant in named:
        if tenant.name and tenant.name in normalized:
            return tenant

    # 业务常用简称；只做明确的短名映射，不猜客户全称。
    aliases = {"南网": "nwdy"}
    for alias, code in aliases.items():
        if alias in normalized:
            return next((tenant for tenant in TENANT_CATALOG if tenant.code == code), None)

    # 只有明确写“租户 1026”时才按 ID 识别，避免把年份 2026 当租户
    match = re.search(r"(?:租户|tenant)\s*(\d{2,5})", normalized, re.IGNORECASE)
    if match:
        tenant_id = int(match.group(1))
        return next(
            (tenant for tenant in TENANT_CATALOG if tenant.tenant_id == tenant_id),
            None,
        )
    return None


def tenant_rows(config: Settings = settings) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    found_current = False
    for tenant in TENANT_CATALOG:
        current = tenant.tenant_id == config.doris_tenant_id
        if current:
            found_current = True
        rows.append(
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "code": tenant.code,
                "mysql_db": tenant.mysql_db,
                "doris_db": tenant.doris_db,
                "current": current,
            }
        )
    if not found_current:
        rows.append(
            {
                "tenant_id": config.doris_tenant_id,
                "name": None,
                "code": config.doris_db.removesuffix("_dm"),
                "mysql_db": config.doris_db.removesuffix("_dm"),
                "doris_db": config.doris_db,
                "current": True,
            }
        )
    return rows


def tenant_answer(config: Settings = settings) -> str:
    rows = tenant_rows(config)
    current = next((row for row in rows if row["current"]), rows[-1])
    lines = [
        "### 目前支持的租户",
        "",
        f"当前接入 **{len(rows)}** 个租户。租户编码同时是库名前缀："
        "MySQL 库名与租户编码相同，Doris 库名为租户编码加 `_dm` 后缀。",
        "",
        "| 租户名称 | 租户编码 | 租户ID | Doris 库 | 当前 |",
        "|---|---|---:|---|---|",
    ]
    for row in rows:
        name = row["name"] or "未提供"
        lines.append(
            f"| {name} | {row['code']} | {row['tenant_id']} | "
            f"`{row['doris_db']}` | {'✓' if row['current'] else ''} |"
        )
    current_name = current["name"] or "未提供"
    lines.extend(
        [
            "",
            f"当前会话使用租户 **{current_name}**"
            f"（{current['tenant_id']} / `{current['code']}`）、"
            f"项目 **{config.doris_project_id}**，查询 Doris 库 **`{current['doris_db']}`**。",
            "",
            "### 口径",
            "",
            "- 租户 ID/数据库映射来自 `energyOperation.eo_tenant_data_sources` 的 "
            "`tenant_id + db_name`；数据源表同时登记 MySQL 与 Doris 两类连接。",
            "- 租户名称来自业务方提供的映射；未提供名称的租户显示“未提供”，不猜测客户名称。",
            f"- 当前目录是静态同步（{len(TENANT_CATALOG)} 条），不会实时读取配置库；"
            "新增/变更租户需更新 `server/tenants.py`。",
            "- Doris 实际库名为 `db_name + \"_dm\"`，例如 `nwdy` → `nwdy_dm`。",
            "- “已接入”指数据源已登记；具体租户是否有数据，以实际查询结果为准。",
            "- 当前会话默认使用上述租户；ALLOW_TENANT_QUERY=true 时可按问题里的租户名"
            "切换 Doris/MySQL provider，生产仍应由网关/SSO 先完成身份校验。",
        ]
    )
    return "\n".join(lines)


__all__ = ["Tenant", "TENANT_CATALOG", "resolve_tenant", "tenant_answer", "tenant_rows"]
