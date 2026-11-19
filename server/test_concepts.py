"""业务口径知识层的路由与答案测试（不连库、不调模型）。"""
from __future__ import annotations

import unittest

from concepts import concept_answer, is_concept_question, match_concept


class ConceptRoutingTest(unittest.TestCase):
    def title(self, question: str) -> str:
        item = match_concept(question)
        return item["title"] if item else ""

    def test_cold_station_cop(self) -> None:
        self.assertEqual(self.title("冷站的制冷量和 COP 是怎么计算的？"), "冷站制冷量与 COP")

    def test_cold_station_power_preferred_over_cop(self) -> None:
        self.assertEqual(self.title("冷站用电怎么算？"), "冷站用电口径")

    def test_sub_metering(self) -> None:
        self.assertEqual(
            self.title("分项计量有哪些分项？照明、空调、动力怎么区分？"),
            "分项计量（照明/空调/动力）",
        )

    def test_demand_even_with_value_wording(self) -> None:
        answer = concept_answer("最大需量是多少？需量怎么算？")
        self.assertIsNotNone(answer)
        self.assertIn("15 分钟", answer)
        self.assertIn("无法计算", answer)

    def test_alarm_flow_with_particles(self) -> None:
        answer = concept_answer("告警的等级和闭环流程是什么？")
        self.assertIsNotNone(answer)
        self.assertIn("0 紧急", answer)
        self.assertIn("未处理", answer)

    def test_ticket_close_rate(self) -> None:
        answer = concept_answer("工单闭环率是多少？处理时长怎么算？")
        self.assertIsNotNone(answer)
        self.assertIn("闭环率", answer)
        self.assertIn("0 行", answer)

    def test_definition_answer_has_structure(self) -> None:
        answer = concept_answer("单位面积能耗怎么算？有没有建筑面积数据？")
        assert answer is not None
        for heading in ("### 结论", "### 计算口径", "### 本项目数据情况", "### 边界与注意事项"):
            self.assertIn(heading, answer)
        self.assertIn("占位值", answer)

    def test_data_questions_do_not_use_concept(self) -> None:
        for question in (
            "南网能源昨天总用电多少？",
            "昨天有没有异常用电？",
            "昨天 35 层和 36 层室温对比",
            "南网能源昨天用电最多的楼层是哪个？",
        ):
            self.assertFalse(is_concept_question(question), question)
            self.assertIsNone(concept_answer(question), question)

    def test_knowledge_payload_contains_concepts(self) -> None:
        from knowledge import knowledge_payload

        payload = knowledge_payload()
        self.assertIn("concepts", payload)
        self.assertIn("cold_station_cop", payload["concepts"])


if __name__ == "__main__":
    unittest.main()
