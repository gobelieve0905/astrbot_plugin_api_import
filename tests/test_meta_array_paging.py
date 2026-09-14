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
        self.assertIn("/me/adaccounts", Meta.tool_guidance("get_adaccounts"))
        self.assertNotIn("项目归属", Meta.tool_guidance("get_adaccounts"))
        self.assertEqual(
            len(self.sent), 1
        )  # Never auto query another account or grant a permission.

    async def test_duplicate_fields_are_deduplicated_without_relaxing_validation(self):
        result = await self.call(
            "get_insights", {"node_id": "act_123", "fields": ["spend", "spend", "ad_id"]}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(parse_qs(self.sent[-1].url.query.decode())["fields"], ["spend,ad_id"])
        sent = len(self.sent)
        for fields in [
            ["spend"] * 101,
            ["unknown_invalid_field", "unknown_invalid_field"],
            ["spend", {"token": "x"}],
        ]:
            self.assertFalse(
                (await self.call("get_insights", {"node_id": "act_123", "fields": fields}))["ok"]
            )
        self.assertEqual(len(self.sent), sent)

    async def test_local_windows_read_all_rows_without_new_network_and_enforce_scope(self):
        definition = one("get_insights")
        self.allowed.add(("abc123ab", "get_insights"))
        self.response = {
            "data": [
                {"ad_id": str(i), "actions": [{"action_type": "purchase", "value": "1"}]}
                for i in range(120)
            ]
        }
        first = await self.executor.execute(definition, {"node_id": "act_123"})
        rid = first["result_id"]
        ids, offset = [], 0
        while offset is not None:
            page = await self.executor.execute(
                definition,
                {"node_id": "act_123", "result_page": {"id": rid, "offset": offset, "limit": 17}},
            )
            self.assertTrue(page["ok"], page)
            ids.extend(row["ad_id"] for row in page["data"])
            offset = page["next_offset"]
        self.assertEqual(ids, [str(i) for i in range(120)])
        self.assertEqual(len(self.sent), 1)
        for node, result_id in [("act_other", rid), ("act_123", "../secret")]:
            denied = await self.executor.execute(
                definition, {"node_id": node, "result_page": {"id": result_id}}
            )
            self.assertFalse(denied["ok"])
        self.assertFalse(
            (
                await self.executor.execute(
                    one("get_insights"), {"node_id": "act_123", "result_page": {"id": rid}}
                )
            )["ok"]
        )
        self.allowed.clear()
        self.assertFalse(
            (
                await self.executor.execute(
                    definition, {"node_id": "act_123", "result_page": {"id": rid}}
                )
            )["ok"]
        )
        self.assertEqual(len(self.sent), 1)

    async def test_local_windows_recheck_related_permissions_and_file_integrity(self):
        definition = one("get_adaccounts")
        self.allowed.update({("abc123ab", "get_adaccounts"), ("abc123ab", "get_ads")})
        self.response = {"data": [{"id": "act_123", "ads": []}]}
        result = await self.executor.execute(definition, {"node_id": "me", "fields": ["ads"]})
        rid = result["result_id"]
        args = {"node_id": "me", "result_page": {"id": rid}}
        self.allowed.remove(("abc123ab", "get_ads"))
        self.assertFalse((await self.executor.execute(definition, args))["ok"])
        self.allowed.add(("abc123ab", "get_ads"))
        self.executor.saved_results[rid][2].write_text('{"data": [{"id": "tampered"}]}')
        self.assertFalse((await self.executor.execute(definition, args))["ok"])
        self.assertEqual(len(self.sent), 1)
