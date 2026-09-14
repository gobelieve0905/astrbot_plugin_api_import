import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_gateway import TaskGateway, redact


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def execute(arguments, **kwargs):
            return {
                "ok": True,
                "data": arguments,
                "file": "/private/result",
                "access_token": "secret",
            }

        self.tool = types.SimpleNamespace(
            name="api_read",
            active=True,
            available=True,
            description="Read",
            parameters={},
            execute=execute,
            definition=types.SimpleNamespace(
                request={"headers": {"Authorization": "Bearer secret"}}
            ),
        )
        self.plugin = types.SimpleNamespace(tools=[self.tool], closed=False)
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = TaskGateway(self.plugin)
        self.path = Path(self.temp.name) / "gateway.sock"
        await self.gateway.start(self.path)
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.path))

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()
        await self.gateway.close()
        self.temp.cleanup()

    async def rpc(self, **payload):
        self.writer.write(json.dumps(payload).encode() + b"\n")
        await self.writer.drain()
        return json.loads(await self.reader.readline())

    async def issue(self, **extra):
        return await self.rpc(action="issue", scopes={"api_read": {"node_id": "act_1"}}, **extra)

    async def test_discovery_and_preflight_never_execute_or_grant(self):
        from unittest.mock import AsyncMock

        self.tool.execute = AsyncMock(side_effect=AssertionError("must not execute"))
        self.tool.parameters = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        found = await self.rpc(action="inspect", query="read")
        self.assertEqual(found["total"], 1)
        self.assertNotIn("credential", found)
        detail = await self.rpc(
            action="inspect", tool="api_read", arguments={"value": 2}, constraints={"value": 1}
        )
        self.assertFalse(detail["valid"])
        self.assertEqual(detail["errors"][0]["rule"], "fixed_constraint")
        bad = await self.rpc(action="inspect", tool="api_read", arguments={"value": "bad"})
        self.assertFalse(bad["valid"])
        good = await self.rpc(action="inspect", tool="api_read", arguments={"value": 1})
        self.assertTrue(good["valid"])
        self.assertNotIn("credential", good)
        self.assertFalse(
            (await self.rpc(action="call", tool="api_read", arguments={"value": 1}))["ok"]
        )
        self.tool.active = False
        self.assertFalse((await self.rpc(action="inspect", tool="api_read"))["ok"])
        self.assertEqual((await self.rpc(action="inspect"))["total"], 0)
        self.tool.execute.assert_not_called()

    async def test_scope_quota_and_redaction(self):
        grant = await self.issue(quota=2)
        first = await self.rpc(
            action="call",
            credential=grant["credential"],
            tool="api_read",
            arguments={"node_id": "act_2"},
        )
        self.assertFalse(first["ok"])
        result = await self.rpc(
            action="call",
            credential=grant["credential"],
            tool="api_read",
            arguments={"node_id": "act_1", "echo": "secret"},
        )
        self.assertTrue(result["ok"])
        self.assertNotIn("file", result)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["remaining"], 0)
        self.assertFalse(
            (
                await self.rpc(
                    action="call",
                    credential=grant["credential"],
                    tool="api_read",
                    arguments={"node_id": "act_1"},
                )
            )["ok"]
        )

    async def test_admin_budget_is_enforced_and_errors_distinct(self):
        denied = await self.issue(quota=201)
        self.assertEqual(denied["error_code"], "QUOTA_POLICY")
        self.plugin.config = {"task_max_calls": 300}
        grant = await self.issue(quota=250)
        self.assertEqual(grant["quota"], 250)
        self.tool.active = False
        denied = await self.rpc(
            action="call",
            credential=grant["credential"],
            tool="api_read",
            arguments={"node_id": "act_1"},
        )
        self.assertEqual(denied["error_code"], "PERMISSION_REVOKED")

    async def test_quota_exhaustion_has_stable_error(self):
        grant = await self.issue(quota=1)
        kwargs = dict(
            action="call",
            credential=grant["credential"],
            tool="api_read",
            arguments={"node_id": "act_1"},
        )
        self.assertTrue((await self.rpc(**kwargs))["ok"])
        denied = await self.rpc(**kwargs)
        self.assertEqual(denied["error_code"], "QUOTA_EXHAUSTED")
        self.assertEqual(denied["remaining"], 0)

    async def test_revoke_current_permissions(self):
        grant = await self.issue()
        self.tool.active = False
        result = await self.rpc(
            action="call",
            credential=grant["credential"],
            tool="api_read",
            arguments={"node_id": "act_1"},
        )
        self.assertFalse(result["ok"])

    async def test_expiry_and_no_admin_method(self):
        grant = await self.issue(ttl=1)
        self.assertFalse((await self.rpc(action="permissions", enabled=True))["ok"])
        await asyncio.sleep(1.05)
        self.assertFalse(
            (
                await self.rpc(
                    action="call",
                    credential=grant["credential"],
                    tool="api_read",
                    arguments={"node_id": "act_1"},
                )
            )["ok"]
        )

    async def test_disabled_not_granted_and_wrong_credential(self):
        self.tool.active = False
        self.assertFalse((await self.issue())["ok"])
        self.tool.active = True
        await self.issue()
        self.assertFalse(
            (await self.rpc(action="call", credential="wrong", tool="api_read", arguments={}))["ok"]
        )

    async def test_disconnect_cancels_pending_request(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def pending(arguments, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.tool.execute = pending
        grant = await self.issue()
        self.writer.write(
            json.dumps(
                {
                    "action": "call",
                    "credential": grant["credential"],
                    "tool": "api_read",
                    "arguments": {"node_id": "act_1"},
                }
            ).encode()
            + b"\n"
        )
        await self.writer.drain()
        await asyncio.wait_for(entered.wait(), 2)
        self.writer.close()
        await self.writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 2)

    async def test_replacement_invalidates_grant(self):
        grant = await self.issue()
        self.plugin.tools = [types.SimpleNamespace(**vars(self.tool))]
        result = await self.rpc(
            action="call",
            credential=grant["credential"],
            tool="api_read",
            arguments={"node_id": "act_1"},
        )
        self.assertFalse(result["ok"])

    def test_encoded_secret_redacted(self):
        result = redact(
            {"data": "a%2Fb a/b", "headers": {"token": "a/b"}}, {"query": {"token": "a/b"}}
        )
        self.assertNotIn("a/b", json.dumps(result))
        self.assertNotIn("a%2Fb", json.dumps(result))
        self.assertEqual(
            redact({"data": "private"}, {"url": "https://example.test?token=private"}),
            {"data": "[REDACTED]"},
        )


class ResultDataTests(unittest.TestCase):
    def test_business_fields_survive_redaction(self):
        result = redact(
            {
                "file": "/private/result",
                "data": {
                    "keywords": ["apple"],
                    "monkey": 3,
                    "file": "report.csv",
                    "access_token": "private",
                },
            },
            {},
        )
        self.assertNotIn("file", result)
        self.assertEqual(
            result["data"],
            {
                "keywords": ["apple"],
                "monkey": 3,
                "file": "report.csv",
                "access_token": "[REDACTED]",
            },
        )
