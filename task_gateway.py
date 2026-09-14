"""Versioned local RPC gateway; no imports or configuration sharing with consumers."""

import asyncio
import json
import os
import re
import secrets
import socket
import struct
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

LIMIT = 12 * 1024 * 1024


class GatewayError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def redact(value, request):
    hidden = set()
    sensitive = re.compile(r"key|token|secret|password|authorization|cookie|credential", re.I)

    def collect(item, protected=False):
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, protected or bool(sensitive.search(key)) or key == "headers")
        elif isinstance(item, list):
            for child in item:
                collect(child, protected)
        elif protected and isinstance(item, str) and item:
            hidden.add(item)
            if item.lower().startswith("bearer "):
                hidden.add(item[7:])

    collect(request)
    url = urlsplit(request.get("url", ""))
    for key, query_value in parse_qsl(url.query):
        if sensitive.search(key) and query_value:
            hidden.add(query_value)
    for credential_part in (url.username, url.password):
        if credential_part:
            hidden.add(credential_part)
    hidden |= {quote(v, safe="") for v in hidden} | {quote_plus(v) for v in hidden}

    response_sensitive = re.compile(
        r"^(?:key|token|(?:access|refresh)[_-]?token|api[_-]?key|secret|client[_-]?secret|"
        r"password|authorization|cookie|credentials?)$",
        re.I,
    )

    def clean(item):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if response_sensitive.fullmatch(key) else clean(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, str):
            for secret in sorted(hidden, key=len, reverse=True):
                item = item.replace(secret, "[REDACTED]")
        return item

    result = clean(value)
    if isinstance(result, dict):
        result.pop("file", None)
        result.pop("file_scope", None)
    return result


