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
    assert json.loads(await tool.call(None, text="hello world"))["data"] == {"text": "hello world"}
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
    assert not json.loads(await tool.call(None, text="old cached call"))["ok"]
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
    from discovery_fixture import spec

    await plugin.discovery.client.aclose()
    discovery_requests = []

    def document_response(req):
        discovery_requests.append(req)
        return httpx.Response(200, json=spec())

    plugin.discovery.client = httpx.AsyncClient(transport=httpx.MockTransport(document_response))
    discovered = await web_call(
        plugin.page_discover,
        {"target_url": "https://api.example.test/v1", "api_key": "synthetic-key"},
    )
    assert discovered.status_code == 200
    operations = json.loads(discovered.body)["operations"]
    assert len(operations) == 4 and all(item["supported"] for item in operations)
    assert all("synthetic-key" not in str(req.url) + str(req.headers) for req in discovery_requests)
    incoming = [dict(item["definition"], enabled=item["method"] == "GET") for item in operations]
    batch = await web_call(
        plugin.page_batch,
        {"revision": plugin.catalog.snapshot()["revision"], "definitions": incoming},
    )
    assert batch.status_code == 200 and len(plugin.tools) == 2
    assert all(item.definition.request["method"] == "GET" for item in plugin.tools)
    cached = plugin.tools[0]
    permissions = await web_call(
        plugin.page_permissions,
        {"revision": plugin.catalog.snapshot()["revision"], "enabled_names": []},
    )
    assert permissions.status_code == 200 and manager.func_list == [foreign]
    assert not json.loads(await cached.call(None))["ok"]
    restored = AstrBotConfig(config_path=config_path, default_config={"tools_json": "[]"})
    assert len(json.loads(restored["tools_json"])) == 4
    assert all(not item["enabled"] for item in json.loads(restored["tools_json"]))
    # Ordinary documents use a provider with no tools, chat history or API credential.
    from unittest.mock import AsyncMock

    from discovery_fixture import TEXT, ordinary_spec

    model_call = AsyncMock(
        return_value=types.SimpleNamespace(completion_text=json.dumps(ordinary_spec()))
    )
    model = types.SimpleNamespace(text_chat=model_call)
    with patch.object(context, "get_using_provider_async", AsyncMock(return_value=model)):
        ordinary = await web_call(
            plugin.page_discover,
            {
                "target_url": "https://api.example.test",
                "api_key": "private-test-key",
                "document_text": TEXT,
            },
        )
    assert ordinary.status_code == 200
    result = json.loads(ordinary.body)
    assert result["inferred"] and result["operations"][0]["supported"]
    kwargs = model_call.call_args.kwargs
    assert kwargs["contexts"] == [] and kwargs["func_tool"] is None
    assert "private-test-key" not in json.dumps(kwargs)
    assert not result["operations"][0]["definition"]["enabled"]
    assert len(plugin.catalog.snapshot()["items"]) == 4  # Discovery never saves.
    with patch.object(context, "get_using_provider_async", AsyncMock(return_value=None)):
        unavailable = await web_call(
            plugin.page_discover,
            {
                "target_url": "https://api.example.test",
                "document_text": TEXT,
            },
        )
    assert unavailable.status_code == 400
    model.text_chat = AsyncMock(side_effect=RuntimeError("private-upstream-detail"))
    with patch.object(context, "get_using_provider_async", AsyncMock(return_value=model)):
        failed_model = await web_call(
            plugin.page_discover,
            {
                "target_url": "https://api.example.test",
                "document_text": TEXT,
            },
        )
    assert failed_model.status_code == 400
    assert b"private-upstream-detail" not in failed_model.body
    await plugin.terminate()
    assert manager.func_list == [foreign]
    assert not context.registered_web_apis
    assert not json.loads(await tool.call(None, text="late"))["ok"]
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
