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
