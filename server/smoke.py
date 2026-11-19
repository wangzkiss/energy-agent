# -*- coding: utf-8 -*-
"""对话冒烟测试：直接打 BFF 的 /anthropic/v1/messages（非流式）。

用法：
    python smoke.py "问题1" "问题2"
    python smoke.py            # 跑内置题库
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8088"
TOKEN = "energy-demo-token"

DEFAULT_QUESTIONS = [
    "南网能源昨天用电最多的楼层是哪个？",
    "南网能源有多少台空调面板？",
    "南网能源最近一周哪些设备一直在运行？",
    "南网能源告警都是什么类型？",
    "南网能源 A1座 和 36 层昨天用电对比",
    "南网能源昨天总用电多少？",
    "南网能源 36 层现在室温多少？",
    "南网能源的电价是多少？",
    "南网能源空压机最近的压力数据怎么样？",
    "南网能源昨天总用电查哪张表？",
    "南网能源 MySQL 里有多少设备？",
    "南网能源 35 层和 36 层室温对比",
    "南网能源最近有哪些工单没有闭环？",
]


def ask(question: str) -> str:
    payload = {
        "model": "deepseek-flash",
        "stream": False,
        "thinking": {"type": "enabled"},
        "messages": [{"role": "user", "content": question}],
    }
    req = urllib.request.Request(
        BASE + "/anthropic/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "x-api-key": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content") or []
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def main(argv: list[str]) -> None:
    questions = argv or DEFAULT_QUESTIONS
    for index, question in enumerate(questions, 1):
        started = time.perf_counter()
        try:
            answer = ask(question)
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== [{index}] {question}\n[请求失败] {exc}")
            continue
        elapsed = time.perf_counter() - started
        head = answer.strip()
        print(f"\n=== [{index}] {question}  ({elapsed:.1f}s, {len(answer)} chars)")
        print(head)


if __name__ == "__main__":
    main(sys.argv[1:])
