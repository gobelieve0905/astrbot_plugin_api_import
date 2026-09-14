"""Administrator-provisioned, immutable fixed proxy endpoints; never model-selectable."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .definitions import DefinitionError


class FixedProxyNodes:
    def __init__(self, path: Path, config):
        self.path = path
        self.config = config

    def _read(self):
        if not self.path.exists():
            return [], b""
        raw = self.path.read_bytes()
        if len(raw) > 131072:
            raise DefinitionError("固定代理节点目录过大，请联系管理员")

        def require(condition):
            if not condition:
                raise ValueError()

        try:
            nodes = json.loads(raw)
            require(isinstance(nodes, list) and len(nodes) <= 64)
            ids, urls = set(), set()
            for node in nodes:
                require(set(node) == {"id", "name", "url"})
                require(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", node["id"]))
                require(isinstance(node["name"], str) and 0 < len(node["name"]) <= 120)
                address = urlsplit(node["url"])
                require(address.scheme in ("http", "https") and address.hostname)
                require(not address.username and not address.password)
                require(address.path in ("", "/") and not address.query and not address.fragment)
                require(address.port)
                require(node["id"] not in ids and node["url"] not in urls)
                ids.add(node["id"])
                urls.add(node["url"])
            return nodes, raw
        except (AssertionError, ValueError, KeyError, TypeError):
            raise DefinitionError("固定代理节点目录无效，请联系管理员") from None

    def snapshot(self):
        nodes, raw = self._read()
        selected = self.config.get("meta_proxy_node", "")
        return {
            "nodes": [{"id": n["id"], "name": n["name"]} for n in nodes],
            "selected": selected,
            "ready": any(n["id"] == selected for n in nodes),
            "revision": hashlib.sha256(raw + str(selected).encode()).hexdigest(),
        }

    def resolve(self):
        from .engine import ExecutionError

        try:
            nodes, _ = self._read()
            selected = self.config.get("meta_proxy_node", "")
            node = next((n for n in nodes if n["id"] == selected), None)
            if node is None:
                raise ExecutionError(
                    "Meta 固定代理节点尚未选择或已不可用。请管理员在 API 后台选择固定节点；不会自动直连或切换节点。"
                )
            return node["url"]
        except (DefinitionError, OSError):
            raise ExecutionError(
                "Meta 固定代理配置无法读取，已停止请求；请联系管理员，不会自动切换节点。"
            ) from None
