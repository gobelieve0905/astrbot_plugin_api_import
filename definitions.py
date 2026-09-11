"""Validate platform-independent API definitions before registering any tool."""

import copy
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
PLACEHOLDER = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")

DEFINITION_SCHEMA = {
    "type": "object",
    "required": ["name", "description", "request"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "pattern": NAME.pattern},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 80, "pattern": r"\S"},
        "connection": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "name", "platform", "operation"],
            "properties": {
                "id": {"type": "string", "pattern": "^[a-zA-Z0-9_]{1,48}$"},
                "name": {"type": "string", "minLength": 1, "maxLength": 80, "pattern": r"\S"},
                "platform": {"type": "string", "pattern": NAME.pattern},
                "operation": {"type": "string", "pattern": NAME.pattern},
            },
        },
        "description": {"type": "string", "minLength": 1, "maxLength": 4000},
        "enabled": {"type": "boolean"},
        "parameters": {"type": "object"},
        "request": {
            "type": "object",
            "required": ["url", "method"],
            "additionalProperties": False,
            "properties": {
                "url": {"type": "string"},
                "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
                "query": {"type": "object"},
                "headers": {"type": "object"},
                "json": {},
                "form": {"type": "object"},
                "timeout": {"type": "number", "minimum": 1, "maximum": 120},
            },
        },
        "response": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pointer": {"type": "string", "pattern": r"^(?:/(?:[^~]|~[01])*)*$"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 20971520},
                "preview_chars": {"type": "integer", "minimum": 100, "maximum": 20000},
                "save": {"type": "boolean"},
            },
        },
    },
}


class DefinitionError(ValueError):
    """A configuration error with a location, without leaking configured values."""


@dataclass(frozen=True)
class Definition:
    name: str
    description: str
    enabled: bool
    parameters: dict
    request: dict
    response: dict
    display_name: str = ""

    @property
    def tool_name(self):
        return "api_" + self.name


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def parse_definitions(raw: str) -> list[Definition]:
    if not isinstance(raw, str) or len(raw) > 1_000_000:
        raise DefinitionError("tools_json 必须是小于 1 MB 的 JSON 文本")
    try:
        values = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as exc:
        raise DefinitionError("tools_json 不是有效 JSON") from exc
    if not isinstance(values, list) or len(values) > 200:
        raise DefinitionError("tools_json 必须是数组，最多 200 个工具")
    result, names = [], set()
    connections = {}
    for index, value in enumerate(values):
        prefix = f"工具[{index + 1}]"
        errors = list(Draft202012Validator(DEFINITION_SCHEMA).iter_errors(value))
        if errors:
            error = errors[0]
            location = ".".join(str(x) for x in error.absolute_path) or "定义"
            raise DefinitionError(f"{prefix}.{location} 不符合 {error.validator} 规则")
        name = value["name"]
        if name in names:
            raise DefinitionError(f"{prefix}: 工具名称重复")
        names.add(name)
        parameters = copy.deepcopy(value.get("parameters", {"type": "object", "properties": {}}))
        if parameters.get("type") != "object":
            raise DefinitionError(f"{prefix}.parameters.type 必须是 object")
        # No remote schema retrieval or executable template expressions.
        if any(
            isinstance(node, dict) and any(k in node for k in ("$ref", "$dynamicRef"))
            for node in _walk(parameters)
        ):
            raise DefinitionError(f"{prefix}.parameters 暂不支持 schema 引用")
        parameters.setdefault("additionalProperties", False)
        try:
            Draft202012Validator.check_schema(parameters)
        except SchemaError as exc:
            raise DefinitionError(f"{prefix}.parameters 不是有效 JSON Schema") from exc
        props = parameters.get("properties", {})
        if any(not NAME.fullmatch(key) for key in props):
            raise DefinitionError(f"{prefix}: 参数名称仅支持字母开头的字母、数字、下划线")
        if not set(parameters.get("required", [])).issubset(props):
            raise DefinitionError(f"{prefix}: required 引用了未定义参数")
        for schema in props.values():
            if isinstance(schema, dict) and "default" in schema:
                if not Draft202012Validator(schema).is_valid(schema["default"]):
                    raise DefinitionError(f"{prefix}: 参数默认值不符合类型约束")
        request = copy.deepcopy(value["request"])
        url = request["url"]
        try:
            parts = urlsplit(url)
            valid_url = (
                parts.scheme in ("http", "https")
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.fragment
                and not any(c in parts.netloc for c in "{}")
                and not any(c in parts.query for c in "{}")
                and not any(ord(c) < 33 for c in url)
            )
            _ = parts.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise DefinitionError(f"{prefix}: URL 必须为固定 HTTP(S) 地址，占位符仅允许出现在路径")
        placeholders = set(PLACEHOLDER.findall(parts.path))
        if any(c in PLACEHOLDER.sub("", parts.path) for c in "{}"):
            raise DefinitionError(f"{prefix}: URL 路径占位符格式错误")
        if not placeholders.issubset(parameters.get("required", [])):
            raise DefinitionError(f"{prefix}: URL 路径参数必须声明为必填参数")
        if "json" in request and "form" in request:
            raise DefinitionError(f"{prefix}: json 和 form 不能同时配置")
        for field in ("query", "headers", "json", "form"):
            for node in _walk(request.get(field)):
                if isinstance(node, dict) and "$param" in node:
                    if (
                        (
                            set(node) not in ({"$param"}, {"$param", "$join"})
                            or ("$join" in node and node["$join"] not in (",", " ", "|"))
                        )
                        or not isinstance(node["$param"], str)
                        or node["$param"] not in props
                    ):
                        raise DefinitionError(f"{prefix}.{field}: $param 必须单独引用已定义参数")
        display_name = value.get("display_name") or "api_" + name
        description = value["description"]
        if value.get("display_name"):
            description = value["display_name"] + "。" + description
        if connection := value.get("connection"):
            identity = (connection["name"], connection["platform"])
            previous, operations = connections.setdefault(connection["id"], (identity, set()))
            if previous != identity or connection["operation"] in operations:
                raise DefinitionError(f"{prefix}: 同一接入账户的名称、平台或操作标识不一致")
            operations.add(connection["operation"])
            display_name = connection["name"] + " / " + display_name
            description = "接入账户：" + connection["name"] + "。" + description
        result.append(
            Definition(
                name,
                description,
                value.get("enabled", True),
                parameters,
                request,
                copy.deepcopy(value.get("response", {})),
                display_name,
            )
        )
    return result
