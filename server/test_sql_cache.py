"""SQL 结果缓存单测（不连库）。"""
from __future__ import annotations

import unittest
from datetime import date

from sql_cache import SqlResultCache
from sqlguard import QuerySource
from toolbox import AgentToolbox

SQL = (
    "SELECT COUNT(1) AS cnt FROM hengtong_dm.dm_eo_e_meter_di "
    "WHERE tenant_id = 1041 AND data_time >= '2026-11-18' "
    "AND data_time < '2026-11-19'"
)


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def ad_hoc_query(self, sql: str, limit: int = 200):
        self.calls += 1
        return [{"cnt": 42}]


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


class SqlCacheTest(unittest.IsolatedAsyncioTestCase):
    def test_key_includes_limit_and_normalizes_whitespace(self) -> None:
        first = SqlResultCache.key("doris", "db", "SELECT  1", 200)
        second = SqlResultCache.key("doris", "db", "SELECT 1", 200)
        third = SqlResultCache.key("doris", "db", "SELECT 1", 500)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_ttl_and_lru(self) -> None:
        cache = SqlResultCache(ttl_seconds=60, max_entries=1)
        cache.put("a", [{"x": 1}])
        cache.put("b", [{"x": 2}])
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))

    def test_expired_entry_misses(self) -> None:
        cache = SqlResultCache(ttl_seconds=60)
        cache.put("a", [{"x": 1}])
        cache._entries["a"].created_at -= 120  # type: ignore[attr-defined]
        self.assertIsNone(cache.get("a"))

    def test_disabled_cache_never_hits(self) -> None:
        cache = SqlResultCache(enabled=False)
        cache.put("a", [{"x": 1}])
        self.assertIsNone(cache.get("a"))

    async def test_toolbox_second_query_uses_cache(self) -> None:
        provider = FakeProvider()
        cache = SqlResultCache(ttl_seconds=60)
        box = AgentToolbox(
            sources=[make_source(provider)],
            today=date(2026, 11, 19),
            sql_cache=cache,
        )
        first = await box.execute("run_sql", {"source": "doris", "sql": SQL})
        second = await box.execute("run_sql", {"source": "doris", "sql": SQL})
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(provider.calls, 1)
        self.assertIn("缓存命中", second.summary)
        self.assertTrue(second.facts["cached"])
        self.assertEqual(cache.stats()["hits"], 1)


if __name__ == "__main__":
    unittest.main()
