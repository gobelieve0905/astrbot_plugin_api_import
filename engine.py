"""Async HTTP execution, typed parameter mapping and bounded result handling."""

import asyncio
import copy
import json
import re
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote, quote_plus

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


def sanitize_error(text, request):
    """Bound untrusted diagnostics and remove configured credentials before model delivery."""
    secrets = []
    sensitive = re.compile(r"(?i)(key|token|secret|password|authorization|cookie|credential)")

    def collect(value, protected=False):
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, protected or bool(sensitive.search(key)))
        elif isinstance(value, list):
            for child in value:
                collect(child, protected)
        elif protected and isinstance(value, str) and value:
            secrets.extend(
                [value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]]
            )
            if value.lower().startswith("bearer ") and value[7:]:
                bare = value[7:]
                secrets.extend([bare, quote(bare, safe=""), quote_plus(bare)])

    collect(request)
    collect(request.get("headers", {}), True)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"https?://[^\s<>]+", "[URL REDACTED]", text)
    text = re.sub(r'(?i)(bearer\s+)[^\s"<>]+', r"\1[REDACTED]", text)
    text = re.sub(
        r'(?i)((?:api[_-]?key|token|secret|password|authorization|cookie)["\s]*[:=]["\s]*)[^\s,"<>;&]+',
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(text.split())[:2000] or "上游未提供可展示的错误详情"


class Executor:
    def __init__(self, data_dir: Path, client=None, *, meta_proxy=None, meta_proxy_resolver=None):
        self.data_dir = Path(data_dir)
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.meta_client = (
            httpx.AsyncClient(proxy=meta_proxy, follow_redirects=False, trust_env=False)
            if meta_proxy
            else None
        )
        self.meta_proxy_resolver = meta_proxy_resolver
        self.fixed_proxy_clients = {}
        self.history = deque(maxlen=50)
        self.semaphore = asyncio.Semaphore(4)
        self.closed = False
        self.permitted = lambda connection_id, operation: False

    async def close(self):
        self.closed = True
        await self.client.aclose()
        if self.meta_client is not None:
            await self.meta_client.aclose()
        for client in self.fixed_proxy_clients.values():
            await client.aclose()

    async def execute(self, definition: Definition, arguments: dict, guard=lambda: True) -> dict:
        started = time.monotonic()
        result = {"ok": False, "tool": definition.tool_name}
        try:
            if self.closed or not definition.enabled or not guard():
                raise ExecutionError("工具已停用或插件已卸载")
            from .platforms import normalize_advertiser_arguments

            arguments = normalize_advertiser_arguments(definition, copy.deepcopy(arguments))
            for key, schema in definition.parameters.get("properties", {}).items():
                if key not in arguments and isinstance(schema, dict) and "default" in schema:
                    arguments[key] = copy.deepcopy(schema["default"])
            errors = list(Draft202012Validator(definition.parameters).iter_errors(arguments))
            if errors:
                path = ".".join(str(x) for x in errors[0].absolute_path) or "参数"
                error = errors[0]
                if error.validator == "additionalProperties":
                    allowed = ", ".join(definition.parameters.get("properties", {}))
                    raise ExecutionError(
                        "存在未定义参数；请严格使用工具声明的参数名称。允许的顶层参数："
                        + allowed[:1800]
                    )
                expected = (
                    error.schema.get("type", "声明类型")
                    if isinstance(error.schema, dict)
                    else "声明类型"
                )
                raise ExecutionError(
                    f"{path} 不符合 {error.validator} 规则；期望 {expected}，数组请传 JSON 数组而不是字符串"
                )
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
            is_meta = definition.connection.get("platform") == "meta_marketing"
            if is_meta:
                from .meta_platform import prepare

                url, kwargs = prepare(definition, arguments, self.permitted)
            # No retries: a failed connection may still have caused a write on the remote service.
            async with self.semaphore:
                if self.closed or not guard():
                    raise ExecutionError("工具已更新、停用或插件已卸载，请重新选择工具")
                if is_meta:
                    url, kwargs = prepare(definition, arguments, self.permitted)
                async with asyncio.timeout(request.get("timeout", 30)):
                    client = (
                        self.meta_client
                        if is_meta and self.meta_client is not None
                        else self.client
                    )
                    if is_meta and self.meta_proxy_resolver is not None:
                        proxy = self.meta_proxy_resolver()
                        if proxy not in self.fixed_proxy_clients:
                            self.fixed_proxy_clients[proxy] = httpx.AsyncClient(
                                proxy=proxy, follow_redirects=False, trust_env=False
                            )
                        client = self.fixed_proxy_clients[proxy]
                    async with client.stream(
                        request["method"],
                        url,
                        **kwargs,
                        timeout=request.get("timeout", 30),
                        follow_redirects=False,
                    ) as response:
                        result["status"] = response.status_code
                        if not 200 <= response.status_code < 300:
                            result["error"] = (
                                "HTTP 请求失败；未自动重试。请依据 error_detail 修正请求，不要猜测平台维护、账户状态或数据是否存在。"
                            )
                            chunks, size = [], 0
                            async for chunk in response.aiter_bytes():
                                chunks.append(chunk[: max(0, 8192 - size)])
                                size += len(chunk)
                                if size >= 8192:
                                    break
                            detail = b"".join(chunks).decode("utf-8", errors="replace")
                            result["error_detail"] = sanitize_error(detail, request)
                            result["error_detail_truncated"] = size >= 8192 or len(detail) > 2000
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
            if is_meta:
                from .meta_platform import redact_response

                raw_paging = data.get("paging", {}) if isinstance(data, dict) else {}
                has_more = bool(raw_paging.get("next")) if isinstance(raw_paging, dict) else False
                data = redact_response(data, request["headers"]["Authorization"][7:])
                if isinstance(data, dict) and data.get("error"):
                    result.update(
                        error="Meta 返回业务错误；未自动重试",
                        error_detail=sanitize_error(
                            json.dumps(data["error"], ensure_ascii=False), request
                        ),
                    )
                    return result
                if isinstance(data, dict) and data.get("paging"):
                    result["pagination"] = (
                        "返回单页；使用 paging.cursors.after 在相同操作中继续，不接受 next URL。"
                    )
            try:
                selected = extract(data, definition.response.get("pointer", ""))
            except (KeyError, IndexError, ValueError) as exc:
                raise ExecutionError("响应中不存在配置的 JSON Pointer 路径") from exc
            serialized = json.dumps(selected, ensure_ascii=False)
            limit = definition.response.get("preview_chars", 4000)
            if is_meta and definition.connection.get("operation") == "get_adaccounts":
                # Account discovery is compact and must not silently hide later accounts.
                limit = max(limit, 64000)
            result.update(ok=True, bytes=size, truncated=len(serialized) > limit)
            if is_meta and isinstance(data, dict) and isinstance(data.get("data"), list):
                paging = data.get("paging", {})
                cursors = paging.get("cursors", {}) if isinstance(paging, dict) else {}
                rows = len(data["data"])
                result["page"] = {
                    "row_count": rows,
                    "has_more": has_more,
                    "after": cursors.get("after") if has_more else None,
                    "response_complete": not result["truncated"],
                    "scope": "仅当前对象的本页，不代表所有账户或全部分页",
                }
                if result["truncated"]:
                    result["page"]["retry_current_page_limit"] = max(
                        1, min(rows - 1, int(rows * limit / len(serialized) / 2))
                    )
                    result["page"]["next_step"] = (
                        "当前预览不完整：保持当前 node_id、筛选及输入 after，缩小 params.limit 重取本页；不要跳到下一页。"
                    )
                elif has_more:
                    result["page"]["next_step"] = (
                        "保持当前对象与查询条件，用 page.after 作为 params.after 继续查询。"
                    )
                else:
                    result["page"]["next_step"] = (
                        "本页完整且没有下一页；仍需核对其他相关账户与项目关联，不能以账户名称排除其他账户。"
                    )
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
        except (httpx.HTTPError, TimeoutError) as exc:
            result["error"] = "网络请求失败或超时；未自动重试，写入操作结果可能未知"
            result["network_error_type"] = type(exc).__name__
            result["network_route"] = (
                "configured_proxy"
                if definition.connection.get("platform") == "meta_marketing"
                and (self.meta_client is not None or self.meta_proxy_resolver is not None)
                else "direct"
            )
            result["network_hint"] = (
                "请检查服务器网络和代理路由；网络失败不代表账号 ID、Token 或后台操作权限无效。"
            )
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
