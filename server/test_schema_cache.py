"""schema 快照缓存与自适应步数的单测（不连库）。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date

from config import settings
from investigator import DataInvestigator
from schema_cache import SchemaCache
from sqlguard import QuerySource
from toolbox import AgentToolbox


class FakeProvider:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def ad_hoc_query(self, sql: str, limit: int = 200):
        self.sqls.append(sql)
        if "information_schema.TABLES" in sql:
            return [
                {"TABLE_NAME": "algo_overview_kpi"},
                {"TABLE_NAME": "dm_eo_e_meter_di"},
            ]
        if "information_schema.COLUMNS" in sql:
            return [
                {
                    "TABLE_NAME": "algo_overview_kpi",
                    "COLUMN_NAME": "system_scop",
                    "COLUMN_TYPE": "double",
                    "COLUMN_COMMENT": "系统SCOP",
                },
                {
                    "TABLE_NAME": "algo_overview_kpi",
                    "COLUMN_NAME": "data_time",
                    "COLUMN_TYPE": "datetime",
                    "COLUMN_COMMENT": "时间",
                },
                {
                    "TABLE_NAME": "dm_eo_e_meter_di",
                    "COLUMN_NAME": "meter_id",
                    "COLUMN_TYPE": "bigint",
                    "COLUMN_COMMENT": "表计",
                },
            ]
        return []


def make_source(provider: FakeProvider) -> QuerySource:
    return QuerySource(
        key="doris",
        label="Doris",
        database="hengtong_dm",
        tenant_id=1041,
        project_id=1,
        provider=provider,
        schema_text="",
    )


class SchemaCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_and_search(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as directory:
            cache = SchemaCache(directory)
            snapshot = await cache.get(make_source(provider))
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(
                snapshot.table_names(), ["algo_overview_kpi", "dm_eo_e_meter_di"]
            )
            hits = snapshot.search(["scop"])
            self.assertEqual(hits[0]["COLUMN_NAME"], "system_scop")
            self.assertEqual(snapshot.time_column("algo_overview_kpi"), "data_time")

    async def test_memory_reuse(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as directory:
            cache = SchemaCache(directory)
            await cache.get(make_source(provider))
            calls = len(provider.sqls)
            await cache.get(make_source(provider))
            self.assertEqual(len(provider.sqls), calls)

    async def test_disk_reload_without_query(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as directory:
            cache = SchemaCache(directory)
            await cache.get(make_source(provider))
            provider.sqls.clear()
            reloaded = SchemaCache(directory)
            snapshot = await reloaded.get(make_source(provider))
            self.assertIsNotNone(snapshot)
            self.assertEqual(provider.sqls, [])

    async def test_toolbox_search_uses_snapshot(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as directory:
            cache = SchemaCache(directory)
            box = AgentToolbox(
                sources=[make_source(provider)],
                today=date(2026, 11, 19),
                schema_cache=cache,
            )
            outcome = await box.execute("search_columns", {"keyword": "scop"})
            self.assertTrue(outcome.ok)
            self.assertIn("system_scop", outcome.summary)
            # 首次查询走实时库并在后台建快照；等快照完成后再查应完全命中缓存。
            if box._schema_tasks:
                await asyncio.gather(*box._schema_tasks.values())
            calls_after_build = len(provider.sqls)
            again = await box.execute("search_columns", {"keyword": "meter"})
            self.assertIn("meter_id", again.summary)
            self.assertEqual(len(provider.sqls), calls_after_build)


class AdaptiveStepsTest(unittest.TestCase):
    def test_soft_steps_capped_by_max(self) -> None:
        investigator = DataInvestigator(llm=None, config=settings, max_steps=12)
        self.assertEqual(investigator.soft_steps, 8)
        shorter = DataInvestigator(llm=None, config=settings, max_steps=5)
        self.assertEqual(shorter.soft_steps, 5)


if __name__ == "__main__":
    unittest.main()
