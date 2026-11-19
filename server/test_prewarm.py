"""启动预热单测（不连库）。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import prewarm
from schema_cache import SchemaSnapshot


class PrewarmTest(unittest.IsolatedAsyncioTestCase):
    async def test_prewarm_current_tenant(self) -> None:
        provider = SimpleNamespace(
            config=SimpleNamespace(
                doris_db="nwdy_dm",
                doris_tenant_id=1026,
                doris_project_id=1,
            )
        )
        snapshot = SchemaSnapshot(
            database="nwdy_dm",
            built_at=1.0,
            tables={"dm_eo_e_meter_di": [{"name": "id"}]},
        )
        with patch.object(prewarm, "get_tenant_provider", return_value=provider), patch.object(
            prewarm, "get_mysql_provider", return_value=None
        ), patch.object(
            prewarm.SCHEMA_CACHE, "get", new=AsyncMock(return_value=snapshot)
        ):
            await prewarm.prewarm_schemas()
        self.assertEqual(prewarm.STATUS.state, "done")
        self.assertIn("nwdy:doris(nwdy_dm)", prewarm.STATUS.tables)
        payload = prewarm.STATUS.as_dict()
        self.assertIn("duration_seconds", payload)

    async def test_prewarm_skips_non_archery(self) -> None:
        with patch.object(
            prewarm, "settings", SimpleNamespace(data_provider="mock")
        ):
            await prewarm.prewarm_schemas()
        self.assertEqual(prewarm.STATUS.state, "skipped")


if __name__ == "__main__":
    unittest.main()