class TaskGateway:
    def __init__(self, plugin):
        self.plugin = plugin
        self.server = None
        self.clients = set()
        self.path = None

    def live(self, name):
        return next(
            (t for t in self.plugin.tools if t.name == name and t.active and t.available), None
        )

    def inspect(self, payload):
        """Metadata/schema checks only. Never grant credentials or execute requests."""
        name = payload.get("tool")
        if name is None:
            query = payload.get("query", "")
            offset = payload.get("offset", 0)
            if (
                not isinstance(query, str)
                or len(query) > 200
                or type(offset) is not int
                or offset < 0
            ):
                raise ValueError("搜索参数无效")
            matches = [
                t
                for t in self.plugin.tools
                if self.live(t.name) is t
                and query.casefold() in (t.name + " " + t.description).casefold()
            ]
            return {
                "ok": True,
                "total": len(matches),
                "next_offset": offset + 20 if offset + 20 < len(matches) else None,
                "items": [
                    redact(
                        {"name": t.name, "description": t.description[:240]}, t.definition.request
                    )
                    for t in matches[offset : offset + 20]
                ],
            }
        if not isinstance(name, str) or not (tool := self.live(name)):
            raise ValueError("工具不存在或未启用；请搜索当前可用工具，不要创建名称别名")
        result = {
            "ok": True,
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "response_contract": "成功响应 data 为供应商原始数据；page 为可用时的分页元数据。失败检查 ok/error。保存日志每行 response 才是响应。",
            "scope_contract": "constraints 是顶层参数等值约束；空对象不固定参数，但不开放后台关闭的操作。",
        }
        if "arguments" in payload:
            from jsonschema import Draft202012Validator

            arguments, constraints = payload["arguments"], payload.get("constraints", {})
            if not isinstance(arguments, dict) or not isinstance(constraints, dict):
                raise ValueError("参数与约束必须为对象")
            errors = [
                {"path": list(e.absolute_path), "rule": e.validator}
                for e in Draft202012Validator(tool.parameters).iter_errors(arguments)
            ]
            errors.extend(
                {"path": [key], "rule": "fixed_constraint"}
                for key, value in constraints.items()
                if key not in arguments or arguments[key] != value
            )
            result.update(
                valid=not errors,
                errors=errors[:20],
                checked="schema_and_fixed_constraints_only",
                notice="未发送请求、未授予权限；平台字段规则、凭据、资源权限及当前后台状态仍在正式执行时校验。",
            )
        return redact(result, tool.definition.request)

    async def start(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self.handle, str(self.path), limit=LIMIT)
        self.path.chmod(0o600)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.clients):
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        if self.path:
            self.path.unlink(missing_ok=True)

    async def handle(self, reader, writer):
        current = asyncio.current_task()
        if len(self.clients) >= 16:
            writer.close()
            return
        self.clients.add(current)
        credential = None
        scopes = {}
        deadline = 0
        remaining = 0
        try:
            sock = writer.get_extra_info("socket")
            if hasattr(socket, "SO_PEERCRED"):
                _, uid, _ = struct.unpack(
                    "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    return
            async with asyncio.timeout(610):
                while line := await asyncio.wait_for(reader.readline(), 605):
                    try:
                        payload = json.loads(line)
                        if not isinstance(payload, dict):
                            raise ValueError("网关请求必须是对象")
                        action = payload.get("action")
                        if self.plugin.closed:
                            raise ValueError("API 插件已关闭")
                        if action == "inspect":
                            result = self.inspect(payload)
                        elif action == "issue" and credential is None:
                            requested = payload.get("scopes", {})
                            if not isinstance(requested, dict) or not 1 <= len(requested) <= 100:
                                raise ValueError("任务须指定 1–100 个操作及参数约束")
                            for name, constraints in requested.items():
                                if not self.live(name) or not isinstance(constraints, dict):
                                    raise ValueError("请求包含未启用操作或无效约束")
                                if any(not isinstance(k, str) for k in constraints):
                                    raise ValueError("无效参数约束")
                            ttl, quota = payload.get("ttl", 120), payload.get("quota", 50)
                            if type(ttl) is not int or not 1 <= ttl <= 600:
                                raise ValueError("任务有效期必须为 1–600 秒")
                            ceiling = (
                                self.plugin.config.get("task_max_calls", 200)
                                if hasattr(self.plugin, "config")
                                else 200
                            )
                            if type(ceiling) is not int or not 1 <= ceiling <= 10000:
                                raise GatewayError("POLICY_INVALID", "管理员任务额度配置无效")
                            if type(quota) is not int or not 1 <= quota <= ceiling:
                                raise GatewayError(
                                    "QUOTA_POLICY", f"请求额度超过管理员上限 {ceiling}"
                                )
                            scopes = requested
                            # Bind to current tool objects; edits revoke existing grants.
                            granted = {name: self.live(name) for name in scopes}
                            credential = secrets.token_urlsafe(32)
                            deadline, remaining = time.monotonic() + ttl, quota
                            result = {
                                "ok": True,
                                "credential": credential,
                                "ttl": ttl,
                                "quota": quota,
                                "tools": [
                                    {
                                        "name": name,
                                        "description": granted[name].description,
                                        "parameters": granted[name].parameters,
                                        "constraints": scopes[name],
                                    }
                                    for name in scopes
                                ],
                            }
                        elif action == "call":
                            if not credential or not secrets.compare_digest(
                                str(payload.get("credential", "")), credential
                            ):
                                raise ValueError("任务凭证无效")
                            if time.monotonic() >= deadline:
                                raise GatewayError("TASK_EXPIRED", "任务已过期")
                            if remaining <= 0:
                                raise GatewayError(
                                    "QUOTA_EXHAUSTED", "调用额度耗尽，请保存进度后创建新的授权任务"
                                )
                            remaining -= 1  # Failed attempts also consume quota; no implicit retry.
                            name, arguments = payload.get("tool"), payload.get("arguments")
                            if name not in scopes or not isinstance(arguments, dict):
                                raise ValueError("操作不在任务范围或参数无效")
                            tool = granted[name]
                            if self.live(name) is not tool:
                                raise GatewayError(
                                    "PERMISSION_REVOKED", "操作已停用或更新，请创建新任务"
                                )
                            if any(
                                key not in arguments or arguments[key] != value
                                for key, value in scopes[name].items()
                            ):
                                raise ValueError("参数超出任务授权范围")
                            execution = asyncio.create_task(
                                tool.execute(
                                    arguments,
                                    full_result=True,
                                    extra_guard=lambda expires=deadline: time.monotonic() < expires,
                                )
                            )
                            disconnected = asyncio.create_task(reader.read(1))
                            try:
                                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                                    await asyncio.wait(
                                        (execution, disconnected),
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )
                                    if disconnected.done():
                                        # RPC is strictly serial: EOF or pipelining revokes the task.
                                        raise ConnectionError("Task channel closed")
                                    result = execution.result()
                            finally:
                                execution.cancel()
                                disconnected.cancel()
                                await asyncio.gather(
                                    execution, disconnected, return_exceptions=True
                                )
                            if self.live(name) is not tool or time.monotonic() >= deadline:
                                raise ValueError("请求期间权限已撤销或任务过期")
                            result = redact(result, tool.definition.request)
                            result["remaining"] = remaining
                        elif action == "revoke":
                            break
                        else:
                            raise ValueError("不支持的网关操作")
                    except (ValueError, TypeError, KeyError) as exc:
                        result = {
                            "ok": False,
                            "error_code": getattr(exc, "code", "GATEWAY_REJECTED"),
                            "remaining": remaining,
                            "error": str(exc)[:300]
                            if isinstance(exc, ValueError)
                            and not isinstance(exc, json.JSONDecodeError)
                            else "网关参数无效",
                        }
                    writer.write(json.dumps(result, ensure_ascii=True).encode() + b"\n")
                    await writer.drain()
        except (ConnectionError, TimeoutError, ValueError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(current)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
