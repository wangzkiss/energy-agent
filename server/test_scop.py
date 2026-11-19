"""冷站 SCOP 名称归集的单元测试（不连库）。"""
from __future__ import annotations

import unittest

from toolbox import AgentToolbox

STATIONS = [
    {"cooling_name": "工艺空调系统", "application_id": 2065},
    {"cooling_name": "空调冷源系统", "application_id": 2531},
    {"cooling_name": "综合冷机", "application_id": 2710},
]

COLD = [
    {"meter_name": "冷源-工艺能量计", "kwh": 18612},
    {"meter_name": "冷源-空调能量计", "kwh": 226963},
]

ELEC = [
    {"meter_name": "工艺冷水机1", "kwh": 10},
    {"meter_name": "工艺冷冻水泵1#", "kwh": 5},
    {"meter_name": "空调冷水机1", "kwh": 20000},
    {"meter_name": "空调冷却水泵1#", "kwh": 5000},
    {"meter_name": "板式换热机组1#", "kwh": 1000},
]


class ScopRowsTest(unittest.TestCase):
    def test_name_mapping_and_status(self) -> None:
        rows, unassigned = AgentToolbox._scop_rows(STATIONS, COLD, ELEC)
        by_name = {row["冷站"]: row for row in rows}
        self.assertEqual(by_name["工艺空调系统"]["冷量kWh"], 18612)
        self.assertEqual(by_name["工艺空调系统"]["用电kWh"], 15)
        self.assertAlmostEqual(by_name["工艺空调系统"]["SCOP"], 1240.8)
        self.assertIn("电量存疑", by_name["工艺空调系统"]["状态"])
        self.assertEqual(by_name["空调冷源系统"]["冷量kWh"], 226963)
        self.assertEqual(by_name["空调冷源系统"]["用电kWh"], 25000)
        self.assertEqual(by_name["空调冷源系统"]["SCOP"], 9.08)
        self.assertEqual(by_name["综合冷机"]["冷量kWh"], 245575)
        self.assertEqual(by_name["综合冷机"]["用电kWh"], 26015)
        self.assertEqual(len(unassigned), 1)
        self.assertEqual(unassigned[0]["meter_name"], "板式换热机组1#")

    def test_no_cold_data_marks_unavailable(self) -> None:
        rows, _ = AgentToolbox._scop_rows(STATIONS, [], ELEC)
        by_name = {row["冷站"]: row for row in rows}
        self.assertEqual(by_name["空调冷源系统"]["状态"], "无冷量数据")
        self.assertIsNone(by_name["空调冷源系统"]["SCOP"])


if __name__ == "__main__":
    unittest.main()
