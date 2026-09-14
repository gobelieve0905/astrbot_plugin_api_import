"""Meta network routing checks; no external connections or production credentials."""

import unittest
from unittest.mock import patch

import httpx
import test_meta as base
from test_engine import Definitions
from test_meta import Engine, Meta, one


class MetaNetwork(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.MetaWireTests.asyncSetUp
    asyncTearDown = base.MetaWireTests.asyncTearDown
    call = base.MetaWireTests.call

    async def test_proxy_client_configuration_does_not_enable_environment_proxy(self):
        proxy_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        with patch.object(Engine.httpx, "AsyncClient", return_value=proxy_client) as factory:
            executor = Engine.Executor(
                self.executor.data_dir,
                client=self.executor.client,
                meta_proxy="http://proxy.test:7890",
            )
        factory.assert_called_once_with(
            proxy="http://proxy.test:7890", follow_redirects=False, trust_env=False
        )
        await executor.close()
        self.assertTrue(proxy_client.is_closed)

    async def test_only_authorized_meta_routes_use_proxy(self):
        proxied = []

        def handle(request):
            proxied.append(request)
            return httpx.Response(200, json={"data": []})

        self.executor.meta_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.assertFalse(
            (await self.executor.execute(one("get_adaccounts"), {"node_id": "me"}))["ok"]
        )
        self.assertFalse(proxied)
        self.assertTrue((await self.call("get_adaccounts", {"node_id": "me"}))["ok"])
        self.assertEqual(len(proxied), 1)
        self.assertFalse(self.sent)
        definition = Definitions.parse_definitions(
            '[{"name":"plain","description":"plain","request":{"method":"GET","url":"https://example.test"}}]'
        )[0]
        self.assertTrue((await self.executor.execute(definition, {}))["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(proxied), 1)

    async def test_proxy_failure_is_not_retried_or_rerouted_direct(self):
        attempts = []

        def handle(request):
            attempts.append(request)
            raise httpx.ProxyError("secret-diagnostic-not-for-output")

        self.executor.meta_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        result = await self.call("post_campaigns", {"node_id": "act_123"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["network_error_type"], "ProxyError")
        self.assertEqual(result["network_route"], "configured_proxy")
        self.assertNotIn("secret-diagnostic", str(result))
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.sent)

    async def test_wrong_discovery_route_does_not_auto_switch_permissions(self):
        result = await self.call("get_ad_accounts", {"node_id": "me"})
        self.assertFalse(result["ok"])
        self.assertIn("get_adaccounts", result["error"])
        self.assertFalse(self.sent)
        self.assertIn("/me/adaccounts", Meta.tool_guidance("get_adaccounts"))
        self.assertIn("不能用于发现当前用户", Meta.tool_guidance("get_ad_accounts"))

    async def test_fixed_node_failure_never_switches_or_uses_direct_client(self):
        attempts = []

        def fail(request):
            attempts.append("fixed_a")
            raise httpx.ConnectTimeout("private")

        self.executor.meta_proxy_resolver = lambda: "http://fixed.test:17900"
        self.executor.fixed_proxy_clients["http://fixed.test:17900"] = httpx.AsyncClient(
            transport=httpx.MockTransport(fail)
        )
        for _ in range(2):
            result = await self.call("get_adaccounts", {"node_id": "me"})
            self.assertFalse(result["ok"])
        self.assertEqual(attempts, ["fixed_a", "fixed_a"])
        self.assertFalse(self.sent)

    async def test_node_directory_fail_closed_and_hides_endpoint(self):
        import importlib
        import json

        FixedProxyNodes = importlib.import_module("api_import_test.proxy_nodes").FixedProxyNodes
        path = self.executor.data_dir / "nodes.json"
        settings = {}
        nodes = FixedProxyNodes(path, settings)
        self.executor.meta_proxy_resolver = nodes.resolve
        self.assertFalse((await self.call("get_adaccounts", {"node_id": "me"}))["ok"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{"id": "node_a", "name": "固定节点", "url": "http://private.test:17900"}])
        )
        self.assertNotIn("private.test", str(nodes.snapshot()))
        settings["meta_proxy_node"] = "unknown"
        self.assertFalse((await self.call("get_adaccounts", {"node_id": "me"}))["ok"])
        settings["meta_proxy_node"] = "node_a"
        self.assertEqual(nodes.resolve(), "http://private.test:17900")
        path.write_text("invalid")
        self.assertFalse((await self.call("get_adaccounts", {"node_id": "me"}))["ok"])
        self.assertFalse(self.sent)
