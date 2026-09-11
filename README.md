# API 工具接入

将 HTTP API 定义为 AstrBot 可调用的工具。无需 MCP，不内置平台配置包；查询和修改接口采用同一种定义。

开发版本，已针对 AstrBot 4.28.0 设计验证；需要支持工具调用的聊天模型。配置为空时不注册任何 API。

## 开始使用

1. 在 AstrBot 插件管理中从本仓库安装插件。当前开发代码在 `develop`，`main` 尚未发布功能版本。
2. 打开插件配置，将 [examples/tools.json](examples/tools.json) 的 JSON 数组复制到「API 工具定义」。
3. 替换示例地址、参数与结果路径，并将需要使用的接口 `enabled` 改为 `true`。示例地址不可直接使用。
4. 保存配置并重载插件，使用 `/api_tools` 检查状态。
5. 对话中描述需求，模型可调用 `api_` 前缀的工具；人格的工具白名单需要允许这些工具。

修改、删除或停用工具均通过编辑配置并重载完成。整个数组先校验后注册，任何定义错误都不注册本插件的 API；用 `/api_tools` 查看错误位置。不要在聊天中粘贴敏感请求头。

## 最小定义

```json
[
  {
    "name": "list_items",
    "description": "查询条目列表，可指定数量。",
    "parameters": {
      "type": "object",
      "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
      }
    },
    "request": {
      "method": "GET",
      "url": "https://api.example.com/items",
      "query": {"limit": {"$param": "limit"}}
    },
    "response": {"pointer": "/data", "save": true}
  }
]
```

## 接口定义

| 字段 | 说明 |
| --- | --- |
| `name` | 唯一名称，字母开头，允许字母、数字、下划线，最多 48 字符；实际工具名为 `api_名称` |
| `description` | 提供给模型的用途说明；修改类工具应明确描述副作用 |
| `enabled` | 默认 `true`；`false` 时不注册工具，也不能试调用 |
| `parameters` | 对象类型 JSON Schema；支持类型、必填、枚举、嵌套结构等，默认拒绝额外的顶层参数；暂不支持 schema 引用 |
| `request.method` | GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONS |
| `request.url` | 固定 HTTP(S) URL，路径允许 `{参数名}`；路径参数必须声明为必填，值会编码 |
| `request.query` | 查询参数对象；与 URL 中已有查询参数合并，同名配置值优先 |
| `request.headers` | 静态或参数映射的请求头，不提供专门的凭据管理功能 |
| `request.json` | JSON 请求体，保留布尔、数字、数组、对象等类型 |
| `request.form` | 表单请求体，与 `json` 二选一；标量数组采用重复字段 |
| `request.timeout` | 单次执行超时秒数，默认 30，范围 1–120 |
| `response.pointer` | JSON Pointer，如 `/data/items`、`/items/0`；默认返回整个响应；键中的 `/` 写为 `~1`，`~` 写为 `~0` |
| `response.preview_chars` | 返回模型的数据预览字符数，默认 4000，范围 100–20000 |
| `response.max_bytes` | 解压后响应上限，默认 5 MiB，最大 20 MiB；超限明确失败 |
| `response.save` | 默认 `false`；为 `true` 时保存完整单次响应为本地 JSON 文件 |

动态值使用单独的对象 `{"$param":"参数名"}`，可嵌套在 JSON 对象、数组中。普通字符串原样传递，不执行表达式或字符串插值。可选参数缺省时省略相应对象字段；顶层参数的 `default` 会在调用前填入。数组中的缺省引用会报错，不悄悄改变数组位置。JSON `null` 可作为字段值或整个 JSON 请求体传递。

查询和表单支持标量与标量数组；请求头只支持标量。插件不自动跟随重定向、不自动重试请求。HTTP 2xx 表示传输成功，不代表平台业务必然成功；业务状态会作为返回数据交给调用者判断。配置的结果路径不存在时明确报错。

## 管理员命令

- `/api_tools`：查看加载状态与工具启停状态。
- `/api_test api_list_items {"limit": 5}`：实际调用已启用工具，支持含空格的 JSON。**修改接口会真实修改远端数据。**
- `/api_history`：查看最近 20 次执行的工具名、HTTP 状态、成功标记和耗时。

试调用结果与模型调用结果相同。管理命令使用 AstrBot 管理员权限；工具可用范围由 AstrBot 的工具配置管理，本版没有额外的用户权限体系。

## 数据与限制

完整结果位于 `data/plugin_data/astrbot_plugin_api_import/results/`，插件更新不覆盖。返回的文件路径是服务器本地路径，不是下载链接，也不会自动向聊天发送附件。非 JSON 响应会作为字符串保存在 JSON 文件中。文件当前不自动清理，启用保存后需按需整理；调用记录仅在内存保留最近 50 次，重载后清空，不保存请求或响应正文。

本版完成配置、注册、HTTP 执行、结果提取、保存和试调用闭环。尚不包含 cURL/OpenAPI 导入、自动分页、异步报表轮询、文件上传、工作流或独立可视化编辑页面。返回的数据仅代表一次响应，不承诺已拉完全部分页。

开发和验收说明见 [开发文档](https://github.com/gobelieve0905/astrbot_plugin_api_import/blob/develop/docs/DEVELOPMENT.md)。
