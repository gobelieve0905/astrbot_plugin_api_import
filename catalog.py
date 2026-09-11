"""Revision-checked CRUD over the existing AstrBot configuration."""

import copy
import hashlib
import json

from .definitions import DefinitionError, parse_definitions
from .platforms import build_connection


class ConflictError(DefinitionError):
    pass


class Catalog:
    def __init__(self, config, apply):
        self.config = config
        self.apply = apply

    def snapshot(self):
        raw = self.config.get("tools_json", "[]")
        revision = hashlib.sha256(str(raw).encode()).hexdigest()
        try:
            parse_definitions(raw)
            return {"items": json.loads(raw), "revision": revision, "error": None}
        except DefinitionError as exc:
            return {"items": [], "revision": revision, "error": str(exc)}

    def mutate(self, action, payload):
        if not isinstance(payload, dict):
            raise DefinitionError("请求必须是 JSON 对象")
        state = self.snapshot()
        if payload.get("revision") != state["revision"]:
            raise ConflictError("接口列表已更新，请刷新列表后重新编辑；当前草稿仍保留")
        if state["error"]:
            raise DefinitionError("原有 JSON 配置无效，请先在插件配置中修复，避免覆盖原始数据")
        items = copy.deepcopy(state["items"])
        if action == "save":
            value = payload.get("definition")
            if not isinstance(value, dict):
                raise DefinitionError("接口定义必须是 JSON 对象")
            original = payload.get("original_name")
            if original is None:
                items.append(value)
            else:
                index = next((i for i, item in enumerate(items) if item["name"] == original), None)
                if index is None:
                    raise ConflictError("该接口已不存在，请刷新列表")
                items[index] = value
        elif action == "connect-platform":
            items.extend(build_connection(payload, items))
        elif action == "batch":
            incoming = payload.get("definitions")
            if not isinstance(incoming, list) or not incoming or len(incoming) > 200:
                raise DefinitionError("请提供 1–200 个接口操作")
            items.extend(incoming)
        elif action == "permissions":
            selections = payload.get("enabled_names")
            known = {item["name"] for item in items}
            if (
                not isinstance(selections, list)
                or any(not isinstance(name, str) for name in selections)
                or not set(selections).issubset(known)
                or len(selections) != len(set(selections))
            ):
                raise DefinitionError("调用权限列表无效，请刷新后重试")
            for item in items:
                item["enabled"] = item["name"] in selections
        elif action == "delete":
            name = payload.get("name")
            if not any(item["name"] == name for item in items):
                raise ConflictError("该接口已不存在，请刷新列表")
            items = [item for item in items if item["name"] != name]
        else:
            raise DefinitionError("未知管理操作")
        try:
            raw = json.dumps(items, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise DefinitionError("接口定义包含无效 JSON 值") from exc
        definitions = parse_definitions(raw)
        self.apply(raw, definitions)
        return self.snapshot()
