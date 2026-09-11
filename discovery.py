"""Bounded, credential-free document discovery and evidence-based OpenAPI conversion."""

import asyncio
import hashlib
import json
import re
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx
import yaml

from .definitions import DefinitionError, parse_definitions

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
MAX_DOCUMENT = 2_000_000


class DiscoveryError(DefinitionError):
    pass


class DocumentLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise DiscoveryError("文档暂不支持 YAML 锚点引用，请提供展开后的 OpenAPI 文档")
        return super().compose_node(parent, index)


def document_from_text(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_DOCUMENT:
        raise DiscoveryError("OpenAPI 文档超过 2 MB 或内容格式无效")
    try:
        value = yaml.load(text, Loader=DocumentLoader)
        # Bound nesting and total nodes before traversing document-controlled structures.
        remaining = 50_000

        def check(node, depth=0):
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > 35:
                raise DiscoveryError("文档结构过大或嵌套过深")
            if isinstance(node, dict):
                for key, child in node.items():
                    if not isinstance(key, (str, int)):
                        raise DiscoveryError("文档键名格式无效")
                    check(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    check(child, depth + 1)

        check(value)
        # Reject YAML dates, nonfinite numbers and other non-JSON objects.
        json.dumps(value, allow_nan=False)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, DiscoveryError):
            raise
        raise DiscoveryError("未获得有效的 OpenAPI JSON/YAML 文档") from exc
    if not isinstance(value, dict) or not isinstance(value.get("paths"), dict):
        raise DiscoveryError("文档缺少 OpenAPI paths，普通网页不能直接生成接口")
    if not (
        str(value.get("openapi", "")).startswith(("3.0.", "3.1.")) or value.get("swagger") == "2.0"
    ):
        raise DiscoveryError("当前支持 OpenAPI 3.0 / 3.1 和 Swagger 2.0")
    return value


def validate_url(url):
    if not isinstance(url, str) or len(url) > 4000:
        raise DiscoveryError("请填写有效的 HTTP(S) URL")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or any(ord(c) < 33 for c in url)
        ):
            raise ValueError
        _ = parts.port
    except ValueError as exc:
        raise DiscoveryError("请填写不含用户名、密码或片段的 HTTP(S) URL") from exc
    return parts


def origin(url):
    parts = validate_url(url)
    return (
        parts.scheme,
        parts.hostname.lower(),
        parts.port or (443 if parts.scheme == "https" else 80),
    )


