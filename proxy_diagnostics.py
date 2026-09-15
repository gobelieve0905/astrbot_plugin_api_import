"""Bounded, credential-free administrator probes through fixed proxy endpoints."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from .definitions import DefinitionError

TARGETS = {
    "latency": "https://www.gstatic.com/generate_204",
    "meta": "https://graph.facebook.com/",
}


class ProxyDiagnostics:
    def __init__(self, nodes):
        self.nodes = nodes
        self.slots = asyncio.Semaphore(3)
        self.running = set()
        self.results = {}
        self.last_started = {}
        self.refresh_lock = asyncio.Lock()

    async def probe(self, node_id, kind, revision):
        if kind not in TARGETS or not isinstance(node_id, str):
            raise DefinitionError("请选择节点和检测类型")
        state = self.nodes.snapshot()
        if revision != state["revision"]:
            raise DefinitionError("节点目录或选择已改变，请刷新后检测")
        nodes, _ = self.nodes._read()
        node = next((n for n in nodes if n["id"] == node_id), None)
        if node is None:
            raise DefinitionError("节点已不在目录中，请刷新")
        key = (node_id, kind)
        if (
            len(self.running) >= 3
            or key in self.running
            or time.monotonic() - self.last_started.get(key, -100) < 3
        ):
            raise DefinitionError("检测正在进行或过于频繁，请稍后重试")
        self.running.add(key)
        self.last_started[key] = time.monotonic()
        result = {"node_id": node_id, "kind": kind, "ok": False, "status": None}
        started = time.monotonic()
        try:
            async with self.slots, asyncio.timeout(12):
                async with httpx.AsyncClient(
                    proxy=node["url"], trust_env=False, follow_redirects=False, timeout=10.0
                ) as client:
                    async with client.stream("GET", TARGETS[kind]) as response:
                        result["status"] = response.status_code
                        # A Graph 400/401 proves HTTPS reachability, not business authorization.
                        result["ok"] = (
                            response.status_code == 204
                            if kind == "latency"
                            else response.status_code in (200, 400, 401, 403, 404, 405)
                        )
                        result["message"] = (
                            (
                                "HTTPS 可达（未验证 Token/业务权限）"
                                if kind == "meta"
                                else "测速成功"
                            )
                            if result["ok"]
                            else "已收到 HTTP 响应，请检查状态码"
                        )
        except (TimeoutError, httpx.TimeoutException):
            result["message"] = "连接或响应超时"
        except httpx.ProxyError:
            result["message"] = "代理隧道建立失败"
        except httpx.ConnectError:
            result["message"] = "连接或 TLS 失败"
        except (httpx.HTTPError, OSError, ValueError):
            result["message"] = "网络请求失败"
        finally:
            self.running.discard(key)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        if self.nodes.snapshot()["revision"] != revision:
            raise DefinitionError("检测期间节点目录或选择已变化，结果已丢弃，请刷新")
        self.results[key] = result
        valid = {n["id"] for n in state["nodes"]}
        self.results = {k: v for k, v in self.results.items() if k[0] in valid}
        return result

    async def refresh(self):
        if self.refresh_lock.locked() or self.running:
            raise DefinitionError("已有刷新或检测在进行，请稍后重试")
        async with self.refresh_lock:
            reader = writer = None
            try:
                async with asyncio.timeout(180):
                    reader, writer = await asyncio.open_unix_connection(
                        str(self.nodes.path.parent / "proxy-admin.sock")
                    )
                    writer.write(b'{"action":"refresh"}\n')
                    await writer.drain()
                    reply = json.loads(await reader.readline())
                    if not reply.get("ok"):
                        raise DefinitionError(reply.get("message", "订阅刷新失败，原目录保留"))
                self.results.clear()
                return self.nodes.snapshot()
            except (OSError, TimeoutError, ValueError):
                raise DefinitionError("订阅刷新服务不可用或超时，请稍后刷新目录确认状态") from None
            finally:
                if writer:
                    writer.close()
                    await writer.wait_closed()
