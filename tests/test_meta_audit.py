"""Permission audit: synthetic credentials and HTTPX mock transport only."""

import asyncio
import unittest
from dataclasses import replace

import test_meta as base
from test_meta import Meta, one


class MetaPermissionAudit(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.MetaWireTests.asyncSetUp
    asyncTearDown = base.MetaWireTests.asyncTearDown
    call = base.MetaWireTests.call

    async def test_every_disabled_route_and_cross_account_grant_denied(self):
        for op in Meta.operations().values():
            definition = one(op["id"])
            args = {key: "123" for key in definition.parameters["required"]}
            self.allowed = {("different_account", op["id"])}
            result = await self.executor.execute(definition, args)
            self.assertFalse(result["ok"], op["id"])
            self.assertFalse(self.sent, op["id"])
            self.allowed = {(definition.connection["id"], op["id"])}
            result = await self.executor.execute(replace(definition, enabled=False), args)
            self.assertFalse(result["ok"], op["id"])
            self.assertFalse(self.sent, op["id"])

    async def test_batch_writes_fail_closed_without_subrequest_authorization(self):
        for op in Meta.operations().values():
            if op["method"] != "GET" and "batch" in op["path"]:
                result = await self.call(op["id"], {"node_id": "123"})
                self.assertFalse(result["ok"], op["id"])
        self.assertFalse(self.sent)

    async def test_copy_requires_all_possible_create_permissions(self):
        args = {"node_id": "123", "params": {"deep_copy": True}}
        self.assertFalse((await self.call("post_copies", args))["ok"])
        for op in ["post_ads", "post_adsets"]:
            self.allowed.add(("abc123ab", op))
        self.assertFalse((await self.call("post_copies", args))["ok"])
        self.assertFalse(self.sent)
        self.allowed.add(("abc123ab", "post_campaigns"))
        self.assertTrue((await self.call("post_copies", args))["ok"])

    async def test_nested_deleted_status_and_whitespace_are_not_delete_bypasses(self):
        for params in [{"status": " DELETED "}, {"creative": {"status": "DELETED"}}]:
            self.assertFalse(
                (await self.call("post_ads", {"node_id": "123", "params": params}))["ok"]
            )
        self.assertFalse(self.sent)

    async def test_redirect_does_not_dispatch_second_request(self):
        self.status = 302
        result = await self.call("post_node", {"node_id": "123"})
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.sent), 1)

    async def test_queued_dependent_permission_revocation(self):
        self.allowed.update(
            {("abc123ab", x) for x in ["post_ads", "post_adsets", "post_campaigns"]}
        )
        self.executor.semaphore = asyncio.Semaphore(0)
        task = asyncio.create_task(self.call("post_copies", {"node_id": "123"}))
        await asyncio.sleep(0)
        self.allowed.discard(("abc123ab", "post_ads"))
        self.executor.semaphore.release()
        self.assertFalse((await task)["ok"])
        self.assertFalse(self.sent)

    async def test_all_authorized_single_routes_have_fixed_host_method_and_path(self):
        self.allowed = {("abc123ab", op) for op in Meta.operations()}
        for op in Meta.operations().values():
            definition = one(op["id"])
            args = {key: "123" for key in definition.parameters["required"]}
            before = len(self.sent)
            result = await self.executor.execute(definition, args)
            if op["method"] != "GET" and "batch" in op["path"]:
                self.assertFalse(result["ok"], op["id"])
                self.assertEqual(len(self.sent), before)
                continue
            self.assertTrue(result["ok"], (op["id"], result))
            req = self.sent[-1]
            self.assertEqual(req.method, op["method"])
            self.assertEqual(req.url.host, "graph.facebook.com")
            self.assertEqual(req.url.scheme, "https")
            url, _ = Meta.prepare(definition, args, self.executor.permitted)
            self.assertEqual(str(req.url), url)
            self.assertEqual(len(self.sent), before + 1)
