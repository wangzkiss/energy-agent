"""search_columns 元数据检索的单测（不连库）。"""
from __future__ import annotations

import unittest
from datetime import date

from sqlguard import QuerySource
from toolbox import AgentToolbox


class FakeProvider:
    def __init__(self, rows):
        self.rows = rows
        self.sqls: list[str] = []

    async def ad_hoc_query(self, sql: str, limit: int = 200):
        self.sqls.append(sql)
        return self.rows


class SearchColumnsTest(unittest.IsolatedAsyncioTestCase):
    async def test_hits_table_and_column(self) -> None:
        provider = FakeProvider(
            [
                {
                    "TABLE_NAME": "algo_overview_kpi",
                    "COLUMN_NAME": "system_scop",
                    "COLUMN_TYPE": "double",
                    "COLUMN_COMMENT": "系统SCOP",
                }
            ]
        )
        source = QuerySource(
            key="doris",
            label="Doris",
            database="hengtong_dm",
            tenant_id=1041,
            project_id=1,
            provider=provider,
            schema_text="",
        )
        box = AgentToolbox(sources=[source], today=date(2026, 11, 19))
        outcome = await box.execute("search_columns", {"keyword": "SCOP"})
        self.assertTrue(outcome.ok)
        self.assertIn("algo_overview_kpi.system_scop", outcome.summary)
        self.assertIn("系统SCOP", outcome.summary)
        self.assertIn("information_schema.COLUMNS", provider.sqls[0])
        self.assertIn("hengtong_dm", provider.sqls[0])

    async def test_empty_keyword_rejected(self) -> None:
        source = QuerySource(
            key="doris",
            label="Doris",
            database="hengtong_dm",
            tenant_id=1041,
            project_id=1,
            provider=FakeProvider([]),
            schema_text="",
        )
        box = AgentToolbox(sources=[source], today=date(2026, 11, 19))
        outcome = await box.execute("search_columns", {"keyword": "  "})
        self.assertFalse(outcome.ok)


if __name__ == "__main__":
    unittest.main()
