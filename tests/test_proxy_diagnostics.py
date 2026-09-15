import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from test_engine import Definitions

Nodes = importlib.import_module("api_import_test.proxy_nodes").FixedProxyNodes
Module = importlib.import_module("api_import_test.proxy_diagnostics")


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"meta_proxy_node": "a"}
        path = Path(self.tmp.name) / "nodes.json"
        path.write_text(json.dumps([{"id": "a", "name": "A", "url": "http://fixed.test:17900"}]))
        self.nodes = Nodes(path, self.config)
        self.diag = Module.ProxyDiagnostics(self.nodes)
        self.rev = self.nodes.snapshot()["revision"]

    async def test_meta_http_error_proves_reachability_without_credentials(self):
        seen = []

        def handle(r):
            seen.append(r)
            return httpx.Response(400)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(Module.httpx, "AsyncClient", return_value=client) as factory:
            result = await self.diag.probe("a", "meta", self.rev)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 400)
        self.assertEqual(str(seen[0].url), "https://graph.facebook.com/")
        self.assertNotIn("authorization", seen[0].headers)
        self.assertNotIn("cookie", seen[0].headers)
        factory.assert_called_once_with(
            proxy="http://fixed.test:17900", trust_env=False, follow_redirects=False, timeout=10.0
        )
        self.assertEqual(self.config["meta_proxy_node"], "a")
        self.assertTrue(client.is_closed)

    async def test_errors_are_redacted_and_never_retried(self):
        seen = []

        def handle(r):
            seen.append(r)
            raise httpx.ConnectError("SECRET proxy-address", request=r)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(Module.httpx, "AsyncClient", return_value=client):
            result = await self.diag.probe("a", "meta", self.rev)
        self.assertFalse(result["ok"])
        self.assertNotIn("SECRET", str(result))
        self.assertEqual(len(seen), 1)
        self.assertFalse(self.diag.running)

    async def test_arbitrary_target_unknown_node_and_stale_revision_rejected(self):
        with patch.object(Module.httpx, "AsyncClient") as factory:
            for node, kind, revision in [
                ("a", "http://localhost", self.rev),
                ("unknown", "meta", self.rev),
                ("a", "meta", "old"),
            ]:
                with self.assertRaises(Definitions.DefinitionError):
                    await self.diag.probe(node, kind, revision)
            factory.assert_not_called()

    async def test_redirect_is_not_success_and_not_followed(self):
        seen = []

        def handle(r):
            seen.append(r)
            return httpx.Response(302, headers={"location": "http://localhost/secret"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)
        with patch.object(Module.httpx, "AsyncClient", return_value=client):
            result = await self.diag.probe("a", "latency", self.rev)
        self.assertFalse(result["ok"])
        self.assertEqual(len(seen), 1)

    async def test_directory_change_during_probe_discards_result(self):
        def handle(r):
            self.config["meta_proxy_node"] = ""
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(Module.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(Definitions.DefinitionError):
                await self.diag.probe("a", "latency", self.rev)
        self.assertFalse(self.diag.results)

    async def test_concurrency_cap_and_cancel_release(self):
        self.diag.running = {("b", "meta"), ("c", "meta"), ("d", "meta")}
        with self.assertRaises(Definitions.DefinitionError):
            await self.diag.probe("a", "meta", self.rev)
        self.diag.running.clear()
        entered = asyncio.Event()

        async def handle(r):
            entered.set()
            await asyncio.Event().wait()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(Module.httpx, "AsyncClient", return_value=client):
            task = asyncio.create_task(self.diag.probe("a", "meta", self.rev))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.diag.running)
        self.assertTrue(client.is_closed)

    async def test_refresh_missing_service_preserves_selection(self):
        with self.assertRaises(Definitions.DefinitionError):
            await self.diag.refresh()
        self.assertEqual(self.nodes.snapshot()["revision"], self.rev)
        self.assertFalse(self.diag.refresh_lock.locked())
