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
