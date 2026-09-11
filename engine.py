"""Async HTTP execution, typed parameter mapping and bounded result handling."""

import asyncio
import copy
import json
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator

from .definitions import PLACEHOLDER, Definition

MISSING = object()


class ExecutionError(ValueError):
    """A safe, user-facing execution error."""


def resolve(value, arguments):
    if isinstance(value, dict):
        if "$param" in value:
            resolved = arguments.get(value["$param"], MISSING)
            if "$join" in value and resolved is not MISSING:
                if not isinstance(resolved, list) or any(
                    isinstance(x, (dict, list)) or x is None for x in resolved
                ):
                    raise ExecutionError("需要标量数组才能按文档拼接参数")
                return value["$join"].join(
                    str(x).lower() if isinstance(x, bool) else str(x) for x in resolved
                )
            return resolved
        result = {}
        for key, child in value.items():
            resolved = resolve(child, arguments)
            if resolved is not MISSING:
                result[key] = resolved
        return result
    if isinstance(value, list):
        result = [resolve(child, arguments) for child in value]
        if any(child is MISSING for child in result):
            raise ExecutionError("数组模板引用的可选参数未提供，请提供参数或默认值")
        return result
    return value


def extract(value, pointer):
    if not pointer:
        return value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ExecutionError("结果路径的数组下标无效")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ExecutionError("结果路径不能继续提取")
    return value


def wire_mapping(value, headers=False):
    result = {}
    for key, item in value.items():
        if isinstance(item, dict) or (headers and isinstance(item, list)):
            raise ExecutionError("请求头需要标量，查询/表单值只支持标量或标量数组")
        items = item if isinstance(item, list) else [item]
        if any(isinstance(x, (dict, list)) for x in items):
            raise ExecutionError("查询/表单数组只支持标量")
        converted = [
            "" if x is None else str(x).lower() if isinstance(x, bool) else str(x) for x in items
        ]
        result[key] = converted if isinstance(item, list) else converted[0]
    return result


class Executor:
    def __init__(self, data_dir: Path, client=None):
        self.data_dir = Path(data_dir)
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.history = deque(maxlen=50)
        self.semaphore = asyncio.Semaphore(4)
        self.closed = False

    async def close(self):
        self.closed = True
        await self.client.aclose()

    async def execute(self, definition: Definition, arguments: dict) -> dict:
        started = time.monotonic()
        result = {"ok": False, "tool": definition.tool_name}
        try:
            if self.closed or not definition.enabled:
                raise ExecutionError("工具已停用或插件已卸载")
            arguments = copy.deepcopy(arguments)
            for key, schema in definition.parameters.get("properties", {}).items():
                if key not in arguments and isinstance(schema, dict) and "default" in schema:
                    arguments[key] = copy.deepcopy(schema["default"])
            errors = list(Draft202012Validator(definition.parameters).iter_errors(arguments))
            if errors:
                path = ".".join(str(x) for x in errors[0].absolute_path) or "参数"
                raise ExecutionError(f"{path} 不符合 {errors[0].validator} 规则")
            request = definition.request

            def path_value(match):
                value = arguments[match.group(1)]
                if value is None or isinstance(value, (dict, list, bool)):
                    raise ExecutionError("URL 路径参数必须是字符串或数字")
                text = str(value)
                if not text or text in (".", ".."):
                    raise ExecutionError("URL 路径参数不能为空或点路径")
                return quote(text, safe="")

            url = PLACEHOLDER.sub(path_value, request["url"])
            kwargs = {
                "params": wire_mapping(resolve(request["query"], arguments))
                if "query" in request
                else None,
                "headers": wire_mapping(resolve(request.get("headers", {}), arguments), True),
            }
            # httpx params replace an existing query; preserve the configured URL query as well.
            if kwargs["params"] is not None:
                kwargs["params"] = httpx.QueryParams(httpx.URL(url).query).merge(kwargs["params"])
            for field, target in (("json", "json"), ("form", "data")):
                if field in request:
                    body = resolve(request[field], arguments)
                    if body is MISSING:
                        continue  # Optional whole-body parameter absent: send no body.
                    if field == "form":
                        kwargs[target] = wire_mapping(body)
                    else:
                        kwargs["content"] = json.dumps(
                            body, ensure_ascii=False, allow_nan=False
                        ).encode("utf-8")
                        headers = httpx.Headers(kwargs["headers"])
                        if "content-type" not in headers:
                            headers["content-type"] = "application/json"
                        kwargs["headers"] = headers
            # No retries: a failed connection may still have caused a write on the remote service.
            async with self.semaphore:
                async with asyncio.timeout(request.get("timeout", 30)):
                    async with self.client.stream(
                        request["method"],
                        url,
                        **kwargs,
                        timeout=request.get("timeout", 30),
                        follow_redirects=False,
                    ) as response:
                        result["status"] = response.status_code
                        if not 200 <= response.status_code < 300:
                            result["error"] = "HTTP 请求失败；未自动重试，请通过平台确认操作状态"
                            return result
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > definition.response.get("max_bytes", 5_242_880):
                                raise ExecutionError("响应超过 max_bytes 限制；未保存完整结果")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        text = raw.decode(response.encoding or "utf-8", errors="replace")
            if raw:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = text
            else:
                data = None
            try:
                selected = extract(data, definition.response.get("pointer", ""))
            except (KeyError, IndexError, ValueError) as exc:
                raise ExecutionError("响应中不存在配置的 JSON Pointer 路径") from exc
            serialized = json.dumps(selected, ensure_ascii=False)
            limit = definition.response.get("preview_chars", 4000)
            result.update(ok=True, bytes=size, truncated=len(serialized) > limit)
            if len(serialized) > limit:
                result["preview"] = serialized[:limit]
            else:
                result["data"] = selected
            if definition.response.get("save", False):
                self.data_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{definition.name}-{uuid.uuid4().hex}.json"
                path = self.data_dir / filename
                await asyncio.to_thread(
                    path.write_text, json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
                result["file"] = str(path.resolve())
                result["file_scope"] = "完整单次响应（未自动分页）；服务器本地 JSON 文件"
        except (httpx.HTTPError, TimeoutError):
            result["error"] = "网络请求失败或超时；未自动重试，写入操作结果可能未知"
        except ExecutionError as exc:
            result["error"] = str(exc)
        except (ValueError, TypeError, LookupError):
            result.update(ok=False, error="请求参数、编码或响应格式无效")
        except OSError:
            result.update(ok=False, error="结果文件保存失败；远程请求可能已经成功")
        finally:
            self.history.append(
                {
                    "tool": definition.tool_name,
                    "ok": result["ok"],
                    "status": result.get("status"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        return result
