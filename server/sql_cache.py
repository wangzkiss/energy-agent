"""只读 SQL 结果的进程内短期缓存（LRU + TTL）。

目的：同一轮调查/相邻问题里经常重复执行同一条 SQL（例如先查总量再按维度拆），
在 TTL 窗口内直接复用结果，减少 Archery 往返。
只缓存成功、行数不超过上限的查询；不缓存元数据工具（它们有独立的 schema 快照）。
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    rows: list[dict[str, Any]]
    created_at: float


class SqlResultCache:
    def __init__(
        self,
        ttl_seconds: int = 120,
        max_entries: int = 200,
        max_rows: int = 500,
        enabled: bool = True,
    ) -> None:
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_rows = max(1, int(max_rows))
        self.enabled = bool(enabled) and self.ttl_seconds > 0
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @staticmethod
    def key(
        source_key: str,
        database: str,
        sql: str,
        limit: int,
    ) -> str:
        normalized = " ".join(sql.split())
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return f"{source_key}:{database}:{limit}:{digest}"

    def get(self, key: str) -> tuple[list[dict[str, Any]], float] | None:
        if not self.enabled:
            return None
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        age = time.time() - entry.created_at
        if age >= self.ttl_seconds:
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.rows, age

    def put(self, key: str, rows: list[dict[str, Any]]) -> None:
        if not self.enabled or len(rows) > self.max_rows:
            return
        self._entries[key] = _Entry(rows=rows, created_at=time.time())
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }
