"""AstrBot integration for administrator-defined HTTP tools."""

import asyncio
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.tool import FunctionTool
from mcp.types import CallToolResult, TextContent

from .catalog import Catalog, ConflictError
from .definitions import DefinitionError, parse_definitions
from .engine import Executor
from .importing import import_curl
from .platforms import platform_catalog


class ImportedTool(FunctionTool):
    def __init__(self, definition, executor):
        super().__init__(
            name=definition.tool_name,
            description=definition.description,
            parameters=definition.parameters,
        )
        self.display_name = definition.display_name
        self.definition = definition
        self.executor = executor
        self.available = True
        self.pending_calls = set()

    def invalidate(self):
        self.available = False
        for task in tuple(self.pending_calls):
            task.cancel()

    async def call(self, context, **kwargs):
        if not self.available or not self.active:
            result = {"ok": False, "error": "该工具已更新、删除或停用，请重新选择工具"}
        else:
            task = asyncio.create_task(
                self.executor.execute(
                    self.definition, kwargs, guard=lambda: self.available and self.active
                )
            )
            self.pending_calls.add(task)
            try:
                result = await task
            except asyncio.CancelledError:
                if self.available:
                    raise
                result = {
                    "ok": False,
                    "error": "后台已更新或停用工具，本地调用已取消；若请求已经发出，远端可能已执行，请核实状态，勿自动重试写操作",
                }
            finally:
                self.pending_calls.discard(task)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            isError=not result["ok"],
        )


class ApiImportPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.definitions = []
        self.tools = []
        self.executor = None
        self.configuration_error = None
        self.closed = False
        self.catalog = Catalog(config, self._apply_saved)
        self.edit_lock = asyncio.Lock()
        self.web_handlers = []
        for route, handler, methods in (
            ("catalog", self.page_catalog, ["GET"]),
            ("save", self.page_save, ["POST"]),
            ("delete", self.page_delete, ["POST"]),
            ("import-curl", self.page_import_curl, ["POST"]),
            ("platforms", self.page_platforms, ["GET"]),
            ("connect-platform", self.page_connect_platform, ["POST"]),
            ("batch", self.page_batch, ["POST"]),
            ("update-connection", self.page_update_connection, ["POST"]),
            ("delete-connection", self.page_delete_connection, ["POST"]),
            ("permissions", self.page_permissions, ["POST"]),
        ):
            self.context.register_web_api(
                f"/astrbot_plugin_api_import/{route}", handler, methods, "API 接口管理"
            )
            self.web_handlers.append(handler)

    def _prepare_tools(self, definitions):
        manager = self.context.get_llm_tool_manager()
        owned = {id(tool) for tool in self.tools}
        existing = {tool.name for tool in manager.func_list if id(tool) not in owned}
        if any(item.enabled and item.tool_name in existing for item in definitions):
            raise DefinitionError("存在与其他插件同名的工具，请修改接口调用名称 tool_name")
        return [ImportedTool(item, self.executor) for item in definitions if item.enabled]

    def _apply_saved(self, raw, definitions):
        # No awaits between validation, atomic config persistence and registry swap.
        # A failed write restores the old registry and leaves cached tools usable.
        if self.closed:
            raise DefinitionError("插件已卸载，请刷新页面")
        new_tools = self._prepare_tools(definitions)
        manager = self.context.get_llm_tool_manager()
        previous_registry = list(manager.func_list)
        previous_raw = self.config.get("tools_json", "[]")
        owned = {id(tool) for tool in self.tools}
        try:
            manager.func_list[:] = [tool for tool in manager.func_list if id(tool) not in owned]
            if new_tools:
                self.context.add_llm_tools(*new_tools)
            self.config["tools_json"] = raw
            self.config.save_config()
        except Exception:
            manager.func_list[:] = previous_registry
            self.config["tools_json"] = previous_raw
            raise
        for tool in self.tools:
            tool.invalidate()
        self.tools = new_tools
        self.definitions = definitions
        self.configuration_error = None

    async def initialize(self):
        self.executor = Executor(StarTools.get_data_dir("astrbot_plugin_api_import") / "results")
        self.executor.permitted = lambda connection_id, operation: any(
            tool.available
            and tool.active
            and tool.definition.connection.get("id") == connection_id
            and tool.definition.connection.get("operation") == operation
            for tool in self.tools
        )
        try:
            definitions = parse_definitions(self.config.get("tools_json", "[]"))
            tools = self._prepare_tools(definitions)
        except DefinitionError as exc:
            self.configuration_error = str(exc)
            logger.error("API 工具配置未加载: " + self.configuration_error)
            return
        self.definitions = definitions
        self.tools = tools
        try:
            if self.tools:
                self.context.add_llm_tools(*self.tools)
        except Exception:
            await self.terminate()
            raise
        logger.info(f"API 工具接入已加载 {len(self.tools)} 个工具")

    async def page_catalog(self):
        if self.closed:
            return error_response("插件已卸载，请刷新页面", status_code=503)
        state = self.catalog.snapshot()
        state["error"] = state["error"] or self.configuration_error
        return json_response(state)

    async def _page_mutate(self, action):
        try:
            payload = await request.json()
            async with self.edit_lock:
                return json_response(self.catalog.mutate(action, payload))
        except ConflictError as exc:
            return error_response(str(exc), status_code=409)
        except DefinitionError as exc:
            return error_response(str(exc))
        except Exception:
            logger.error("API 管理保存失败，已保留原配置和工具")
            return error_response("保存失败，原配置和工具已保留", status_code=500)

    async def page_save(self):
        return await self._page_mutate("save")

    async def page_delete(self):
        return await self._page_mutate("delete")

    async def page_import_curl(self):
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise DefinitionError("请求必须是 JSON 对象")
            return json_response({"definition": import_curl(payload.get("text"))})
        except (DefinitionError, ValueError) as exc:
            return error_response(str(exc))

    async def page_update_connection(self):
        return await self._page_mutate("update-connection")

    async def page_delete_connection(self):
        return await self._page_mutate("delete-connection")

    async def page_batch(self):
        return await self._page_mutate("batch")

    async def page_permissions(self):
        return await self._page_mutate("permissions")

    async def page_platforms(self):
        if self.closed:
            return error_response("插件已卸载，请刷新页面", status_code=503)
        return json_response(platform_catalog())

    async def page_connect_platform(self):
        return await self._page_mutate("connect-platform")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("api_tools")
    async def list_tools(self, event: AstrMessageEvent):
        """查看接口加载状态；修改配置后请在后台重载插件。"""
        if self.configuration_error:
            yield event.plain_result("配置未加载：" + self.configuration_error)
            return
        lines = [f"API 工具：{len(self.tools)} 个已启用"]
        lines.extend(
            f"{item.tool_name} — {'启用' if item.enabled else '停用'}" for item in self.definitions
        )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("api_test")
    async def test_tool(self, event: AstrMessageEvent):
        """实际调用：/api_test api_工具名 {JSON 参数}。修改接口会真实执行。"""
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result('用法：/api_test api_工具名 {"参数": "值"}；会实际执行接口')
            return
        tool = next((item for item in self.tools if item.name == parts[1]), None)
        if tool is None:
            yield event.plain_result("没有找到已启用的工具，请使用 /api_tools 查看")
            return
        try:
            arguments = json.loads(parts[2]) if len(parts) > 2 else {}
            if not isinstance(arguments, dict):
                raise ValueError
        except ValueError:
            yield event.plain_result("参数必须是 JSON 对象")
            return
        result = await tool.call(None, **arguments)
        yield event.plain_result(
            "\n".join(block.text for block in result.content if block.type == "text")
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("api_history")
    async def history(self, event: AstrMessageEvent):
        """查看最近 20 次调用的状态与耗时，不包含请求和响应内容。"""
        records = list(self.executor.history)[-20:] if self.executor else []
        yield event.plain_result(json.dumps(records, ensure_ascii=False))

    async def terminate(self):
        self.closed = True
        for tool in self.tools:
            tool.invalidate()
        self.context.registered_web_apis[:] = [
            route for route in self.context.registered_web_apis if route[1] not in self.web_handlers
        ]
        manager = self.context.get_llm_tool_manager()
        owned = {id(tool) for tool in self.tools}
        manager.func_list[:] = [tool for tool in manager.func_list if id(tool) not in owned]
        if self.executor:
            await self.executor.close()
        self.tools = []
