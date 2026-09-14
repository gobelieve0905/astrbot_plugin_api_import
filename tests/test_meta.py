"""Meta catalog coverage and dispatch authorization; never call a real Meta account."""

import asyncio
import base64
import copy
import importlib
import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from test_engine import Definitions
from test_platforms import Platforms

Meta = importlib.import_module("api_import_test.meta_platform")
Engine = importlib.import_module("api_import_test.engine")
Connections = importlib.import_module("api_import_test.connections")
Catalog = importlib.import_module("api_import_test.catalog").Catalog


def make_items(prefix="Meta_Main", enabled=()):
    return Platforms.build_connection(
        {
            "platform_id": Meta.PLATFORM,
            "name": "仅后台名称",
            "call_name": prefix,
            "token": "synthetic-meta-token",
            "enabled_operations": list(enabled),
        },
        [],
    )


def one(operation, enabled=True):
    item = Meta.definition(
        Meta.operations()[operation], "synthetic-meta-token", "abc123ab", enabled
    )
    item["connection"] = {
        "id": "abc123ab",
        "name": "仅后台名称",
        "call_name": "Meta_Main",
        "platform": Meta.PLATFORM,
        "operation": operation,
    }
    return Definitions.parse_definitions(json.dumps([item]))[0]


class MetaCatalogTests(unittest.TestCase):
    def test_catalog_complete_and_unique_routes(self):
        ops = Meta.operations()
        self.assertEqual(len(ops), 714)
        self.assertEqual(len({(o["method"], o["path"]) for o in ops.values()}), 714)
        self.assertEqual(
            Counter(o["method"] for o in ops.values()),
            {"GET": 412, "POST": 252, "PUT": 1, "DELETE": 49},
        )
        self.assertEqual(sum(len(o["sources"]) for o in ops.values()), 1490)
        self.assertEqual(Meta.catalog()["api_version"], "v26.0")
        self.assertTrue(all(len(key) <= 23 for key in ops))
        # SDK aliases sharing POST /{node_id} cannot be authorized separately.
        sources = {s["object"] for s in ops["post_node"]["sources"]}
        self.assertTrue({"Ad", "AdSet", "Campaign"} <= sources)
        self.assertEqual(len(Platforms.platform_catalog()["platforms"]), 2)

    def test_full_connection_default_off_and_two_accounts(self):
        first = make_items()
        second = make_items("Meta_Second", ["get_adaccounts", "post_campaigns"])
        parsed = Definitions.parse_definitions(json.dumps(first + second))
        self.assertEqual(len(parsed), 1428)
        self.assertEqual(sum(p.enabled for p in parsed), 2)
        self.assertEqual(len({p.tool_name for p in parsed}), 1428)
        for p in parsed:
            self.assertNotIn("仅后台名称", p.description)
            self.assertNotIn("synthetic-meta-token", p.description + json.dumps(p.parameters))
            self.assertEqual(p.tool_name, p.display_name)
        Definitions.parse_definitions(json.dumps(make_items("A" * 40)))

    def test_rotation_and_permission_change_are_account_scoped(self):
        first, second = make_items(enabled=["get_ads"]), make_items("Second")
        changed = Connections.update_connection(
            first + second,
            {
                "connection_id": first[0]["connection"]["id"],
                "name": "新名称",
                "call_name": "Renamed",
                "token": "replacement",
                "enabled_names": [first[0]["name"]],
            },
        )
        self.assertEqual(sum(p["enabled"] for p in changed), 1)
        self.assertTrue(
            all(
                p["request"]["headers"]["Authorization"] == "Bearer replacement"
                for p in changed[:714]
            )
        )
        self.assertEqual(changed[714:], second)
        self.assertTrue(
            all(
                p.tool_name.startswith("Renamed_")
                for p in Definitions.parse_definitions(json.dumps(changed[:714]))
            )
        )
        with self.assertRaises(Definitions.DefinitionError):
            Connections.update_connection(
                first + second,
                {
                    "connection_id": first[0]["connection"]["id"],
                    "name": "新名称",
                    "enabled_names": [second[0]["name"]],
                },
            )

    def test_preset_routing_and_schema_cannot_be_replaced(self):
        original = make_items()[0]
        for field, value in [
            ("url", "https://example.test/{node_id}"),
            ("method", "POST"),
            ("query", {"method": "POST"}),
            ("headers", {"Authorization": "Bearer synthetic", "X-HTTP-Method-Override": "POST"}),
        ]:
            item = copy.deepcopy(original)
            item["request"][field] = value
            with self.subTest(field=field), self.assertRaises(Definitions.DefinitionError):
                Definitions.parse_definitions(json.dumps([item]))
        item = copy.deepcopy(original)
        item["parameters"]["additionalProperties"] = True
        with self.assertRaises(Definitions.DefinitionError):
            Definitions.parse_definitions(json.dumps([item]))

    def test_unknown_permissions_reject_atomically(self):
        config = {"tools_json": "[]"}
        writes = []
        catalog = Catalog(config, lambda *args: writes.append(args))
        with self.assertRaises(Definitions.DefinitionError):
            catalog.mutate(
                "connect-platform",
                {
                    "revision": catalog.snapshot()["revision"],
                    "platform_id": Meta.PLATFORM,
                    "token": "synthetic",
                    "enabled_operations": ["arbitrary_http_request"],
                },
            )
        self.assertFalse(writes)


class MetaWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sent = []
        self.allowed = set()
        self.response = {"data": []}
        self.status = 200

        async def handle(request):
            self.sent.append(request)
            return httpx.Response(self.status, json=self.response)

        self.executor = Engine.Executor(
            Path(self.temp.name), httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        self.executor.permitted = lambda connection_id, operation: (
            (connection_id, operation) in self.allowed
        )

    async def asyncTearDown(self):
        await self.executor.close()
        self.temp.cleanup()

    async def call(self, operation, arguments, **options):
        definition = one(operation)
        self.allowed.add((definition.connection["id"], operation))
        return await self.executor.execute(definition, arguments, **options)

    async def test_list_insights_and_form_encoding(self):
        result = await self.call(
            "get_insights",
            {
                "node_id": "act_123",
                "fields": ["spend", "impressions"],
                "params": {
                    "level": "campaign",
                    "time_range": {"since": "2026-09-01", "until": "2026-09-02"},
                    "after": "cursor",
                },
            },
        )
        self.assertTrue(result["ok"], result)
        req = self.sent[-1]
        self.assertEqual(
            str(req.url).split("?")[0], "https://graph.facebook.com/v26.0/act_123/insights"
        )
        self.assertEqual(req.headers["Authorization"], "Bearer synthetic-meta-token")
        self.assertEqual(json.loads(req.url.params["time_range"])["since"], "2026-09-01")
        self.assertEqual(req.url.params["fields"], "spend,impressions")
        self.assertNotIn("access_token", req.url.params)
        result = await self.call(
            "post_campaigns",
            {
                "node_id": "act_123",
                "params": {
                    "name": "Campaign",
                    "objective": "OUTCOME_SALES",
                    "status": "PAUSED",
                    "special_ad_categories": [],
                },
            },
        )
        self.assertTrue(result["ok"], result)
        req = self.sent[-1]
        self.assertEqual(req.method, "POST")
        self.assertEqual(parse_qs(req.content.decode())["special_ad_categories"], ["[]"])

    async def test_disabled_even_with_old_enabled_definition(self):
        definition = one("get_ads")
        result = await self.executor.execute(definition, {"node_id": "act_123"})
        self.assertFalse(result["ok"])
        self.assertFalse(self.sent)
        self.allowed.add(("other_account", "get_ads"))
        result = await self.executor.execute(definition, {"node_id": "act_123"})
        self.assertFalse(result["ok"])
        self.assertFalse(self.sent)
        self.allowed.add((definition.connection["id"], "get_ads"))
        result = await self.executor.execute(
            replace(definition, enabled=False), {"node_id": "act_123"}
        )
        self.assertFalse(result["ok"])
        self.assertFalse(self.sent)

    async def test_queued_call_checks_revocation_before_dispatch(self):
        self.executor.semaphore = asyncio.Semaphore(0)
        task = asyncio.create_task(self.call("get_ads", {"node_id": "act_123"}))
        await asyncio.sleep(0)
        self.allowed.clear()
        self.executor.semaphore.release()
        self.assertFalse((await task)["ok"])
        self.assertFalse(self.sent)
        self.executor.semaphore = asyncio.Semaphore(0)
        valid = True
        task = asyncio.create_task(
            self.call("get_ads", {"node_id": "act_123"}, guard=lambda: valid)
        )
        await asyncio.sleep(0)
        valid = False
        self.executor.semaphore.release()
        self.assertFalse((await task)["ok"])
        self.assertFalse(self.sent)

    async def test_protocol_and_path_bypasses_never_reach_network(self):
        attempts = [
            {"node_id": "../me"},
            {"node_id": "me?method=POST"},
            {"node_id": "act_123/ads"},
            {"node_id": "act_123%2Fads"},
            {"node_id": "https://graph.facebook.com"},
            {"node_id": "act_123", "params": {"access_token": "replacement"}},
            {"node_id": "act_123", "params": {"method": "POST"}},
            {"node_id": "act_123", "params": {"batch": []}},
            {"node_id": "act_123", "params": {"ids": "123,456"}},
            {"node_id": "act_123", "params": {"fields": "ads{id}"}},
            {"node_id": "act_123", "fields": ["ads{id}"]},
            {"node_id": "act_123", "fields": ["alias:ads"]},
            {"node_id": "act_123", "fields": ["ads.limit(10)"]},
            {"node_id": "act_123", "fields": ["not_a_real_official_field"]},
        ]
        for args in attempts:
            with self.subTest(args=args):
                self.assertFalse((await self.call("get_node", args))["ok"])
        self.assertFalse(self.sent)

    async def test_field_relation_and_delete_alias_require_permission(self):
        self.assertFalse(
            (await self.call("get_node", {"node_id": "act_123", "fields": ["campaigns"]}))["ok"]
        )
        self.assertFalse(
            (await self.call("post_node", {"node_id": "123", "params": {"status": "DELETED"}}))[
                "ok"
            ]
        )
        self.assertFalse(self.sent)
        self.allowed.add(("abc123ab", "delete_node"))
        self.assertTrue(
            (await self.call("post_node", {"node_id": "123", "params": {"status": "DELETED"}}))[
                "ok"
            ]
        )

    async def test_nested_override_blocked(self):
        self.assertFalse(
            (
                await self.call(
                    "post_campaigns",
                    {
                        "node_id": "act_123",
                        "params": {
                            "execution_options": [],
                            "promoted_object": {"method": "DELETE"},
                        },
                    },
                )
            )["ok"]
        )
        self.assertFalse(self.sent)

    async def test_file_upload_is_bounded_bytes_not_local_path(self):
        result = await self.call(
            "post_adplayables",
            {
                "node_id": "act_123",
                "params": {
                    "source": {
                        "base64": base64.b64encode(b"synthetic-file").decode(),
                        "filename": "demo.html",
                    }
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertIn(b"synthetic-file", self.sent[-1].content)
        self.assertIn("multipart/form-data", self.sent[-1].headers["content-type"])
        self.sent.clear()
        for value in [
            "/etc/passwd",
            {"base64": "bad!!!"},
            {"base64": "YQ==", "filename": "../../passwd"},
        ]:
            self.assertFalse(
                (
                    await self.call(
                        "post_adplayables", {"node_id": "act_123", "params": {"source": value}}
                    )
                )["ok"]
            )
        self.assertFalse(self.sent)

    async def test_error_and_paging_credentials_not_in_model_or_files(self):
        self.response = {
            "data": [
                {"id": "1", "access_token": "new-derived-token", "note": "synthetic-meta-token"}
            ],
            "paging": {
                "next": "https://graph.facebook.com/?access_token=synthetic-meta-token",
                "cursors": {"after": "cursor"},
            },
        }
        result = await self.call("get_ads", {"node_id": "act_123"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["paging"]["cursors"]["after"], "cursor")
        combined = json.dumps(result) + Path(result["file"]).read_text()
        for secret in ["synthetic-meta-token", "new-derived-token", "access_token=", '"next"']:
            self.assertNotIn(secret, combined)
        self.response = {"error": {"message": "bad synthetic-meta-token", "code": 190}}
        result = await self.call("get_ads", {"node_id": "act_123"})
        self.assertFalse(result["ok"])
        self.assertNotIn("file", result)
        self.assertNotIn("synthetic-meta-token", json.dumps(result))
        self.status = 400
        before = len(self.sent)
        result = await self.call("post_node", {"node_id": "123", "params": {"status": "PAUSED"}})
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.sent), before + 1)
        self.assertNotIn("synthetic-meta-token", json.dumps(result))

    async def test_sdk_dynamic_subscription_path(self):
        operation = next(o["id"] for o in Meta.operations().values() if "{Dynamic}" in o["path"])
        result = await self.call(operation, {"node_id": "123", "Dynamic": "page"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(str(self.sent[-1].url).endswith("/123/subscriptions/page"))