def without_query(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def resolve_ref(document, value, seen=(), depth=0):
    if depth > 25:
        raise DiscoveryError("参数 schema 嵌套过深")
    if isinstance(value, list):
        return [resolve_ref(document, v, seen, depth + 1) for v in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise DiscoveryError("暂不支持外部 schema 引用，请提供合并后的文档")
        if ref in seen:
            raise DiscoveryError("暂不支持递归 schema，请使用手动定义")
        node = document
        try:
            for key in ref[2:].split("/"):
                node = node[key.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise DiscoveryError("文档包含无效的本地引用") from exc
        expanded = resolve_ref(document, node, (*seen, ref), depth + 1)
        if not isinstance(expanded, dict):
            raise DiscoveryError("引用目标必须是对象")
        return {
            **expanded,
            **resolve_ref(
                document, {k: v for k, v in value.items() if k != "$ref"}, seen, depth + 1
            ),
        }
    return {k: resolve_ref(document, v, seen, depth + 1) for k, v in value.items()}


def request_schema(document, schema):
    schema = resolve_ref(document, schema)
    if not isinstance(schema, dict):
        raise DiscoveryError("参数 schema 必须是对象")

    def convert(node):
        if isinstance(node, list):
            return [convert(v) for v in node]
        if not isinstance(node, dict):
            return node
        result = {k: convert(v) for k, v in node.items() if k not in ("nullable", "example", "xml")}
        if node.get("nullable") and isinstance(result.get("type"), str):
            result["type"] = [result["type"], "null"]
        if isinstance(result.get("properties"), dict):
            readonly = {
                k
                for k, v in result["properties"].items()
                if isinstance(v, dict) and v.get("readOnly")
            }
            result["properties"] = {
                k: v for k, v in result["properties"].items() if k not in readonly
            }
            if "required" in result:
                result["required"] = [k for k in result["required"] if k not in readonly]
        return result

    return convert(schema)


def slug(text, length=30):
    value = re.sub(r"[^a-zA-Z0-9_]", "_", str(text)).strip("_") or "operation"
    if not value[0].isalpha():
        value = "p_" + value
    return value[:length]


def credential_binding(document, operation, api_key, override):
    if override.get("mode", "auto") != "auto":
        mode = override["mode"]
        if mode == "none":
            return {}, "不附加 Key（手动选择）"
        if mode not in ("bearer", "header", "query"):
            raise DiscoveryError("未知 Key 传递方式")
        if not api_key:
            raise DiscoveryError("该操作需要填写 API Key / Token")
        key = "Authorization" if mode == "bearer" else override.get("name", "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise DiscoveryError("请填写有效的 Key 字段名称")
        return {
            ("headers" if mode in ("bearer", "header") else "query"): {
                key: ("Bearer " + api_key if mode == "bearer" else api_key)
            }
        }, f"{mode}: {key}（手动指定）"
    security = operation.get("security", document.get("security"))
    if security == [] or (security and {} in security):
        return {}, "文档声明无需鉴权"
    schemes = (
        document.get("securityDefinitions", {})
        if document.get("swagger")
        else document.get("components", {}).get("securitySchemes", {})
    )
    if security:
        if not api_key:
            raise DiscoveryError("文档声明此操作需要 API Key / Token")
        for requirement in security:
            if not isinstance(requirement, dict) or len(requirement) != 1:
                continue
            name, scopes = next(iter(requirement.items()))
            scheme = resolve_ref(document, schemes.get(name, {}))
            if scheme.get("type") == "apiKey" and scheme.get("in") in ("header", "query"):
                field = scheme.get("name")
                if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", field):
                    continue
                return {
                    ("headers" if scheme["in"] == "header" else "query"): {field: api_key}
                }, f"{scheme['in']}: {field}"
            if (
                scheme.get("type") == "http" and str(scheme.get("scheme")).lower() == "bearer"
            ) or scheme.get("type") in ("oauth2", "openIdConnect"):
                label = "Bearer Token"
                if scopes:
                    label += "；文档要求 scopes: " + ", ".join(str(s) for s in scopes)
                return {"headers": {"Authorization": "Bearer " + api_key}}, label
        raise DiscoveryError("文档要求多凭据或不支持的鉴权方式，请使用手动配置")
    documented_keys = {}
    for raw in operation.get("parameters", []):
        param = resolve_ref(document, raw)
        if param.get("in") in ("query", "header") and str(param.get("name", "")).lower() in (
            "api_key",
            "apikey",
            "x-api-key",
            "access_token",
        ):
            documented_keys[(param["in"], param["name"])] = param
    if len(documented_keys) == 1:
        if not api_key:
            raise DiscoveryError("文档参数包含 API Key / Token，请填写 Key")
        param = next(iter(documented_keys.values()))
        field = "headers" if param["in"] == "header" else "query"
        return {field: {param["name"]: api_key}}, f"按文档参数识别 {param['in']}: {param['name']}"
    if api_key:
        raise DiscoveryError(
            "文档未声明 Key 的传递位置；请在补充设置中选择 Header、Query 或 Bearer"
        )
    return {}, "文档未声明鉴权；未附加 Key"


def server_url(document, path_item, operation, document_url, target):
    if document.get("swagger"):
        host = document.get("host", urlsplit(target).netloc)
        scheme = (operation.get("schemes") or document.get("schemes") or [urlsplit(target).scheme])[
            0
        ]
        return f"{scheme}://{host}" + document.get("basePath", "")
    servers = operation.get("servers", path_item.get("servers", document.get("servers", [])))
    if not servers:
        parts = urlsplit(target)
        return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    # Select a documented server matching the user-entered destination, never a different host.
    for server in servers:
        url = server.get("url", "")
        for key, variable in server.get("variables", {}).items():
            if "default" not in variable:
                raise DiscoveryError("server 变量缺少默认值")
            url = url.replace("{" + key + "}", str(variable["default"]))
        if "{" in url:
            continue
        resolved = urljoin(document_url or target, url)
        if origin(resolved) == origin(target):
            return resolved
    raise DiscoveryError("文档服务器与 Target URL 不同，不会向其他主机发送 Key")


def operation_definition(
    document, path, path_item, method, operation, document_url, target, api_key, override
):
    if operation.get("x-import-warning"):
        raise DiscoveryError(str(operation["x-import-warning"])[:800])
    base = server_url(document, path_item, operation, document_url, target)
    url = base.rstrip("/") + path
    if origin(url) != origin(target):
        raise DiscoveryError("操作地址与 Target URL 不同")
    target_path = urlsplit(target).path.rstrip("/")
    base_path = urlsplit(base).path.rstrip("/")
    matcher = re.escape(urlsplit(url).path)
    path_names = re.findall(r"\{([^{}]+)\}", path)
    for name in path_names:
        matcher = matcher.replace(re.escape("{" + name + "}"), "([^/]+)")
    match = re.fullmatch(matcher.rstrip("/"), target_path)
    # A service base imports its operations; a concrete URL imports only its matching path.
    if (
        target_path not in ("", base_path)
        and not match
        and target_path != urlsplit(url).path.rstrip("/")
    ):
        return None
    auth_operation = {
        **operation,
        "parameters": [*path_item.get("parameters", []), *operation.get("parameters", [])],
    }
    bindings, auth_label = credential_binding(document, auth_operation, api_key, override)
    parameters = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    request = {"method": method, "url": url, **bindings}
    used = set()

    def add_parameter(name, schema, required):
        alias = slug(name, 40)
        base_alias, suffix = alias[:35], 1
        while alias in used:
            alias = base_alias + "_" + str(suffix)
            suffix += 1
        used.add(alias)
        parameters["properties"][alias] = schema
        if required:
            parameters["required"].append(alias)
        return alias

    combined = {}
    for value in [*path_item.get("parameters", []), *operation.get("parameters", [])]:
        param = resolve_ref(document, value)
        combined[(param.get("in"), param.get("name"))] = param
    for (location, name), param in combined.items():
        if not isinstance(name, str):
            raise DiscoveryError("参数缺少名称")
        if location == "body":
            if "json" in request:
                raise DiscoveryError("不支持多个请求体参数")
            media = operation.get("consumes", document.get("consumes", ["application/json"]))
            if not any("json" in content_type for content_type in media):
                raise DiscoveryError("请求体格式暂不支持自动接入")
            alias = add_parameter(
                name,
                request_schema(document, param.get("schema", {})),
                param.get("required", False),
            )
            request["json"] = {"$param": alias}
            continue
        if location not in ("path", "query", "header", "formData"):
            raise DiscoveryError("暂不支持 Cookie 或未知参数位置")
        field = {"query": "query", "header": "headers", "formData": "form"}.get(location)
        if field and any(str(k).lower() == name.lower() for k in bindings.get(field, {})):
            continue  # Credentials are constants, never model-visible arguments.
        schema = request_schema(
            document,
            param.get(
                "schema",
                {
                    k: v
                    for k, v in param.items()
                    if k in ("type", "items", "enum", "default", "minimum", "maximum", "format")
                },
            ),
        )
        kind = schema.get("type")
        if kind not in ("string", "number", "integer", "boolean", "array") or (
            location in ("path", "header") and kind == "array"
        ):
            raise DiscoveryError("路径/请求头需要标量；对象参数或未知参数类型请手动配置")
        if kind == "array" and schema.get("items", {}).get("type") not in (
            "string",
            "integer",
            "number",
            "boolean",
        ):
            raise DiscoveryError("仅支持标量数组参数")
        if param.get("description"):
            schema["description"] = str(param["description"])[:1500]
        if location == "path" and match and name in path_names:
            candidate = unquote(match.group(path_names.index(name) + 1))
            if not candidate.startswith("{"):
                try:
                    value = candidate if kind == "string" else json.loads(candidate)
                    from jsonschema import Draft202012Validator

                    if Draft202012Validator(schema).is_valid(value):
                        schema["default"] = value
                except ValueError:
                    pass
        alias = add_parameter(name, schema, location == "path" or param.get("required", False))
        value = {"$param": alias}
        if kind == "array":
            if document.get("swagger"):
                style = param.get("collectionFormat", "csv")
                joins = {"csv": ",", "ssv": " ", "pipes": "|", "multi": None}
                if style not in joins:
                    raise DiscoveryError("数组序列化方式暂不支持")
                delimiter = joins[style]
            else:
                style = param.get("style", "form")
                if style == "form":
                    delimiter = None if param.get("explode", True) else ","
                elif style in ("spaceDelimited", "pipeDelimited"):
                    delimiter = " " if style == "spaceDelimited" else "|"
                else:
                    raise DiscoveryError("数组序列化方式暂不支持")
            if delimiter is not None:
                value["$join"] = delimiter
        elif not document.get("swagger") and param.get(
            "style", "simple" if location in ("header", "path") else "form"
        ) not in ("simple", "form"):
            raise DiscoveryError("参数序列化方式暂不支持")
        if location == "path":
            request["url"] = request["url"].replace("{" + name + "}", "{" + alias + "}")
        else:
            request.setdefault(field, {})[name] = value
    if "requestBody" in operation:
        body = resolve_ref(document, operation["requestBody"])
        media = body.get("content", {})
        media_type = next(
            (m for m in media if m == "application/json" or m.endswith("+json")), None
        )
        if media_type is None and "application/x-www-form-urlencoded" in media:
            media_type = "application/x-www-form-urlencoded"
        if not media_type:
            raise DiscoveryError("文件上传或该请求体格式暂不支持自动接入")
        schema = request_schema(document, media[media_type].get("schema", {}))
        if media_type == "application/x-www-form-urlencoded":
            if media[media_type].get("encoding") or schema.get("type") != "object":
                raise DiscoveryError("自定义表单编码暂不支持")
            if any(
                v.get("type") not in ("string", "number", "integer", "boolean")
                for v in schema.get("properties", {}).values()
            ):
                raise DiscoveryError("自动表单接入暂仅支持标量字段")
        alias = add_parameter("body", schema, body.get("required", False))
        request["form" if media_type == "application/x-www-form-urlencoded" else "json"] = {
            "$param": alias
        }
        request.setdefault("headers", {})["Content-Type"] = media_type
    # Fixed URL query values are kept only when they do not replace generated parameters or credentials.
    for key, value in parse_qsl(urlsplit(target).query, keep_blank_values=True):
        if key not in request.get("query", {}):
            request.setdefault("query", {})[key] = value
    name = slug(operation.get("operationId") or method.lower() + "_" + path, 32)
    name += "_" + hashlib.sha256((method + url).encode()).hexdigest()[:8]
    description = str(
        operation.get("summary") or operation.get("description") or method + " " + path
    )[:3500]
    definition = {
        "name": name,
        "description": description,
        "enabled": False,
        "parameters": parameters,
        "request": request,
    }
    parse_definitions(json.dumps([definition], allow_nan=False))
    return {
        "method": method,
        "path": path,
        "description": description,
        "auth": auth_label,
        "definition": definition,
        "supported": True,
        "reason": None,
        "evidence": str(operation.get("x-evidence", ""))[:1000],
        "notes": str(operation.get("x-import-notes", ""))[:1000],
    }


def convert_document(document, target, document_url="", api_key="", override=None):
    results = []
    for path, raw_path in document["paths"].items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(raw_path, dict):
            continue
        if "$ref" in raw_path:
            # Resolve a Path Item only, not its potentially recursive response schemas.
            ref = raw_path["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise DiscoveryError("Path Item 外部引用暂不支持")
            item = document
            try:
                for key in ref[2:].split("/"):
                    item = item[key.replace("~1", "/").replace("~0", "~")]
                raw_path = item
            except (KeyError, TypeError) as exc:
                raise DiscoveryError("Path Item 引用无效") from exc
        if not isinstance(raw_path, dict):
            raise DiscoveryError("Path Item 引用目标不是对象")
        for method in METHODS:
            operation = raw_path.get(method.lower())
            if not isinstance(operation, dict):
                continue
            if len(results) >= 200:
                raise DiscoveryError("识别结果超过 200 个操作，请填写更具体的 Target URL")
            try:
                result = operation_definition(
                    document,
                    path,
                    raw_path,
                    method,
                    operation,
                    document_url,
                    target,
                    api_key,
                    override or {},
                )
                if result:
                    results.append(result)
            except (
                DefinitionError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                RecursionError,
            ) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, DiscoveryError)
                    else "文档中的参数结构暂不能转换，请使用手动接入"
                )
                results.append(
                    {
                        "method": method,
                        "path": path,
                        "description": str(operation.get("summary", ""))[:500],
                        "supported": False,
                        "definition": None,
                        "reason": reason,
                    }
                )
    return results


class Discovery:
    def __init__(self, client=None, reader=None):
        self.reader = reader
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def _fetch(self, url, method="GET", max_bytes=MAX_DOCUMENT, timeout=5):
        # Deliberately no API key, guessed authentication or redirects during discovery.
        try:
            async with self.client.stream(
                method,
                url,
                timeout=timeout,
                headers={"Accept": "application/json, application/yaml, text/yaml, text/html"},
                follow_redirects=False,
            ) as response:
                if method == "OPTIONS":
                    return response.headers.get("allow", ""), response.status_code
                if response.status_code != 200:
                    return "", response.status_code
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return "", 413
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8-sig"), 200
        except (httpx.HTTPError, UnicodeError, ValueError):
            return "", 0

    async def discover(self, payload):
        if not isinstance(payload, dict):
            raise DiscoveryError("请求必须是 JSON 对象")
        target = payload.get("target_url")
        parts = validate_url(target)
        api_key = payload.get("api_key", "")
        if not isinstance(api_key, str) or len(api_key) > 4096 or any(ord(c) < 32 for c in api_key):
            raise DiscoveryError("API Key 格式无效")
        override = payload.get("auth", {})
        if not isinstance(override, dict):
            raise DiscoveryError("Key 设置格式无效")
        manual_url = payload.get("document_url", "")
        manual_text = payload.get("document_text", "")
        if manual_url:
            validate_url(manual_url)
        from .document_import import interpret_document

        inferred = False
        if not isinstance(manual_text, str) or len(manual_text.encode()) > MAX_DOCUMENT:
            raise DiscoveryError("文档内容超过 2 MB 或格式无效")
        if api_key and (api_key in unquote(manual_url) or api_key in unquote(parts.path)):
            raise DiscoveryError("文档地址请勿包含 API Key，Key 只需填写在专用输入框")
        if manual_text or manual_url:
            source = manual_url or target
            if manual_text:
                raw = manual_text
            else:
                raw, status = await self._fetch(manual_url, max_bytes=8_000_000, timeout=15)
                if not raw:
                    raise DiscoveryError(
                        "文档网页无法读取（可能重定向、需登录或被拦截），请粘贴接口说明或请求示例"
                    )
            try:
                document = document_from_text(raw)
            except DiscoveryError:
                # Broken structured specs remain errors, rather than inviting model guesses.
                if re.search(r'(?:"openapi"|"swagger"|^openapi:|^swagger:)', raw[:1000], re.M):
                    raise
                document = await interpret_document(raw, target, api_key, self.reader)
                inferred = True
        else:
            root = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
            prefix = without_query(target).rstrip("/") + "/"
            candidates = []
            if parts.path.endswith((".json", ".yaml", ".yml", "/api-docs")):
                candidates.append(without_query(target))
            candidates += [
                urljoin(prefix, "openapi.json"),
                urljoin(root, "openapi.json"),
                urljoin(root, "swagger.json"),
                urljoin(root, "v3/api-docs"),
                urljoin(root, "openapi.yaml"),
                urljoin(root, "swagger/v1/swagger.json"),
            ]
            document, source = None, ""
            for url in list(dict.fromkeys(candidates))[:7]:
                raw, status = await self._fetch(url)
                if not raw:
                    continue
                try:
                    document = document_from_text(raw)
                    source = url
                    break
                except DiscoveryError:
                    continue
            if document is None:
                allow, _ = await self._fetch(without_query(target), "OPTIONS")
                methods = [m for m in METHODS if m in {v.strip().upper() for v in allow.split(",")}]
                return {
                    "operations": [],
                    "methods": methods,
                    "source": None,
                    "message": "未找到可自动读取的接口定义。请填写平台 API 文档网页，或粘贴接口说明、参数表、请求示例，不要求 OpenAPI 格式。服务端声明的方法不能验证 Key 权限。",
                }
        operations = convert_document(document, target, source, api_key, override)
        return {
            "operations": operations,
            "methods": sorted({o["method"] for o in operations}),
            "source": without_query(source),
            "inferred": inferred,
            "message": (
                "已由模型阅读普通文档生成草稿，请核对路径、请求方式及参数后选择允许调用的操作。未执行接口验证，Key 的实际权限未验证。"
                if inferred
                else "已根据文档生成操作。请选择允许 AstrBot 调用的操作；未勾选的操作会保存为停用。Key 的实际权限未验证。"
            )
            if operations
            else "文档中没有匹配 Target URL 的操作，请确认服务根地址或具体接口路径。",
        }

    async def run(self, payload):
        if not isinstance(payload, dict):
            raise DiscoveryError("请求必须是 JSON 对象")
        try:
            async with asyncio.timeout(
                210 if payload.get("document_url") or payload.get("document_text") else 25
            ):
                return await self.discover(payload)
        except TimeoutError as exc:
            raise DiscoveryError("识别超时，请粘贴相关接口段落后重试") from exc
