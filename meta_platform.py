"""Pinned Meta Business API operations; permissions are routes, never model instructions."""

import base64
import binascii
import copy
import json
import re
from functools import lru_cache
from pathlib import Path

from .definitions import DefinitionError
from .meta_descriptions import describe

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


def tool_guidance(operation_id):
    op = operations()[operation_id]
    objects = sorted({source["object"] for source in op["sources"]})
    scope = "适用 SDK 对象：" + ", ".join(objects[:12])
    if len(objects) > 12:
        scope += f" 等 {len(objects)} 类对象，完整范围请查看后台操作说明"
    if operation_id == "get_adaccounts":
        scope += "。查询当前用户可访问的广告账户使用 node_id=me；路径为 /me/adaccounts（不含下划线）。必须遍历完整账户列表及分页；账户名称不能证明项目归属，不能仅凭名称关键词排除其他账户，也不能找到一个同名账户就停止。使用后台已开启的广告/广告组等查询核对应用标识、promoted_object 或其他明确项目映射；无法核实时列出未核实范围，不得称为全量结果"
    elif operation_id == "get_ad_accounts":
        scope += "。此接口不能用于发现当前用户的广告账户，不接受 node_id=me；用户账户列表需使用另一个已授权的 get_adaccounts 操作"
    if operation_id == "get_insights":
        scope += "。这就是 Meta Insights 报表接口。用户已给出广告系列归属规则时，可对各账户直接查询 level=ad 的报表并用 campaign.name 等已确认字段筛选，无须先逐个查询广告系列、广告组、广告再取指标。先列全账户，维护成功、失败、未查询账户清单；不能只扫前一批就结束。大报表如需异步任务，只有后台已开启对应 POST 和报告查询操作才可使用，不能自行开启。只返回当前 node_id 的单页报表，不代表全部相关账户。跨账户汇总先核实项目与账户/广告的关联，遍历相关账户和分页后再聚合排序；未核实账户、截断或未读分页必须说明。返回文件仅是本次响应，不等于已经读取、分析完整数据。跨账户、按素材名称聚合后的 Top N，必须取全范围再聚合排序，不能用单账户 sort 加 limit 代替。不要猜测 purchase_descending 等排序字段；sort 无效的 #100 错误属于参数错误，不能解释为无权限。消耗排序 spend_descending 不等于 Purchase 排序；Purchase 统计应核对 actions 中实际 action_type，避免重复计算不同归因口径。"
    return (
        scope
        + "。请检查 page 信息；预览截断时先缩小 limit 重取当前页，不能直接用下一页游标跳过未读数据。不要凭接口名称相似而替换对象类型或绕过后台权限。"
    )


def permission_restriction(op):
    if op["method"] != "GET" and "batch" in op["path"]:
        return "安全限制：此批量写入接口暂不执行，因为尚不能逐项验证内部子操作权限；即使勾选也会被后端拒绝。请使用单项操作。"
    if op["id"] == "post_copies":
        return "复制权限：还须同时开启创建广告系列、广告组和广告，避免复制或深度复制绕过创建开关。"
    return ""


def operation_help(op):
    root = (
        "https://github.com/facebook/facebook-python-business-sdk/blob/"
        + catalog()["source_commit"]
        + "/facebook_business/adobjects/"
    )
    files = {}
    for source in op["sources"]:
        files.setdefault(source["file"], []).append(source["object"] + "." + source["method"])
    effect = {
        "GET": "查询对象或关联数据；返回字段、筛选和分页由请求参数决定。",
        "POST": "可能创建对象、修改配置或提交任务；具体行为取决于对象和 SDK 方法，不能按只读查询理解。",
        "DELETE": "可能删除对象或解除关联；具体范围由对象和参数决定。",
        "PUT": "更新对象或关联配置；具体范围由对象和参数决定。",
    }[op["method"]]
    return {
        "permission_explanation": (
            permission_restriction(op)
            + effect
            + " 此开关允许当前接入账户调用 "
            + op["method"]
            + " /{node_id}"
            + op["path"]
            + "；下列 SDK 对象共享该方法与路径权限，不是单独授权其中一个对象。"
            + " 关闭后 Agent 不能调用此项；开启不等于获得 Meta 权限，仍受 Token、应用权限及资产访问范围限制。"
        ),
        "parameter_explanation": "下列参数来自固定 SDK 目录；同一路径合并了不同对象的参数，不表示每个对象都支持全部参数。请在对应对象的 SDK 方法中核对字段、类型与枚举；必填项及平台权限以官方接口要求为准。",
        "references": [
            {
                "label": "官方接入指南",
                "url": "https://developers.facebook.com/docs/business-sdk/getting-started",
            },
            {
                "label": "官方 Access Token 说明",
                "url": "https://developers.facebook.com/docs/facebook-login/access-tokens",
            },
            {
                "label": "官方 Marketing API 字段参考目录",
                "url": "https://developers.facebook.com/docs/marketing-api/reference",
            },
        ]
        + [
            {"label": "官方 SDK · " + ", ".join(sorted(set(methods))), "url": root + file}
            for file, methods in files.items()
        ],
    }


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
                "title": VERBS[op["method"]]
                + " · "
                + TITLES.get(op["path"].strip("/") or "node", describe(op)["label"]),
                **describe(op),
                **operation_help(op),
                "method": op["method"],
                "path": "/{node_id}" + op["path"],
                "description": "对象："
                + ", ".join(sorted({s["object"] for s in op["sources"]}))
                + "；参数："
                + ", ".join(
                    k + " (" + " / ".join(v) + ")"
                    for k, v in op["params"].items()
                    if k not in RESERVED
                ),
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
    if op["method"] != "GET" and "batch" in op["path"]:
        raise ExecutionError(permission_restriction(op))
    if op["id"] == "post_copies" and not all(
        permitted(connection["id"], child)
        for child in ("post_campaigns", "post_adsets", "post_ads")
    ):
        raise ExecutionError(permission_restriction(op))
    if op["id"] == "get_ad_accounts" and arguments.get("node_id") == "me":
        raise ExecutionError(
            "当前用户账户发现应使用 get_adaccounts（/me/adaccounts，无下划线）；get_ad_accounts 只适用于合作广告配置或信用账单组。请选择后台已开启的 get_adaccounts，未开启时请联系管理员，不能绕过权限。"
        )
    params = copy.deepcopy(arguments.get("params", {}))

    def inspect(value, parent_key=""):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in RESERVED - {"fields"}:
                    raise ExecutionError("禁止在 Meta 参数中覆盖鉴权、方法或嵌入 Graph 批量请求")
                if key.lower() == "fields":
                    raise ExecutionError("请使用顶层 fields 数组，禁止嵌入字段展开")
                inspect(child, key.lower())
        elif isinstance(value, list):
            for child in value:
                inspect(child, parent_key)
        elif (
            parent_key == "status"
            and isinstance(value, str)
            and value.strip().upper() == "DELETED"
            and not permitted(connection["id"], "delete_node")
        ):
            raise ExecutionError("删除状态需要后台同时开启删除对象操作")

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
