"""Real AstrBot tool registration and commands; never start an adapter or make network requests."""

import asyncio
import importlib
import json
import os
import socket
import sys
import tempfile
import types
from pathlib import Path


def deny_network(*args, **kwargs):
    raise AssertionError("Outbound network forbidden in integration test")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
workspace = tempfile.TemporaryDirectory()
os.environ["ASTRBOT_ROOT"] = workspace.name


async def main():
    import httpx
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.provider.func_tool_manager import FuncCall
    from astrbot.core.star.context import Context

    package = types.ModuleType("data.plugins.astrbot_plugin_api_import")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    module = importlib.import_module(package.__name__ + ".main")
    context = Context.__new__(Context)
    context._config = {"http_proxy": ""}
    context.provider_manager = types.SimpleNamespace(llm_tools=FuncCall())
    manager = context.get_llm_tool_manager()
    foreign = FunctionTool(name="foreign", description="unrelated", parameters={"type": "object"})
    manager.func_list.append(foreign)
    definition = {
        "name": "echo",
        "description": "Echo",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "request": {
            "method": "POST",
            "url": "https://example.test",
            "json": {"text": {"$param": "text"}},
        },
    }
    plugin = module.ApiImportPlugin(context, {"tools_json": json.dumps([definition])})
    await plugin.initialize()
    assert len(plugin.tools) == 1
    tool = plugin.tools[0]
    assert tool.handler_module_path == "data.plugins.astrbot_plugin_api_import.main", (
        tool.handler_module_path
    )
    assert tool.parameters["required"] == ["text"]
    await plugin.executor.client.aclose()
    plugin.executor.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=json.loads(request.content))
        )
    )
    successful = await tool.call(None, text="hello world")
    assert not successful.isError
    assert json.loads(successful.content[0].text)["data"] == {"text": "hello world"}
    invalid_call = await tool.call(None, unexpected=True)
    assert invalid_call.isError
    assert "data.plugins.astrbot_plugin_feishu_agent_card" not in sys.modules
    event = types.SimpleNamespace(
        message_str='/api_test api_echo {"text": "hello world"}', plain_result=lambda text: text
    )
    outputs = [result async for result in plugin.test_tool(event)]
    assert json.loads(outputs[0])["data"] == {"text": "hello world"}
    # Save through actual PluginRequest handlers using the real atomic AstrBotConfig.
    from astrbot.api import AstrBotConfig
    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.requests import Request

    config_path = str(Path(workspace.name) / "api-config.json")
    plugin.config = AstrBotConfig(
        config_path=config_path, default_config={"tools_json": json.dumps([definition])}
    )
    plugin.catalog.config = plugin.config

    async def web_call(handler, payload):
        body = json.dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        req = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/test",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("test", 80),
            },
            receive,
        )
        with bind_request_context(
            PluginRequest(req, plugin_name="astrbot_plugin_api_import", username="test")
        ):
            return await handler()

    revision = plugin.catalog.snapshot()["revision"]
    updated = {**definition, "name": "renamed"}
    response = await web_call(
        plugin.page_save, {"revision": revision, "original_name": "echo", "definition": updated}
    )
    assert response.status_code == 200
    assert (
        json.loads(Path(config_path).read_text(encoding="utf-8-sig"))["tools_json"]
        == plugin.config["tools_json"]
    )
    assert not json.loads((await tool.call(None, text="old cached call")).content[0].text)["ok"]
    assert [item.name for item in manager.func_list] == ["foreign", "api_renamed"]
    stale = await web_call(plugin.page_delete, {"revision": revision, "name": "renamed"})
    assert stale.status_code == 409
    current_tool = plugin.tools[0]
    # A persistence error must restore registry, memory configuration and old tool availability.
    from unittest.mock import patch

    current_raw = plugin.config["tools_json"]
    with patch.object(AstrBotConfig, "save_config", side_effect=OSError("test disk failure")):
        failure = await web_call(
            plugin.page_save,
            {
                "revision": plugin.catalog.snapshot()["revision"],
                "original_name": "renamed",
                "definition": definition,
            },
        )
    assert failure.status_code == 500 and plugin.config["tools_json"] == current_raw
    assert plugin.tools[0] is current_tool and current_tool.available
    assert manager.func_list == [foreign, current_tool]
    imported = await web_call(plugin.page_import_curl, {"text": "curl https://example.test"})
    assert imported.status_code == 200 and not json.loads(imported.body)["definition"]["enabled"]
    deleted = await web_call(
        plugin.page_delete, {"revision": plugin.catalog.snapshot()["revision"], "name": "renamed"}
    )
    assert deleted.status_code == 200 and manager.func_list == [foreign]
    assert json.loads(Path(config_path).read_text(encoding="utf-8-sig"))["tools_json"] == "[]"
    platforms = await plugin.page_platforms()
    assert len(json.loads(platforms.body)["platforms"][0]["operations"]) == 6
    assert not any(
        "discover" in route[0] or "document-models" in route[0]
        for route in context.registered_web_apis
    )
    connected = await web_call(
        plugin.page_connect_platform,
        {
            "revision": plugin.catalog.snapshot()["revision"],
            "platform_id": "applovin_report",
            "token": "synthetic-key",
            "enabled_operations": ["advertiser", "cohort_sessions"],
        },
    )
    assert connected.status_code == 200 and len(plugin.tools) == 2
    assert all(
        "synthetic-key" not in json.dumps(tool.parameters) + tool.description
        for tool in plugin.tools
    )
    cached_before_rename = plugin.tools[0]
    first = plugin.catalog.snapshot()["items"][0]
    renamed_connection = await web_call(
        plugin.page_update_connection,
        {
            "revision": plugin.catalog.snapshot()["revision"],
            "connection_id": first["connection"]["id"],
            "name": "验收账户",
            "token": "",
            "enabled_names": [item.definition.name for item in plugin.tools],
        },
    )
    assert renamed_connection.status_code == 200
    assert "验收账户" not in plugin.tools[0].description
    assert plugin.tools[0].display_name == plugin.tools[0].name
    assert not hasattr(plugin.tools[0], "result_status_format")
    assert not cached_before_rename.available
    cached = plugin.tools[0]
    permissions = await web_call(
        plugin.page_permissions,
        {
            "revision": plugin.catalog.snapshot()["revision"],
            "enabled_names": [plugin.tools[1].definition.name],
        },
    )
    assert permissions.status_code == 200 and len(plugin.tools) == 1
    assert "cohort_sessions" in plugin.tools[0].name
    assert not json.loads((await cached.call(None)).content[0].text)["ok"]
    restored = AstrBotConfig(config_path=config_path, default_config={"tools_json": "[]"})
    assert len(json.loads(restored["tools_json"])) == 6
    assert sum(item["enabled"] for item in json.loads(restored["tools_json"])) == 1
    # Renaming changes the real registered name without a presentation plugin.
    named = plugin.catalog.snapshot()["items"][-1]
    named["tool_name"] = "Ninety_retention"
    previous = plugin.tools[0]
    plugin.catalog.mutate(
        "save",
        {
            "revision": plugin.catalog.snapshot()["revision"],
            "original_name": named["name"],
            "definition": named,
        },
    )
    assert plugin.tools[0].name == "Ninety_retention"
    assert not previous.available
    assert plugin.tools[0].definition.request == previous.definition.request
    # Meta catalog is independent of card plugins; closed permissions invalidate queued calls.
    meta_connected = await web_call(
        plugin.page_connect_platform,
        {
            "revision": plugin.catalog.snapshot()["revision"],
            "platform_id": "meta_marketing",
            "name": "Meta 后台账户",
            "call_name": "Meta_Main",
            "token": "synthetic-meta-key",
            "enabled_operations": ["get_adaccounts", "post_campaigns"],
        },
    )
    assert meta_connected.status_code == 200
    assert len(plugin.tools) == 3
    meta_tools = [t for t in plugin.tools if t.name.startswith("Meta_Main_")]
    assert {t.name for t in meta_tools} == {"Meta_Main_get_adaccounts", "Meta_Main_post_campaigns"}
    sent = []

    def meta_response(request):
        sent.append(request)
        return httpx.Response(200, json={"data": [{"id": "act_123"}]})

    meta_tool = next(t for t in meta_tools if t.name == "Meta_Main_get_adaccounts")
    assert (await meta_tool.call(None, node_id="me")).isError
    assert not sent
    proxy_url = "http://fixed.test:17900"
    plugin.proxy_nodes.path.parent.mkdir(parents=True, exist_ok=True)
    plugin.proxy_nodes.path.write_text(
        json.dumps(
            [
                {"id": "fixed_a", "name": "固定节点 A", "url": proxy_url},
                {"id": "fixed_b", "name": "固定节点 B", "url": "http://fixed.test:17901"},
            ]
        )
    )
    app_tool = next(t for t in plugin.tools if not t.name.startswith("Meta_Main_"))
    revision = plugin.proxy_nodes.snapshot()["revision"]
    rejected = await web_call(
        plugin.page_save_meta_proxy, {"revision": revision, "node_id": "arbitrary"}
    )
    assert rejected.status_code == 400
    saved = await web_call(
        plugin.page_save_meta_proxy, {"revision": revision, "node_id": "fixed_a"}
    )
    assert saved.status_code == 200
    assert not meta_tool.available and app_tool in plugin.tools and app_tool.available
    stale = await web_call(
        plugin.page_save_meta_proxy, {"revision": revision, "node_id": "fixed_b"}
    )
    assert stale.status_code == 409
    assert plugin.config["meta_proxy_node"] == "fixed_a"
    meta_tools = [t for t in plugin.tools if t.name.startswith("Meta_Main_")]
    plugin.executor.fixed_proxy_clients[proxy_url] = httpx.AsyncClient(
        transport=httpx.MockTransport(meta_response)
    )
    meta_tool = next(t for t in meta_tools if t.name == "Meta_Main_get_adaccounts")
    assert "/me/adaccounts" in meta_tool.description
    assert "User" in meta_tool.description
    assert not (await meta_tool.call(None, node_id="me")).isError
    assert len(sent) == 1
    # Simulate transport waiting for a connection, before any upstream side effect.
    waiting = asyncio.Event()
    transport_cancelled = asyncio.Event()

    async def waiting_transport(request):
        waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            transport_cancelled.set()
            raise
        raise AssertionError("Revoked request must never proceed")

    await plugin.executor.fixed_proxy_clients[proxy_url].aclose()
    plugin.executor.fixed_proxy_clients[proxy_url] = httpx.AsyncClient(
        transport=httpx.MockTransport(waiting_transport)
    )
    connecting = asyncio.create_task(meta_tool.call(None, node_id="me"))
    await waiting.wait()
    plugin.executor.semaphore = asyncio.Semaphore(0)
    queued = asyncio.create_task(meta_tool.call(None, node_id="me"))
    await asyncio.sleep(0)
    meta_items = [
        item
        for item in plugin.catalog.snapshot()["items"]
        if item.get("connection", {}).get("platform") == "meta_marketing"
    ]
    meta_disabled = await web_call(
        plugin.page_update_connection,
        {
            "revision": plugin.catalog.snapshot()["revision"],
            "connection_id": meta_items[0]["connection"]["id"],
            "name": "Meta 后台账户",
            "enabled_names": [],
            "token": "",
        },
    )
    assert meta_disabled.status_code == 200
    assert (await connecting).isError
    assert transport_cancelled.is_set()
    assert not meta_tool.pending_calls or all(t.done() for t in meta_tool.pending_calls)
    plugin.executor.semaphore.release()
    assert (await queued).isError and len(sent) == 1
    assert (await meta_tool.call(None, node_id="me")).isError
    assert len(plugin.tools) == 1  # AppLovin remains independently available.
    assert "data.plugins.astrbot_plugin_feishu_agent_card" not in sys.modules
    await plugin.terminate()
    assert manager.func_list == [foreign]
    assert not context.registered_web_apis
    assert not json.loads((await tool.call(None, text="late")).content[0].text)["ok"]
    # Reload registers exactly one tool and respects disabled definitions.
    again = module.ApiImportPlugin(context, {"tools_json": json.dumps([definition])})
    await again.initialize()
    assert len(manager.func_list) == 2
    await again.terminate()
    definition["enabled"] = False
    disabled = module.ApiImportPlugin(context, {"tools_json": json.dumps([definition])})
    await disabled.initialize()
    assert not disabled.tools and manager.func_list == [foreign]
    await disabled.terminate()
    invalid = module.ApiImportPlugin(context, {"tools_json": "bad"})
    await invalid.initialize()
    assert invalid.configuration_error and manager.func_list == [foreign]
    await invalid.terminate()
    definition["enabled"] = True
    foreign.name = "api_echo"
    conflict = module.ApiImportPlugin(context, {"tools_json": json.dumps([definition])})
    await conflict.initialize()
    assert conflict.configuration_error and manager.func_list == [foreign]
    await conflict.terminate()
    print(
        "Real AstrBot integration passed: registration, ownership, call, commands, reload, disabled, invalid, collision"
    )


asyncio.run(main())
workspace.cleanup()
