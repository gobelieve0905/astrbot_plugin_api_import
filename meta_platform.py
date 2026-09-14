"""Pinned Meta Business API operations; permissions are routes, never model instructions."""

import base64
import binascii
import copy
import json
import re
from functools import lru_cache
from pathlib import Path

from .definitions import DefinitionError

PLATFORM = "meta_marketing"
DOCUMENTATION = "https://developers.facebook.com/docs/marketing-api/"
RESERVED = {
    "access_token",
    "appsecret_proof",
    "app_secret",
    "client_secret",
    "method",
    "_method",
    "http_method",
    "batch",
    "relative_url",
    "ids",
    "fields",
    "debug",
}
NODE = r"^(?:me|act_[0-9]+|[0-9]+(?:_[0-9]+)?)$"
FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
TITLES = {
    "node": "对象详情 / 通用对象",
    "adaccounts": "广告账户",
    "campaigns": "广告系列",
    "adsets": "广告组",
    "ads": "广告",
    "insights": "效果报表",
    "adcreatives": "广告创意",
    "customaudiences": "自定义受众",
    "adimages": "广告图片",
    "advideos": "广告视频",
    "adspixels": "广告像素",
    "activities": "活动记录",
}
VERBS = {"GET": "查询", "POST": "创建 / 修改", "DELETE": "删除", "PUT": "更新"}


@lru_cache(maxsize=1)
def catalog():
    return json.loads((Path(__file__).parent / "data/meta_operations.json").read_text())


@lru_cache(maxsize=1)
def operations():
    return {op["id"]: op for op in catalog()["operations"]}


def title(op):
    edge = op["path"].strip("/") or "node"
    return VERBS[op["method"]] + " · " + TITLES.get(edge, edge)


def endpoint(op):
    return "https://graph.facebook.com/" + catalog()["api_version"] + "/{node_id}" + op["path"]


def platform_entry():
    return {
        "id": PLATFORM,
        "name": "Meta 广告管理",
        "token_label": "Access Token",
        "summary": "广告账户、系列、广告组、广告、报表及 Business SDK 资产操作",
        "description": f"Meta 官方 Business SDK {catalog()['sdk_version']} / Graph {catalog()['api_version']} 操作目录。填写 Access Token；账户或对象 ID 在调用时提供。默认全部关闭，按请求方法与路径授权；同一路径的 SDK 别名共享权限。插件不会授予 Meta 账户或应用权限。通用对象操作适用于 Token 可访问的对象，请按需开启。",
        "operations": [
            {
                "id": op["id"],
                "title": title(op),
                "method": op["method"],
                "path": "/{node_id}" + op["path"],
                "description": "对象："
                + ", ".join(sorted({s["object"] for s in op["sources"]}))
                + "；参数："
                + ", ".join(k for k in op["params"] if k not in RESERVED),
                "documentation": "https://github.com/facebook/facebook-python-business-sdk/blob/"
                + catalog()["source_commit"]
                + "/facebook_business/adobjects/"
                + op["sources"][0]["file"],
            }
            for op in operations().values()
        ],
    }


def _type_schema(types):
    schemas = []
    for typ in types:
        if typ == "file":
            schema = {
                "type": "object",
                "required": ["base64"],
                "additionalProperties": False,
                "properties": {
                    "base64": {"type": "string", "maxLength": 13981016},
                    "filename": {"type": "string", "maxLength": 120},
                },
            }
        elif typ == "list":
            schema = {"type": "array"}
        elif typ.startswith("list<"):
            schema = {"type": "array", "items": _type_schema([typ[5:-1]])}
        elif typ in {"int", "unsigned int"}:
            schema = {"type": "integer"}
            if typ == "unsigned int":
                schema["minimum"] = 0
        elif typ in {"float", "double"}:
            schema = {"type": "number"}
        elif typ == "bool":
            schema = {"type": "boolean"}
        elif typ in {"string", "datetime"} or typ.endswith("_enum"):
            # SDK enums include both numeric and string constants.
            schema = (
                {"type": ["string", "integer"]} if typ.endswith("_enum") else {"type": "string"}
            )
        else:
            schema = {"type": "object"}
        if schema not in schemas:
            schemas.append(schema)
    return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}


@lru_cache(maxsize=1024)
def parameters(operation_id):
    op = operations()[operation_id]
    props = {key: _type_schema(types) for key, types in op["params"].items() if key not in RESERVED}
    # Graph cursors are independent of SDK edge-specific params.
    if op["method"] == "GET":
        for key in ("after", "before"):
            props.setdefault(key, {"type": "string", "maxLength": 4096})
        props.setdefault("limit", {"type": "integer", "minimum": 1, "maximum": 1000})
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["node_id"],
        "properties": {
            "node_id": {
                "type": "string",
                "pattern": NODE,
                "description": "广告账户使用 act_数字；广告系列、广告组、广告等使用真实对象 ID；当前用户可用 me。不要猜测 ID。",
            },
            "fields": {
                "type": "array",
                "items": {"type": "string", "pattern": FIELD.pattern},
                "maxItems": 100,
                "uniqueItems": True,
                "description": "返回字段名数组。禁止关系展开、别名、函数和内嵌查询；分页使用 params.after。",
            },
            "params": {
                "type": "object",
                "additionalProperties": False,
                "properties": props,
                "description": "官方操作参数；对象和数组自动编码为 JSON，Token 由后台提供。文件参数传 {base64, filename}，单次文件总量最多 10 MiB。参数组合与权限由 Meta 校验。",
            },
        },
    }

    for variable in re.findall(r"\{([A-Za-z][A-Za-z0-9_]*)\}", op["path"]):
        schema["properties"][variable] = {
            "type": "string",
            "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,199}$",
            "description": "官方路径中的对象标识，不接受路径、URL 或查询字符串。",
        }
        schema["required"].append(variable)
    return schema


