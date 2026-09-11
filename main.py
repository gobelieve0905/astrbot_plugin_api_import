"""AstrBot integration for administrator-defined HTTP tools."""

import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.tool import FunctionTool

from .definitions import DefinitionError, parse_definitions
from .engine import Executor


class ImportedTool(FunctionTool):
    def __init__(self, definition, executor):
        super().__init__(
            name=definition.tool_name,
            description=definition.description,
            parameters=definition.parameters,
        )
        self.definition = definition
        self.executor = executor

    async def call(self, context, **kwargs):
        return json.dumps(await self.executor.execute(self.definition, kwargs), ensure_ascii=False)


class ApiImportPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.definitions = []
        self.tools = []
        self.executor = None
        self.configuration_error = None

    async def initialize(self):
        try:
            definitions = parse_definitions(self.config.get("tools_json", "[]"))
            manager = self.context.get_llm_tool_manager()
            existing = {tool.name for tool in manager.func_list}
            if any(item.enabled and item.tool_name in existing for item in definitions):
                raise DefinitionError("存在与其他插件同名的工具，请修改接口 name")
        except DefinitionError as exc:
            self.configuration_error = str(exc)
            logger.error("API 工具配置未加载: " + self.configuration_error)
            return
        self.executor = Executor(StarTools.get_data_dir("astrbot_plugin_api_import") / "results")
        self.definitions = definitions
        self.tools = [ImportedTool(item, self.executor) for item in definitions if item.enabled]
        try:
            if self.tools:
                self.context.add_llm_tools(*self.tools)
        except Exception:
            await self.terminate()
            raise
        logger.info(f"API 工具接入已加载 {len(self.tools)} 个工具")

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
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("api_history")
    async def history(self, event: AstrMessageEvent):
        """查看最近 20 次调用的状态与耗时，不包含请求和响应内容。"""
        records = list(self.executor.history)[-20:] if self.executor else []
        yield event.plain_result(json.dumps(records, ensure_ascii=False))

    async def terminate(self):
        manager = self.context.get_llm_tool_manager()
        owned = {id(tool) for tool in self.tools}
        manager.func_list[:] = [tool for tool in manager.func_list if id(tool) not in owned]
        if self.executor:
            await self.executor.close()
        self.tools = []
