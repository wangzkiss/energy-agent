"""工具箱与自主调查循环的轻量单测；不连接数据库、不调用真实模型。"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date

from agent import EnergyAgent, detect_intent
from config import settings
from investigator import DataInvestigator, QuerySource
from providers import MockProvider
from semantic import plan_question
from sqlguard import MYSQL_FACT_TABLES, forbidden_databases
from toolbox import AgentToolbox


class FakeProvider:
    """记录调用并返回固定结果，用来隔离数据库。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ad_hoc_query(self, sql: str, limit: int = 200) -> list[dict]:
        self.calls.append(sql)
        if sql.upper().startswith("SHOW TABLES"):
            return [{"Tables_in_nwdy_dm": "dm_eo_e_meter_di"}, {"Tables_in_nwdy_dm": "dim_eo_d_device"}]
        if sql.upper().startswith("SHOW FULL COLUMNS"):
            return [
                {"Field": "meter_id", "Type": "bigint", "Comment": "表计 ID"},
                {"Field": "energy_value", "Type": "decimal", "Comment": "用电量"},
            ]
        return [{"meter_id": 1, "energy_value": 12.5}]

    async def energy_total(self, start: date, end: date) -> dict:
        return {"value": 100.0, "active_meters": 3, "start": str(start), "end": str(end)}

    async def energy_by_space(self, start: date, end: date, limit: int = 30) -> dict:
        return {
            "available": True,
            "spaces": [{"space_name": "A1座", "space_code": "10001", "energy_value": 100.0, "meters": 3}],
            "space_count": 1,
            "total": 100.0,
            "note": "只有 A1座，楼层未拆分",
        }

    async def energy_by_floor(self, start: date, end: date, limit: int = 50) -> dict:
        return {
            "available": True,
            "floors": [{"floor": "19", "energy_value": 1003.32, "meters": 8}],
            "floor_count": 1,
            "total": 4102.44,
            "covered": 1003.32,
            "unmatched": 3099.12,
            "coverage_pct": 24.5,
            "meters": 758,
            "note": "test",
        }

    async def device_readings(self, keyword="", point_codes=(), hours=24, limit=200) -> dict:
        return {
            "available": bool(keyword),
            "rows": [
                {
                    "device_id": 1,
                    "device_name": keyword + "空调面板",
                    "space_name": "35层",
                    "point_code": "RoomTemp",
                    "avg": 25.5,
                    "min": 24.0,
                    "max": 27.0,
                    "samples": 100,
                    "active_hours": 12,
                    "last_time": "2026-11-19 13:00:00",
                }
            ],
            "count": 1,
            "keyword": keyword,
            "point_codes": list(point_codes),
            "window_hours": hours,
            "note": "test",
        }


def doris_source(provider: FakeProvider) -> QuerySource:
    return QuerySource(
        key="doris",
        label="Doris 分析库",
        database="nwdy_dm",
        tenant_id=1026,
        project_id=1,
        provider=provider,
        schema_text="- nwdy_dm.dm_eo_e_meter_di: 日用电",
        fact_tables={"dm_eo_e_meter_di": ("data_time",)},
        require_tenant=True,
        forbidden_databases=forbidden_databases("nwdy_dm"),
    )


class FakeLLM:
    configured = True
    model = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, **kwargs) -> str:
        self.prompts.append(user)
        if not self.replies:
            return '{"action":"final","answer":"（没有更多脚本）"}'
        return self.replies.pop(0)


class ToolboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeProvider()
        self.box = AgentToolbox(sources=[doris_source(self.provider)], today=date(2026, 11, 19))

    def test_find_tables_uses_show_tables(self) -> None:
        outcome = asyncio.run(self.box.execute("find_tables", {"keyword": "meter"}))
        self.assertTrue(outcome.ok)
        self.assertIn("dm_eo_e_meter_di", outcome.summary)
        self.assertIn("SHOW TABLES", self.provider.calls[0])

    def test_describe_table_formats_columns(self) -> None:
        outcome = asyncio.run(self.box.execute("describe_table", {"table": "dm_eo_e_meter_di"}))
        self.assertTrue(outcome.ok)
        self.assertIn("meter_id", outcome.summary)
        self.assertIn("表计 ID", outcome.summary)

    def test_run_sql_requires_tenant_filter(self) -> None:
        outcome = asyncio.run(
            self.box.execute(
                "run_sql",
                {
                    "source": "doris",
                    "sql": "SELECT * FROM nwdy_dm.dm_eo_e_meter_di "
                    "WHERE data_time = '2026-11-18' LIMIT 5",
                },
            )
        )
        self.assertFalse(outcome.ok)
        self.assertIn("tenant_id", outcome.error or "")
        self.assertEqual(self.provider.calls, [])

    def test_run_sql_ok(self) -> None:
        outcome = asyncio.run(
            self.box.execute(
                "run_sql",
                {
                    "source": "doris",
                    "sql": "SELECT meter_id FROM nwdy_dm.dm_eo_e_meter_di "
                    "WHERE tenant_id = 1026 AND data_time = '2026-11-18' LIMIT 5",
                },
            )
        )
        self.assertTrue(outcome.ok)
        self.assertIn("meter_id", outcome.summary)

    def test_read_code_whitelist(self) -> None:
        ok = self.box.execute("read_code", {"query": "energy_total", "path": "server/providers.py"})
        outcome = asyncio.run(ok)
        self.assertTrue(outcome.ok)
        self.assertIn("energy_total", outcome.summary)
        blocked = asyncio.run(self.box.execute("read_code", {"query": "x", "path": "../secret.txt"}))
        self.assertFalse(blocked.ok)

    def test_knowledge_and_metric(self) -> None:
        knowledge = asyncio.run(self.box.execute("knowledge", {"query": "总用电 口径"}))
        self.assertTrue(knowledge.ok)
        self.assertTrue(knowledge.summary.strip())
        metric = asyncio.run(
            self.box.execute("metric", {"metric": "energy_total", "start": "2026-11-18"})
        )
        self.assertTrue(metric.ok)
        self.assertIn("100.0", metric.summary)
        unknown = asyncio.run(self.box.execute("metric", {"metric": "nope"}))
        self.assertFalse(unknown.ok)

    def test_space_energy_and_device_readings(self) -> None:
        space = asyncio.run(
            self.box.execute("metric", {"metric": "space_energy", "start": "2026-11-18"})
        )
        self.assertTrue(space.ok)
        self.assertIn("A1座", space.summary)
        self.assertIn("楼层未拆分", space.summary)
        readings = asyncio.run(
            self.box.execute(
                "metric",
                {
                    "metric": "device_readings",
                    "keyword": "36层",
                    "point_codes": "RoomTemp,RoomTempSp",
                    "hours": 6,
                },
            )
        )
        self.assertTrue(readings.ok)
        self.assertIn("36层空调面板", readings.summary)
        self.assertIn("最近 6 小时", readings.summary)

    def test_floor_energy_metric(self) -> None:
        out = asyncio.run(
            self.box.execute("metric", {"metric": "floor_energy", "start": "2026-11-18"})
        )
        self.assertTrue(out.ok)
        self.assertIn("19", out.summary)
        self.assertIn("24.5%", out.summary)

    def test_knowledge_finds_tool_metrics(self) -> None:
        out = asyncio.run(
            self.box.execute("knowledge", {"query": "最近一周哪些设备一直在运行"})
        )
        self.assertTrue(out.ok)
        self.assertIn("running_devices", out.summary)

    def test_unknown_tool(self) -> None:
        outcome = asyncio.run(self.box.execute("shell", {}))
        self.assertFalse(outcome.ok)
        self.assertIn("未知工具", outcome.error or "")


class ToolLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        # 调查循环共用进程级 SQL 缓存；每个用例从空缓存开始，避免互相命中。
        from caches import SQL_CACHE

        SQL_CACHE.clear()

    def test_metric_then_final(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"tool","tool":"metric","args":{"metric":"energy_total",'
                '"start":"2026-11-18","end":"2026-11-18"}}',
                '{"action":"final","answer":"### 结论\\n昨天 100 kWh。"}',
            ]
        )
        investigator = DataInvestigator(llm, settings, today=date(2026, 11, 19))
        result = asyncio.run(
            investigator.investigate_sources("昨天总用电多少", [doris_source(FakeProvider())])
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("100 kWh", result.answer)
        self.assertIn("查询记录", result.answer)
        self.assertEqual(len(result.tool_log), 1)
        self.assertEqual(result.tool_log[0]["tool"], "metric")

    def test_legacy_run_sql_protocol(self) -> None:
        provider = FakeProvider()
        llm = FakeLLM(
            [
                '{"action":"run_sql","source":"doris","sql":"SELECT meter_id FROM '
                'nwdy_dm.dm_eo_e_meter_di WHERE tenant_id = 1026 AND '
                "data_time = '2026-11-18' LIMIT 5\"}",
                '{"action":"final","answer":"### 结论\\n查到 1 块表。"}',
            ]
        )
        investigator = DataInvestigator(llm, settings, today=date(2026, 11, 19))
        result = asyncio.run(
            investigator.investigate_sources("查一下表计", [doris_source(provider)])
        )
        assert result is not None
        self.assertIn("查到 1 块表", result.answer)
        self.assertTrue(any("SELECT meter_id" in sql for sql in provider.calls))
        import json
        import re

        match = re.search(r"```query-log\n(.+?)\n```", result.answer, re.S)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        self.assertEqual(payload["records"][0]["tool"], "run_sql")
        self.assertIn("SELECT meter_id", payload["records"][0]["sql"])
        self.assertTrue(payload["records"][0]["ok"])

    def test_bad_json_falls_back_without_crashing(self) -> None:
        llm = FakeLLM(["不是 JSON", "仍然不是 JSON", "还是不是 JSON"])
        investigator = DataInvestigator(llm, settings, today=date(2026, 11, 19))
        result = asyncio.run(
            investigator.investigate_sources("随便问问", [doris_source(FakeProvider())])
        )
        assert result is not None
        self.assertIn("结论", result.answer)
        self.assertIn("没有拿到可用的数据结果", result.answer)

    def test_duplicate_call_is_skipped(self) -> None:
        provider = FakeProvider()
        sql = (
            "SELECT meter_id FROM nwdy_dm.dm_eo_e_meter_di WHERE tenant_id = 1026 "
            "AND data_time = '2026-11-18' LIMIT 5"
        )
        call = '{"action":"tool","tool":"run_sql","args":{"source":"doris","sql":"' + sql + '"}}'
        llm = FakeLLM([call, call, '{"action":"final","answer":"### 结论\\n完成。"}'])
        investigator = DataInvestigator(llm, settings, today=date(2026, 11, 19))
        result = asyncio.run(
            investigator.investigate_sources("查表计", [doris_source(provider)])
        )
        assert result is not None
        queried = [sql for sql in provider.calls if sql.upper().startswith("SELECT")]
        self.assertEqual(len(queried), 1)


class RoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = EnergyAgent(MockProvider(), FakeLLM([]), today=date(2026, 11, 19))

    def _loop(self, question: str) -> bool:
        plan = plan_question(question)
        return self.agent._should_use_tool_loop(question, plan, detect_intent(question))

    def test_floor_ranking_goes_to_loop(self) -> None:
        self.assertTrue(self._loop("南网能源昨天用电最多的楼层是哪个？"))

    def test_compare_goes_to_loop(self) -> None:
        self.assertTrue(self._loop("南网能源 A1座 和 36 层昨天用电对比"))

    def test_panel_count_goes_to_loop(self) -> None:
        self.assertTrue(self._loop("南网能源有多少台空调面板？"))

    def test_room_temperature_compare_goes_to_loop(self) -> None:
        self.assertTrue(self._loop("南网能源 35 层和 36 层室温对比"))
        self.assertFalse(self._loop("南网能源 36 层现在室温多少？"))

    def test_comparison_goes_to_loop(self) -> None:
        self.assertTrue(self._loop("南网能源 35 层和 36 层昨天用电谁高？"))
        self.assertTrue(self._loop("南网能源和 36 层用电对比"))
        self.assertFalse(self._loop("南网能源昨天总用电多少？"))

    def test_plain_total_stays_on_template(self) -> None:
        self.assertFalse(self._loop("南网能源昨天总用电多少？"))
        self.assertFalse(self._loop("南网能源昨天控制成功率多少？"))
        self.assertFalse(self._loop("南网能源昨天总用电查哪张表？"))

    def test_unavailable_detection(self) -> None:
        self.assertTrue(
            EnergyAgent._is_unavailable("alarm", {"result": {"available": False}})
        )
        self.assertFalse(
            EnergyAgent._is_unavailable("alarm", {"result": {"available": True, "count": 0}})
        )
        self.assertTrue(
            EnergyAgent._needs_escalation("room_temp", {"result": {"available": False}})
        )


if __name__ == "__main__":
    unittest.main()