def definition(op, token, suffix, enabled):
    return {
        "name": f"meta_{op['id']}_{suffix}",
        "display_name": title(op),
        "description": f"Meta {title(op)}。{op['method']} /{{node_id}}{op['path']}。仅执行后台已授权的操作。账户 ID 用 act_ 前缀。返回单页，使用 after 游标继续；写请求不自动重试。Meta 返回错误时依据实际错误解释，不能猜测结果。",
        "enabled": enabled,
        "parameters": copy.deepcopy(parameters(op["id"])),
        "request": {
            "method": op["method"],
            "url": endpoint(op),
            "headers": {"Authorization": "Bearer " + token},
            "timeout": 60,
        },
        "response": {"preview_chars": 12000, "max_bytes": 5242880, "save": True},
    }


def validate_preset(value):
    """Fail closed if an edited preset tries to change its routing or schema."""
    op = operations().get(value["connection"]["operation"])
    req = value["request"]
    authorization = req.get("headers", {}).get("Authorization", "")
    if (
        not op
        or req.get("method") != op["method"]
        or req.get("url") != endpoint(op)
        or set(req) - {"method", "url", "headers", "timeout"}
        or set(req.get("headers", {})) != {"Authorization"}
        or not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or not authorization[7:]
        or any(c.isspace() for c in authorization[7:])
        or len(authorization) > 4103
        or value.get("parameters") != parameters(op["id"])
    ):
        raise DefinitionError(
            "Meta 预设的操作、地址和参数结构由官方目录固定，请在账户设置中修改 Token 和权限"
        )


def prepare(definition, arguments, permitted):
    """Build exactly one allowed route; never accept Graph request envelopes."""
    from .engine import ExecutionError

    connection = definition.connection
    op = operations()[connection["operation"]]
    if not permitted(connection["id"], op["id"]):
        raise ExecutionError("此 Meta 操作已被后台关闭，不能通过对话开启")
    params = copy.deepcopy(arguments.get("params", {}))

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in RESERVED - {"fields"}:
                    raise ExecutionError("禁止在 Meta 参数中覆盖鉴权、方法或嵌入 Graph 批量请求")
                if key.lower() == "fields":
                    raise ExecutionError("请使用顶层 fields 数组，禁止嵌入字段展开")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(params)
    fields = arguments.get("fields", [])
    for field in fields:
        if not FIELD.fullmatch(field) or field not in catalog()["fields"]:
            raise ExecutionError("返回字段必须来自官方字段目录，且不能包含字段展开或别名")
        edge = next(
            (
                candidate
                for candidate in operations().values()
                if candidate["method"] == "GET" and candidate["path"] == "/" + field
            ),
            None,
        )
        if edge and not permitted(connection["id"], edge["id"]):
            raise ExecutionError("字段涉及后台未开启的关联查询，请先在后台调整对应操作")
    if str(params.get("status", "")).upper() == "DELETED" and not permitted(
        connection["id"], "delete_node"
    ):
        raise ExecutionError("删除状态需要后台同时开启删除对象操作")
    if fields:
        params["fields"] = ",".join(fields)
    files, total = {}, 0
    for key, types in op["params"].items():
        if "file" not in types or key not in params:
            continue
        value = params[key]
        if not isinstance(value, dict) or "base64" not in value:
            continue
        try:
            data = base64.b64decode(value["base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ExecutionError("文件 base64 编码无效") from exc
        total += len(data)
        if total > 10 * 1024 * 1024:
            raise ExecutionError("单次上传文件总量不能超过 10 MiB，请使用官方分片上传操作")
        filename = value.get("filename", "upload.bin")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", filename) or filename in {".", ".."}:
            raise ExecutionError("上传文件名只允许英文字母、数字、点、横线和下划线")
        files[key] = (filename, data, "application/octet-stream")
        del params[key]
    encoded = {
        key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, (dict, list, bool))
        else str(value)
        for key, value in params.items()
    }
    kwargs = {"headers": definition.request["headers"]}
    if op["method"] == "GET":
        kwargs["params"] = encoded
    else:
        kwargs["data"] = encoded
    if files:
        kwargs["files"] = files
    url = re.sub(r"\{([A-Za-z][A-Za-z0-9_]*)\}", lambda match: arguments[match[1]], endpoint(op))
    return url, kwargs


def redact_response(value, token):
    """No reusable credentials or credential-bearing paging URLs enter model/files."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if re.search(r"(?i)(token|secret|password|authorization|cookie)", key)
            else redact_response(child, token)
            for key, child in value.items()
            if key not in {"next", "previous"}
        }
    if isinstance(value, list):
        return [redact_response(child, token) for child in value]
    if isinstance(value, str):
        from urllib.parse import quote

        value = value.replace(token, "[REDACTED]").replace(quote(token, safe=""), "[REDACTED]")
        return re.sub(r"(?i)(access_token|appsecret_proof)=[^&\s]+", r"\1=[REDACTED]", value)
    return value
