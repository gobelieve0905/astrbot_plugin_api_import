"""Regression for observed sort/filter crashes and loss of page coverage."""

import json
import unittest
from urllib.parse import parse_qs

import test_meta as base
from test_meta import Meta, one


class MetaArrayPaging(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.MetaWireTests.asyncSetUp
    asyncTearDown = base.MetaWireTests.asyncTearDown
    call = base.MetaWireTests.call

    async def test_sort_and_nested_filter_arrays_preserve_wire_values(self):
        for params in [
            {"sort": ["purchase_descending"]},
            {"filtering": [{"field": "action_type", "operator": "IN", "value": ["purchase"]}]},
            {"breakdowns": ["country"], "action_attribution_windows": ["7d_click"]},
        ]:
            result = await self.call("get_insights", {"node_id": "act_123", "params": params})
            self.assertTrue(result["ok"], result)
            query = parse_qs(self.sent[-1].url.query.decode())
            for key, value in params.items():
                self.assertEqual(json.loads(query[key][0]), value)

    async def test_nested_lists_do_not_bypass_reserved_or_deleted_checks(self):
        definition = one("post_ads")
        self.allowed = {("abc123ab", "post_ads")}
        for params in [
            {"creative": {"payload": [{"access_token": "override"}]}},
            {"creative": {"status": ["DELETED"]}},
            {"creative": {"payload": [{"status": " DELETED "}]}},
        ]:
            with self.assertRaises(ValueError):
                Meta.prepare(
                    definition, {"node_id": "123", "params": params}, self.executor.permitted
                )
        self.assertFalse(self.sent)

    async def test_paging_survives_preview_without_skipping_unread_rows(self):
        self.response = {
            "data": [{"ad_name": "x" * 500} for _ in range(50)],
            "paging": {
                "cursors": {"after": "safe_cursor"},
                "next": "https://graph.facebook.com/?access_token=synthetic-meta-token",
            },
        }
        result = await self.call("get_insights", {"node_id": "act_123"})
        self.assertTrue(result["truncated"])
        self.assertEqual(result["page"]["row_count"], 50)
        self.assertTrue(result["page"]["has_more"])
        self.assertEqual(result["page"]["after"], "safe_cursor")
        self.assertFalse(result["page"]["response_complete"])
        self.assertLess(result["page"]["retry_current_page_limit"], 50)
        self.assertNotIn("synthetic-meta-token", json.dumps(result))
        self.assertNotIn("https://graph.facebook.com/?", json.dumps(result))
        self.response = {"data": [], "paging": {"cursors": {"after": "last_cursor"}}}
        result = await self.call("get_insights", {"node_id": "act_123"})
        self.assertFalse(result["page"]["has_more"])
        self.assertIsNone(result["page"]["after"])

    async def test_account_discovery_returns_full_page_under_bounded_budget(self):
        self.response = {
            "data": [{"id": f"act_{i}", "name": "无项目关键字的账户" * 4} for i in range(111)]
        }
        result = await self.call("get_adaccounts", {"node_id": "me"})
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["data"]["data"]), 111)
        self.assertIn("账户名称不能证明项目归属", Meta.tool_guidance("get_adaccounts"))
        self.assertEqual(
            len(self.sent), 1
        )  # Never auto query another account or grant a permission.
