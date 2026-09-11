"""Account identity, legacy preservation and changes isolated to one credential."""

import copy
import importlib
import json
import unittest

from test_engine import Definitions

Platforms = importlib.import_module("api_import_test.platforms")
Connections = importlib.import_module("api_import_test.connections")
CatalogModule = importlib.import_module("api_import_test.catalog")


class ConnectionTests(unittest.TestCase):
    def test_actual_names_follow_account_and_explicit_override(self):
        items = Platforms.build_connection(
            {
                "platform_id": "applovin_report",
                "name": "AppLovin Report Ninety",
                "token": "fake",
                "enabled_operations": ["advertiser"],
            },
            [],
        )
        parsed = Definitions.parse_definitions(json.dumps(items))
        self.assertEqual(parsed[0].tool_name, "AppLovin_Report_Ninety_advertiser")
        items[0]["tool_name"] = "Ninety_daily_spend"
        self.assertEqual(
            Definitions.parse_definitions(json.dumps(items))[0].tool_name, "Ninety_daily_spend"
        )
        items[1]["tool_name"] = "Ninety_daily_spend"
        with self.assertRaises(Definitions.DefinitionError):
            Definitions.parse_definitions(json.dumps(items))
        items[1]["tool_name"] = "bad name"
        with self.assertRaises(Definitions.DefinitionError):
            Definitions.parse_definitions(json.dumps(items))

    def test_auto_names_remain_unique_across_duplicate_accounts(self):
        items = self.first + self.second
        for item in items:
            item["connection"]["name"] = "Same Account"
        parsed = Definitions.parse_definitions(json.dumps(items))
        self.assertEqual(len({d.tool_name for d in parsed}), len(items))
        self.assertTrue(all(Definitions.TOOL_NAME.fullmatch(d.tool_name) for d in parsed))
        unicode_names = Definitions.parse_definitions(json.dumps(self.first + self.second))
        self.assertEqual(len({d.tool_name for d in unicode_names}), len(items))

    def setUp(self):
        self.first = Platforms.build_connection(
            {
                "platform_id": "applovin_report",
                "name": "国内账户",
                "token": "first-key",
                "enabled_operations": ["advertiser"],
            },
            [],
        )
        self.second = Platforms.build_connection(
            {
                "platform_id": "applovin_report",
                "name": "海外账户",
                "token": "second-key",
                "enabled_operations": ["max_revenue"],
            },
            self.first,
        )
        self.config = {"tools_json": json.dumps(self.first + self.second)}
        self.catalog = CatalogModule.Catalog(self.config, self.apply)

    def apply(self, raw, definitions):
        self.config["tools_json"] = raw

    def change(self, **kwargs):
        return self.catalog.mutate(
            "update-connection",
            {
                "revision": self.catalog.snapshot()["revision"],
                "connection_id": self.first[0]["connection"]["id"],
                "name": "新名称",
                "token": "",
                "enabled_names": [self.first[0]["name"]],
                **kwargs,
            },
        )

    def test_name_token_and_permission_are_account_scoped(self):
        result = self.change(token="rotated-key", enabled_names=[self.first[1]["name"]])
        self.assertEqual(result["items"][6:], self.second)
        for item in result["items"][:6]:
            self.assertEqual(item["connection"]["name"], "新名称")
            self.assertEqual(item["request"]["query"]["api_key"], "rotated-key")
            self.assertEqual(item["enabled"], item["name"] == self.first[1]["name"])
        parsed = Definitions.parse_definitions(self.config["tools_json"])
        self.assertIn("接入账户：新名称", parsed[0].description)
        self.assertIn("接入账户：海外账户", parsed[6].description)
        self.assertEqual([item.name for item in parsed][:6], [item["name"] for item in self.first])
        self.change()
        self.assertEqual(
            self.catalog.snapshot()["items"][0]["request"]["query"]["api_key"], "rotated-key"
        )

    def test_legacy_is_non_destructive_and_separate(self):
        legacy = copy.deepcopy(self.first + self.second)
        for item in legacy:
            del item["connection"]
            del item["display_name"]
        self.config["tools_json"] = raw = json.dumps(legacy)
        migrated = self.catalog.snapshot()["items"]
        self.assertEqual(self.config["tools_json"], raw)
        self.assertEqual(len({item["connection"]["id"] for item in migrated}), 2)
        for old, new in zip(legacy, migrated, strict=True):
            self.assertEqual({key: new[key] for key in old}, old)
        self.change()
        self.assertEqual(json.loads(self.config["tools_json"])[0]["connection"]["name"], "新名称")

    def test_legacy_modified_credentials_not_guessed(self):
        legacy = copy.deepcopy(self.first)
        for item in legacy:
            del item["connection"]
        legacy[0]["request"]["query"]["api_key"] = "separate-account"
        self.assertFalse(any(item.get("connection") for item in Connections.enrich_legacy(legacy)))

    def test_reject_cross_account_or_stale_change(self):
        raw = self.config["tools_json"]
        for kwargs in [
            {"enabled_names": [self.second[0]["name"]]},
            {"name": " "},
            {"token": "bad key"},
            {"revision": "stale"},
        ]:
            with self.assertRaises(Definitions.DefinitionError):
                self.change(**kwargs)
            self.assertEqual(self.config["tools_json"], raw)

    def test_delete_one_account(self):
        result = self.catalog.mutate(
            "delete-connection",
            {
                "revision": self.catalog.snapshot()["revision"],
                "connection_id": self.first[0]["connection"]["id"],
            },
        )
        self.assertEqual(result["items"], self.second)

    def test_rotation_rejects_modified_endpoint_atomically(self):
        self.first[-1]["request"]["url"] = "https://example.test/changed"
        self.config["tools_json"] = raw = json.dumps(self.first + self.second)
        with self.assertRaises(Definitions.DefinitionError):
            self.change(token="rotated-key")
        self.assertEqual(self.config["tools_json"], raw)

    def test_connection_metadata_must_be_consistent(self):
        self.first[0]["connection"]["name"] = "conflict"
        with self.assertRaises(Definitions.DefinitionError):
            Definitions.parse_definitions(json.dumps(self.first))
