# 开发与验收

- `definitions.py`：接口定义校验，与平台无关。
- `engine.py`：独立的请求执行与结果处理，不依赖 AstrBot。
- `main.py`：工具注册和管理员命令，生命周期清理。

本地：`uv venv .venv`，`uv pip install --python .venv/bin/python -r requirements.txt ruff`。
检查：`.venv/bin/ruff check .`、`.venv/bin/ruff format --check .`、`.venv/bin/python -B -m unittest discover -s tests -v`。
真实框架隔离检查：在运行镜像的无网络容器内执行 `tests/integration_offline.py`，不启动平台适配器或发送消息。

开发在 develop，中文提交；检查通过后明确推送 develop，再按既有运维入口对固定 SHA 备份、加部署锁、隔离验证和热加载。main 仅保留初始骨架，等待用户验收决定合并；所有标签由用户手工管理。开发文档和测试通过 export-ignore 从源码安装归档排除。

## 2026-09-11 验证

本地 15 项单元测试覆盖参数拒绝、映射、编码、HTTP 错误/重定向、结果上限、文件保存与超时。AstrBot 4.28.0 镜像的无网络检查覆盖真实 FunctionTool 构造与注册、调用、管理员命令方法、卸载、重载、停用及名称冲突。服务器部署另行验证固定提交的归档和运行状态，不自动请求业务 API。

## 0.0.1 页面管理

`catalog.py` 管理版本校验与 CRUD，`importing.py` 仅解析 cURL 文本；`pages/manage/` 是 AstrBot 原生插件页面，通过官方 bridge 调用后端。原 tools_json 是唯一持久化来源，不迁移、不清空。后端在无 await 的切换区间内构造新工具、原子保存配置并替换注册；异常恢复原工具和内存配置。页面可编辑范围外的 schema 保留 JSON 入口。

浏览器检查单独运行：安装开发依赖 playwright 后执行 `.venv/bin/python -B tests/browser_check.py`。它启动一次性本地测试目录和独立无账号浏览器，不访问业务接口或生产飞书。普通 unittest 与生产插件依赖不要求 playwright。

本轮验证：29 项本地单元测试通过；真实 AstrBot 4.28.0 无网络检查覆盖页面请求上下文、配置持久化、冲突拒绝、写盘失败恢复、工具即时替换与删除。浏览器测试通过表单、JSON、cURL、编辑、启停、删除、搜索、重复名称错误、参数约束保留、深浅色及 390px 窄屏布局。开发页面与 i18n 为运行时文件，包含在安装 ZIP；测试和开发文档继续排除。

## 0.1.1 平台快速接入

`platforms.py` 维护平台目录与后端工具定义。GET `platforms` 只返回无凭据的操作信息；POST `connect-platform` 接收 platform_id、token、enabled_operations 和 revision，使用服务器预设生成通用定义，经 catalog 原子保存。新增平台可扩展该目录与构造器，不依赖模型或远端文档。自动发现和文档模型模块、端点及专用测试已删除。

每次接入生成随机编号，六个操作分别成为普通工具，复用原有 CRUD 和权限保存；不增加第二份配置。新工具默认停用，已保存的旧工具仍可加载。Report Key 不进入工具参数 schema，不允许调用参数覆盖固定 api_key 或 report_type。

测试覆盖六条请求映射、默认列的逗号序列化、分页、常用筛选、业务状态保留、未知平台/开关拒绝、过期 revision、多个账号和默认停用。浏览器覆盖只填 Token、独立开关、权限更新、手动 CRUD 和窄屏；真实 AstrBot 无网络测试覆盖工具注册、旧引用失效、持久化及失败恢复。生产验证只读检查页面、平台目录、插件加载和服务健康，不调用报表。

本轮本地 33 项单元测试、ruff 和 JavaScript 语法检查通过；浏览器检查通过平台接入与原有管理流程。服务器隔离集成与部署结果另存既有运维归档。

## 0.1.2 接入账户

通用定义增加可选 display_name 和 connection 元数据，仍使用 tools_json 单一持久化来源。定义校验要求同一 connection.id 的名称、平台一致且 operation 不重复；生成模型说明时附上账户名称及显示名称，不改内部工具名。catalog 通过 connections.py 保守识别旧 AppLovin 标准预设，以编号和凭据一致性归组，snapshot 只做内存副本，首次保存才写元数据。

update-connection 和 delete-connection 复用 edit_lock、revision 和原子替换，仅修改指定账户。更换凭据前验证所有目标仍是对应预设地址，任何一项失败不写配置。空 Token 保留原值。页面账户卡片默认折叠，独立模态框修改账户名称、Token、权限；已有手动表单新增显示名称。

本地 40 项单元测试通过，包括双账户隔离、旧配置兼容、只读不改配置、Token 留空保留、跨账户权限拒绝、旧 revision、目标地址变更时拒绝批量换 Token。浏览器覆盖双账户创建、账户和操作改名、统一换 Key、搜索、单账户删除、桌面/深色/窄屏及旧管理流程；真实框架测试增加改名后的工具说明和旧引用失效验证。
