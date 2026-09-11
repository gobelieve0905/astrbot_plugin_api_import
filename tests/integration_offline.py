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
    await plugin.terminate()
    assert manager.func_list == [foreign]
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
