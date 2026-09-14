"""Connection identity and conservative compatibility for 0.1.1 presets."""

import copy
import json
import re

from .definitions import DefinitionError, parse_definitions
from .platforms import BASE, OPERATIONS


def enrich_legacy(items):
    items = copy.deepcopy(items)
    candidates = {}
    operations = {op[0]: op for op in OPERATIONS}
    for item in items:
        if item.get("connection"):
            continue
        match = re.fullmatch(r"applovin_(.+)_([0-9a-f]{8})", item["name"])
        if not match or match[1] not in operations:
            continue
        operation, suffix = match.groups()
        expected = operations[operation]
        req = item["request"]
        if req["method"] != "GET" or req["url"] != BASE + expected[2]:
            continue
        if expected[2] == "/report" and req.get("query", {}).get("report_type") != operation:
            continue
        candidates.setdefault(suffix, []).append((item, expected))
    used = {item["connection"]["id"] for item in items if item.get("connection")}
    for suffix, group in candidates.items():
        keys = [item["request"].get("query", {}).get("api_key") for item, _ in group]
        if suffix in used or not all(
            isinstance(key, str) and key and key == keys[0] for key in keys
        ):
            continue
        for item, operation in group:
            item["connection"] = {
                "id": suffix,
                "name": "AppLovin 账户 " + suffix,
                "platform": "applovin_report",
                "operation": operation[0],
            }
            item.setdefault("display_name", operation[1])
    return items


def update_connection(items, payload, delete=False):
    connection_id = payload.get("connection_id")
    members = [item for item in items if item.get("connection", {}).get("id") == connection_id]
    if not isinstance(connection_id, str) or not members:
        raise DefinitionError("接入账户已不存在，请刷新列表")
    if delete:
        return [item for item in items if item not in members]
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise DefinitionError("接入名称须为 1–80 个字符")
    call_name = payload.get("call_name")
    if call_name is not None and (
        not isinstance(call_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z_]{0,39}", call_name)
    ):
        raise DefinitionError("调用名称须为 1–40 位英文字母或下划线，并以英文字母开头")
    token = payload.get("token", "")
    if not isinstance(token, str) or len(token) > 4096 or any(c.isspace() for c in token):
        raise DefinitionError("Token 格式无效，请不要包含空格或换行")
    selected = payload.get("enabled_names")
    known = {item["name"] for item in members}
    if (
        not isinstance(selected, list)
        or any(not isinstance(value, str) for value in selected)
        or len(selected) != len(set(selected))
        or not set(selected).issubset(known)
    ):
        raise DefinitionError("操作开关无效，请刷新后重试")
    current_names = {item.name: item.tool_name for item in parse_definitions(json.dumps(items))}
    operations = {op[0]: op for op in OPERATIONS}
    for item in members:
        if token and item["connection"]["platform"] == "meta_marketing":
            from .meta_platform import validate_preset

            validate_preset(item)
            item["request"]["headers"]["Authorization"] = "Bearer " + token
        elif token:
            operation = operations.get(item["connection"]["operation"])
            req = item["request"]
            if (
                not operation
                or item["connection"]["platform"] != "applovin_report"
                or req["url"] != BASE + operation[2]
                or req["method"] != "GET"
            ):
                raise DefinitionError("此账户包含已自定义地址的操作，请在操作编辑中单独修改 Token")
            req.setdefault("query", {})["api_key"] = token
        if call_name is None and not item["connection"].get("call_name"):
            item.setdefault("tool_name", current_names[item["name"]])
        if call_name is not None:
            item["connection"]["call_name"] = call_name
            item.pop("tool_name", None)
        item["connection"]["name"] = name.strip()
        item["enabled"] = item["name"] in selected
    return items
