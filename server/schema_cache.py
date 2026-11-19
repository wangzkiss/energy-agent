"""租户库表结构快照缓存。

目的：把 information_schema 的“表清单 + 字段名/类型/注释”缓存到本地文件，
避免每次调查都重复查询元数据；search_columns / find_tables / describe_table
优先读快照，过期或缺失时再查实时库。

设计约束：
- 只缓存元数据，不缓存业务数据；
- 快照按 source.key + database 分文件，TTL 默认 6 小时；
- 构建失败不抛异常，返回 None，调用方回退实时查询；
- 快照写盘使用临时文件 + 原子替换，避免半截文件。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("energy-schema-cache")

CACHE_VERSION = 1
DEFAULT_TTL_SECONDS = 6 * 3600
TABLE_PAGE = 500
COLUMN_BATCH = 40

SNAPSHOT_DIR = Path(__file__).resolve().parent / ".cache" / "schema"


@dataclass
class SchemaSnapshot:
    database: str
    built_at: float
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def table_names(self) -> list[str]:
        return sorted(self.tables)

    def columns(self, table: str) -> list[dict[str, str]]:
        return list(self.tables.get(table) or [])

    def time_column(self, table: str) -> str:
        for column in self.tables.get(table) or []:
            data_type = str(column.get("type") or "").lower()
            if data_type.startswith(("datetime", "timestamp", "date")):
                return str(column.get("name") or "")
        return ""

    def search(self, tokens: list[str], limit: int = 100) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for table_name, columns in self.tables.items():
            for column in columns:
                name = str(column.get("name") or "").lower()
                comment = str(column.get("comment") or "").lower()
                if any(
                    token.lower() in name or token.lower() in comment
                    for token in tokens
                ):
                    matches.append(
                        {
                            "TABLE_NAME": table_name,
                            "COLUMN_NAME": name,
                            "COLUMN_TYPE": str(column.get("type") or ""),
                            "COLUMN_COMMENT": comment,
                        }
                    )
                    if len(matches) >= limit:
                        return matches
        return matches


class SchemaCache:
    def __init__(
        self,
        directory: Path | str = SNAPSHOT_DIR,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self._memory: dict[str, SchemaSnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    async def get(self, source: Any, *, refresh: bool = False) -> SchemaSnapshot | None:
        key = f"{source.key}:{source.database}"
        now = time.time()
        if not refresh:
            cached = self._memory.get(key)
            if cached and now - cached.built_at < self.ttl_seconds:
                return cached
            snapshot = self._read_file(source)
            if snapshot and now - snapshot.built_at < self.ttl_seconds:
                self._memory[key] = snapshot
                return snapshot
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._memory.get(key)
            if not refresh and cached and time.time() - cached.built_at < self.ttl_seconds:
                return cached
            snapshot = await self._build(source)
            if snapshot is not None:
                self._memory[key] = snapshot
            return snapshot

    def peek(self, source: Any) -> SchemaSnapshot | None:
        """只看内存/磁盘快照，不触发构建；用于“有就用、没有就实时查”。"""
        key = f"{source.key}:{source.database}"
        now = time.time()
        cached = self._memory.get(key)
        if cached and now - cached.built_at < self.ttl_seconds:
            return cached
        snapshot = self._read_file(source)
        if snapshot and now - snapshot.built_at < self.ttl_seconds:
            self._memory[key] = snapshot
            return snapshot
        return None

    def _read_file(self, source: Any) -> SchemaSnapshot | None:
        path = self._path(source)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("version") or 0) != CACHE_VERSION:
                return None
            tables = payload.get("tables") or {}
            if not isinstance(tables, dict) or not tables:
                return None
            return SchemaSnapshot(
                database=str(payload.get("database") or source.database),
                built_at=float(payload.get("built_at") or 0),
                tables={
                    str(name): [dict(column) for column in columns]
                    for name, columns in tables.items()
                },
            )
        except Exception as exc:  # noqa: BLE001 - 坏文件当作没有缓存
            logger.warning("read schema cache failed: %s", exc)
            return None

    def _write_file(self, source: Any, snapshot: SchemaSnapshot) -> None:
        path = self._path(source)
        payload = {
            "version": CACHE_VERSION,
            "database": snapshot.database,
            "built_at": snapshot.built_at,
            "tables": snapshot.tables,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temporary, path)
        except Exception as exc:  # noqa: BLE001 - 写盘失败只影响下次冷启动
            logger.warning("write schema cache failed: %s", exc)

    def _path(self, source: Any) -> Path:
        return self.directory / f"{source.key}-{source.database}.json"

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    async def _build(self, source: Any) -> SchemaSnapshot | None:
        database = str(source.database)
        try:
            names = await self._table_names(source, database)
        except Exception as exc:  # noqa: BLE001
            logger.warning("schema cache table list failed: %s", exc)
            return None
        if not names:
            return None
        tables: dict[str, list[dict[str, str]]] = {}
        for start in range(0, len(names), COLUMN_BATCH):
            chunk = names[start : start + COLUMN_BATCH]
            rows = await self._column_rows(source, database, chunk)
            for row in rows:
                table_name = str(row.get("TABLE_NAME") or "")
                if not table_name:
                    continue
                tables.setdefault(table_name, []).append(
                    {
                        "name": str(row.get("COLUMN_NAME") or ""),
                        "type": str(row.get("COLUMN_TYPE") or ""),
                        "comment": str(row.get("COLUMN_COMMENT") or ""),
                    }
                )
        if not tables:
            return None
        snapshot = SchemaSnapshot(
            database=database, built_at=time.time(), tables=tables
        )
        self._write_file(source, snapshot)
        logger.info(
            "schema snapshot built: %s %s tables", database, len(snapshot.tables)
        )
        return snapshot

    async def _column_rows(
        self, source: Any, database: str, tables: list[str]
    ) -> list[dict[str, Any]]:
        """一批表的字段；若刚好撞到 500 行上限，拆半重查避免截断。"""
        quoted = ",".join("'" + name.replace("'", "''") + "'" for name in tables)
        sql = (
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
            f"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME IN ({quoted}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT 500"
        )
        try:
            rows = await source.provider.ad_hoc_query(sql, limit=500)
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响其它表
            logger.warning("schema cache columns failed: %s", exc)
            return []
        if len(rows) >= 500 and len(tables) > 1:
            middle = len(tables) // 2
            left = await self._column_rows(source, database, tables[:middle])
            right = await self._column_rows(source, database, tables[middle:])
            return left + right
        return rows

    async def _table_names(self, source: Any, database: str) -> list[str]:
        names: list[str] = []
        offset = 0
        while True:
            sql = (
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA = '{database}' ORDER BY TABLE_NAME "
                f"LIMIT {TABLE_PAGE} OFFSET {offset}"
            )
            rows = await source.provider.ad_hoc_query(sql, limit=TABLE_PAGE)
            if not rows:
                break
            names.extend(
                str(row.get("TABLE_NAME") or "")
                for row in rows
                if row.get("TABLE_NAME")
            )
            if len(rows) < TABLE_PAGE:
                break
            offset += TABLE_PAGE
            if offset > 5000:
                break
        return names
