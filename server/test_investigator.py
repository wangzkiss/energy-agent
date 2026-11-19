"""SQL Guard 与小时表完整性答案的轻量单测；不连接数据库。"""
from __future__ import annotations

import unittest
from datetime import date

from agent import EnergyAgent, detect_intent, should_investigate
from investigator import (
    MYSQL_FACT_TABLES,
    SQLGuardError,
    validate_readonly_sql,
)


class ReadonlySQLGuardTest(unittest.TestCase):
    def test_select_auto_limit(self) -> None:
        sql = validate_readonly_sql(
            "SELECT meter_id FROM nwdy_dm.dm_eo_e_meter_di "
            "WHERE tenant_id = 1026 AND data_time = '2026-11-19'",
            database="nwdy_dm",
            tenant_id=1026,
            fact_tables={"dm_eo_e_meter_di"},
        )
        self.assertIn("LIMIT 200", sql)

    def test_reject_other_tenant_or(self) -> None:
        with self.assertRaises(SQLGuardError):
            validate_readonly_sql(
                "SELECT * FROM nwdy_dm.dm_eo_e_meter_di "
                "WHERE tenant_id = 1026 OR tenant_id = 1033 "
                "AND data_time = '2026-11-19' LIMIT 10",
                database="nwdy_dm",
                tenant_id=1026,
                fact_tables={"dm_eo_e_meter_di"},
            )

    def test_reject_union_and_write(self) -> None:
        for sql in (
            "SELECT * FROM nwdy_dm.dm_eo_e_meter_di WHERE tenant_id = 1026 "
            "AND data_time = '2026-11-19' UNION SELECT 1 LIMIT 10",
            "DELETE FROM nwdy_dm.dm_eo_e_meter_di WHERE tenant_id = 1026",
            "SHOW GRANTS",
        ):
            with self.assertRaises(SQLGuardError):
                validate_readonly_sql(
                    sql,
                    database="nwdy_dm",
                    tenant_id=1026,
                    fact_tables={"dm_eo_e_meter_di"},
                )

    def test_show_columns_allowed(self) -> None:
        self.assertIn(
            "SHOW COLUMNS",
            validate_readonly_sql(
                "SHOW COLUMNS FROM nwdy_dm.dm_eo_e_meter_hi",
                database="nwdy_dm",
                tenant_id=1026,
                fact_tables={"dm_eo_e_meter_hi"},
            ),
        )


class MySQLSQLGuardTest(unittest.TestCase):
    def test_mysql_table_without_tenant_id_is_allowed_for_ticket(self) -> None:
        sql = validate_readonly_sql(
            "SELECT id, ticket_no FROM nwdy.eo_ticket "
            "WHERE created_at >= '2026-11-01' LIMIT 20",
            database="nwdy",
            tenant_id=1026,
            fact_tables=MYSQL_FACT_TABLES,
            require_tenant=False,
            forbidden_databases={"rongyao", "rongyao_dm"},
        )
        self.assertIn("LIMIT 20", sql)

    def test_mysql_fact_table_requires_time_filter(self) -> None:
        with self.assertRaises(SQLGuardError):
            validate_readonly_sql(
                "SELECT COUNT(*) FROM nwdy.eo_alarms WHERE tenant_id = 1026",
                database="nwdy",
                tenant_id=1026,
                fact_tables=MYSQL_FACT_TABLES,
                require_tenant=False,
            )

    def test_mysql_show_full_columns_and_remove_limit(self) -> None:
        sql = validate_readonly_sql(
            "SHOW FULL COLUMNS FROM nwdy.eo_ticket LIMIT 200",
            database="nwdy",
            tenant_id=1026,
            fact_tables=MYSQL_FACT_TABLES,
            require_tenant=False,
        )
        self.assertNotIn("LIMIT", sql.upper())
        with self.assertRaises(SQLGuardError):
            validate_readonly_sql(
                "DESC rongyao.eo_devices",
                database="nwdy",
                tenant_id=1026,
                require_tenant=False,
            )

    def test_mysql_rejects_other_tenant_database(self) -> None:
        with self.assertRaises(SQLGuardError):
            validate_readonly_sql(
                "SELECT COUNT(*) FROM nwdy.eo_devices a, rongyao.eo_devices b "
                "WHERE a.tenant_id = 1026",
                database="nwdy",
                tenant_id=1026,
                fact_tables=MYSQL_FACT_TABLES,
                require_tenant=False,
                forbidden_databases={"rongyao", "rongyao_dm"},
            )
        with self.assertRaises(SQLGuardError):
            validate_readonly_sql(
                "SHOW TABLES IN rongyao",
                database="nwdy",
                tenant_id=1026,
                require_tenant=False,
            )

    def test_mysql_intent_routes_to_investigator(self) -> None:
        self.assertEqual(detect_intent("南网能源 MySQL 里有多少设备？"), "mysql")
        self.assertTrue(should_investigate("南网能源 MySQL 控制任务分布"))


class IntentRoutingTest(unittest.TestCase):
    def test_data_question_with_abnormal_is_open(self) -> None:
        self.assertTrue(should_investigate("南网能源今天有哪些异常表计"))
        self.assertFalse(should_investigate("你好"))

    def test_cold_station_intent(self) -> None:
        self.assertEqual(detect_intent("南网能源配置了哪些冷站？"), "cold_station")
        self.assertEqual(detect_intent("双汇的冷源系统有哪些？"), "cold_station")


class HourlyAnswerTest(unittest.TestCase):
    def test_past_day_complete(self) -> None:
        rows = [
            {"data_time": f"2026-11-18 {hour:02d}:00:00", "row_count": 10, "meter_count": 10}
            for hour in range(24)
        ]
        answer = EnergyAgent._hourly_quality_answer(
            {"rows": rows, "source": "nwdy_dm.dm_eo_e_meter_hi"},
            date(2026, 11, 18),
            "昨天",
        )
        self.assertIn("未发现缺数", answer)
        self.assertNotIn("09:00 尚未", answer)

    def test_past_day_missing_hour(self) -> None:
        rows = [
            {"data_time": f"2026-11-18 {hour:02d}:00:00", "row_count": 10, "meter_count": 10}
            for hour in range(23)
        ]
        answer = EnergyAgent._hourly_quality_answer(
            {"rows": rows, "source": "nwdy_dm.dm_eo_e_meter_hi"},
            date(2026, 11, 18),
            "昨天",
        )
        self.assertIn("发现疑似缺数", answer)
        self.assertIn("23:00", answer)


if __name__ == "__main__":
    unittest.main()
