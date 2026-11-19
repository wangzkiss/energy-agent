"""进程内共享缓存实例。

- SCHEMA_CACHE：库表结构快照（落盘，跨请求/跨进程重启可复用）
- SQL_CACHE：只读 SQL 结果短期缓存（进程内）
"""
from __future__ import annotations

from config import settings
from schema_cache import SchemaCache
from sql_cache import SqlResultCache

SCHEMA_CACHE = SchemaCache(
    ttl_seconds=max(60, settings.schema_cache_ttl_seconds),
)
SQL_CACHE = SqlResultCache(
    ttl_seconds=max(1, settings.sql_cache_ttl_seconds),
    max_entries=max(1, settings.sql_cache_size),
    enabled=settings.sql_cache_enabled,
)
