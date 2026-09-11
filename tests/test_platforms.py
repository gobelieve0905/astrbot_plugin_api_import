"""Quick connection contracts, atomic validation and mocked wire requests."""

import importlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from test_engine import Definitions

Platforms = importlib.import_module("api_import_test.platforms")
CatalogModule = importlib.import_module("api_import_test.catalog")
Executor = importlib.import_module("api_import_test.engine").Executor


def payload(**changes):
    return {
        "platform_id": "applovin_report",
        "token": "synthetic-only",
        "enabled_operations": ["advertiser"],
        **changes,
    }


class PlatformTests(unittest.TestCase):
    def test_contract_and_accounts(self):
        first = Platforms.build_connection(payload(), [])
        parsed = Definitions.parse_definitions(json.dumps(first))
        self.assertEqual(len(parsed), 6)
        self.assertEqual(sum(item.enabled for item in parsed), 1)
        self.assertEqual(first[0]["request"]["query"]["report_type"], "advertiser")
        self.assertEqual(first[1]["request"]["query"]["report_type"], "publisher")
        self.assertEqual(
            [item["request"]["url"] for item in first][2:],
            [
                "https://r.applovin.com/maxReport",
                "https://r.applovin.com/maxCohort",
                "https://r.applovin.com/maxCohort/imp",
                "https://r.applovin.com/maxCohort/session",
            ],
        )
        self.assertNotIn("synthetic-only", json.dumps(Platforms.platform_catalog()))
        for item in first:
            self.assertNotIn("synthetic-only", json.dumps(item["parameters"]) + item["description"])
        second = Platforms.build_connection(payload(enabled_operations=[]), first)
        self.assertFalse({item["name"] for item in first} & {item["name"] for item in second})
        self.assertFalse(any(item["enabled"] for item in second))

    def test_reject_bad_payload_without_changes(self):
        config = {"tools_json": "[]"}
        writes = []
        catalog = CatalogModule.Catalog(config, lambda *args: writes.append(args))
        for invalid in [
            payload(token=""),
            payload(token="bad\nkey"),
            payload(token=42),
            payload(platform_id="unknown"),
            payload(enabled_operations=["DELETE"]),
            payload(enabled_operations=[{}]),
            payload(enabled_operations=["advertiser", "advertiser"]),
        ]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(Definitions.DefinitionError):
                    catalog.mutate(
                        "connect-platform", {**invalid, "revision": catalog.snapshot()["revision"]}
                    )
        with self.assertRaises(CatalogModule.ConflictError):
            catalog.mutate("connect-platform", {**payload(), "revision": "stale"})
        self.assertEqual(writes, [])
        self.assertEqual(config["tools_json"], "[]")


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_filters_pagination_and_token_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(Path(directory))
            await executor.client.aclose()
            requests = []

            def respond(request):
                requests.append(request)
                return httpx.Response(200, json={"code": 200, "count": 1, "results": [{"cost": 5}]})

            executor.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                definitions = Definitions.parse_definitions(
                    json.dumps(
                        Platforms.build_connection(
                            payload(enabled_operations=[op[0] for op in Platforms.OPERATIONS]), []
                        )
                    )
                )
                for definition in definitions:
                    result = await executor.execute(
                        definition,
                        {
                            "start": "2026-09-01",
                            "end": "2026-09-02",
                            "filter_country": ["us", "gb"],
                            "offset": 1000,
                        },
                    )
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["data"]["code"], 200)
                    req = requests[-1]
                    self.assertEqual(req.method, "GET")
                    self.assertEqual(req.url.params["api_key"], "synthetic-only")
                    self.assertEqual(req.url.params["filter_country"], "us,gb")
                    self.assertEqual(req.url.params["offset"], "1000")
                    self.assertEqual(req.url.params["limit"], "1000")
                    self.assertEqual(req.url.params["format"], "json")
                    self.assertIn(",", req.url.params["columns"])
                    self.assertNotIn("sort_day", req.url.params)
                count = len(requests)
                rejected = await executor.execute(
                    definitions[0],
                    {"start": "2026-09-01", "end": "2026-09-02", "api_key": "override"},
                )
                self.assertFalse(rejected["ok"])
                self.assertEqual(len(requests), count)
            finally:
                await executor.close()
