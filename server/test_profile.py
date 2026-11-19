"""table_profile / find_tables 真实库兜底的单元测试（不连库）。"""
from __future__ import annotations

import unittest
from datetime import date

from sqlguard import QuerySource
from toolbox import AgentToolbox


class ScriptedProvider:
    def __init__(self, rows_count: int = 0):
        self.rows_count = rows_count
        self.sqls: list[str] = []

    async def ad_hoc_query(self, sql: str, limit: int = 200):
        self.sqls.append(sql)
        if sql.strip().upper() == "SHOW TABLES":
            return [{"Tables_in_x": "algo_overview_kpi"}, {"Tables_in_x": "algo_unknown_metric"}]
        if "information_schema.COLUMNS" in sql:
            return [
                {
                    "TABLE_NAME": "algo_unknown_metric",
                    "COLUMN_NAME": "system_scop",
                    "COLUMN_TYPE": "double",
                    "COLUMN_COMMENT": "系统SCOP",
                }
            ]
        if "COUNT(*) AS rows_cnt" in sql:
            return [{"rows_cnt": self.rows_count}]
        if "MIN(" in sql:
            return [{"min_t": None, "max_t": None}]
        if "COUNT(DISTINCT tenant_id)" in sql:
            raise RuntimeError("Unknown column 'tenant_id'")
        return []


def make_box(provider: ScriptedProvider) -> AgentToolbox:
    source = QuerySource(
        key="doris",
        label="Doris",
        database="hengtong_dm",
        tenant_id=1041,
        project_id=1,
        provider=provider,
        schema_text="",
    )
    return AgentToolbox(sources=[source], today=date(2026, 11, 19))


class TableProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_table_is_flagged(self) -> None:
        box = make_box(ScriptedProvider(rows_count=0))
        outcome = await box.execute("table_profile", {"table": "algo_overview_kpi"})
        self.assertTrue(outcome.ok)
        self.assertIn("0 行", outcome.summary)
        self.assertIn("空表", outcome.summary)
        self.assertIn("不能当 0 值", outcome.summary)
        self.assertTrue(outcome.facts["empty"])
        self.assertIsNone(outcome.facts["tenant_count"])

    async def test_non_empty_table(self) -> None:
        box = make_box(ScriptedProvider(rows_count=42))
        outcome = await box.execute("table_profile", {"table": "algo_overview_kpi"})
        self.assertIn("42 行", outcome.summary)
        self.assertIn("有数据", outcome.summary)
        self.assertFalse(outcome.facts["empty"])


class FindTablesFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_uncurated_table_found_by_column(self) -> None:
        box = make_box(ScriptedProvider())
        outcome = await box.execute("find_tables", {"keyword": "system_scop"})
        self.assertTrue(outcome.ok)
        self.assertIn("algo_unknown_metric", outcome.summary)
        self.assertIn("字段兜底", outcome.summary)


if __name__ == "__main__":
    unittest.main()
