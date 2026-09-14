"""Regression for the observed advertiser alias/string failure; no real report requests."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
from test_platforms import Definitions, Executor, Platforms, payload


class AdvertiserCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.requests = []

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={"results": []})

        self.executor = Executor(
            Path(self.temp.name), httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )
        self.definition = Definitions.parse_definitions(
            json.dumps(Platforms.build_connection(payload(), []))
        )[0]
        self.arguments = {
            "start": "2026-09-12",
            "end": "2026-09-13",
            "columns": ["country", "campaign", "cost"],
            "filter_package_name": "com.example.game",
            "sort_day": "DESC",
            "limit": 1000,
        }

    async def asyncTearDown(self):
        await self.executor.close()
        self.temp.cleanup()

    async def test_observed_alias_and_single_value_reach_canonical_wire(self):
        result = await self.executor.execute(self.definition, self.arguments)
        self.assertTrue(result["ok"], result)
        query = self.requests[0].url.params
        self.assertEqual(query["filter_campaign_package_name"], "com.example.game")
        self.assertNotIn("filter_package_name", query)
        self.assertEqual(query["columns"], "country,campaign,cost")
        self.assertEqual(query["start"], "2026-09-12")
        self.assertEqual(self.arguments["filter_package_name"], "com.example.game")

    async def test_canonical_scalar_and_multiple_values(self):
        for value in ["com.example.game", ["com.example.game", "com.example.second"]]:
            args = {k: v for k, v in self.arguments.items() if k != "filter_package_name"}
            args["filter_campaign_package_name"] = value
            self.assertTrue((await self.executor.execute(self.definition, args))["ok"])
        self.assertEqual(
            self.requests[-1].url.params["filter_campaign_package_name"],
            "com.example.game,com.example.second",
        )

    async def test_conflicts_ambiguous_values_and_unknown_parameters_rejected(self):
        for extra in [
            {"filter_campaign_package_name": ["com.other.game"]},
            {"filter_package_name": "com.first,com.second"},
            {"filter_package_name": ""},
            {"filter_package_name": 123},
            {"api_key": "override"},
            {"unknown_filter": "x"},
        ]:
            result = await self.executor.execute(self.definition, {**self.arguments, **extra})
            self.assertFalse(result["ok"], extra)
        self.assertFalse(self.requests)
        self.assertIn("filter_campaign_package_name", result["error"])

    async def test_scope_and_disabled_tools_remain_strict(self):
        for definition in [
            replace(self.definition, enabled=False),
            replace(self.definition, name="custom", connection={}),
            replace(
                self.definition,
                request={**self.definition.request, "url": "https://example.test/report"},
            ),
        ]:
            self.assertFalse((await self.executor.execute(definition, self.arguments))["ok"])
        self.assertFalse(self.requests)
        # Recognizable pre-connection presets retain the compatibility path.
        self.assertTrue(
            (await self.executor.execute(replace(self.definition, connection={}), self.arguments))[
                "ok"
            ]
        )
