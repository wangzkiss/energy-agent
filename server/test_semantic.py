"""指标语义层单测；不连接数据库。"""
from __future__ import annotations

import unittest

from semantic import METRICS, format_metric_context, metric_catalog, plan_question


class QuestionPlanTest(unittest.TestCase):
    def test_cold_station_energy_is_not_config(self) -> None:
        plan = plan_question("南网能源冷站今天用电多少")
        self.assertEqual(plan.key, "cold_station_electricity")
        self.assertEqual(plan.analysis, "value")
        self.assertGreaterEqual(plan.confidence, 0.9)

    def test_cold_station_config(self) -> None:
        plan = plan_question("南网能源配置了哪些冷站")
        self.assertEqual(plan.key, "cold_station_config")
        self.assertEqual(plan.analysis, "config")
        self.assertEqual(plan_question("南网能源有多少冷站").key, "cold_station_config")

    def test_energy_anomaly_is_not_total(self) -> None:
        plan = plan_question("南网能源今天用电异常，帮我分析原因")
        self.assertEqual(plan.key, "energy_anomaly")
        self.assertEqual(plan.analysis, "root_cause")
        self.assertIn("基线", plan.metric.description if plan.metric else "")

    def test_device_and_meter_count_are_distinct(self) -> None:
        device = plan_question("南网能源 MySQL 里有多少设备")
        meter = plan_question("南网能源 MySQL 里有多少表计")
        both = plan_question("南网能源 MySQL 里有多少设备、多少表计")
        self.assertEqual(device.key, "device_count")
        self.assertEqual(meter.key, "meter_count")
        self.assertEqual(both.key, "asset_counts")
        self.assertNotEqual(device.key, meter.key)

    def test_control_alarm_quality(self) -> None:
        self.assertEqual(
            plan_question("南网能源昨天控制成功率").key, "control_success"
        )
        self.assertEqual(plan_question("南网能源今天告警").key, "alarm_stats")
        self.assertEqual(plan_question("南网能源小时表缺数").key, "data_quality")

    def test_total_and_trend(self) -> None:
        self.assertEqual(plan_question("南网能源今天总用电").key, "energy_total")
        self.assertEqual(
            plan_question("南网能源近7天用电趋势").key, "energy_trend"
        )

    def test_unknown_question_has_no_metric(self) -> None:
        plan = plan_question("你好")
        self.assertIsNone(plan.metric)
        self.assertTrue(plan.needs_clarification)

    def test_ambiguous_cold_station_returns_candidates(self) -> None:
        plan = plan_question("南网能源冷站怎么样")
        self.assertIsNone(plan.metric)
        self.assertIn("metric", plan.missing)
        self.assertIn("cold_station_electricity", plan.candidates)


class MetricCatalogTest(unittest.TestCase):
    def test_catalog_and_context(self) -> None:
        catalog = metric_catalog()
        keys = {item["key"] for item in catalog}
        self.assertIn("cold_station_electricity", keys)
        self.assertIn("energy_anomaly", keys)
        self.assertIn("device_count", keys)
        text = format_metric_context(METRICS["cold_station_electricity"])
        self.assertIn("冷站用电", text)
        self.assertIn("eo_energy_meters", text)
        self.assertIn("冷量计", text)


if __name__ == "__main__":
    unittest.main()
