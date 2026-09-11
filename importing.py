"""Convert pasted cURL text into a draft, without shell execution or network access."""

import json
import shlex
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .definitions import DefinitionError, parse_definitions


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            previous = result[key]
            result[key] = [*previous, value] if isinstance(previous, list) else [previous, value]
        else:
            result[key] = value
    return result


def import_curl(text):
    if not isinstance(text, str) or len(text) > 200_000:
        raise DefinitionError("cURL 内容必须是少于 200 KB 的文本")
    try:
        lexer = shlex.shlex(
            text.replace("\\\r\n", "").replace("\\\n", ""), posix=True, punctuation_chars=";&|<>"
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise DefinitionError("cURL 引号不完整，请检查粘贴内容") from exc
    if not tokens or tokens.pop(0) != "curl":
        raise DefinitionError("请粘贴以 curl 开头的单条命令")
    if any(token in (";", "&&", "||", "|", "&", ">", "<") for token in tokens):
        raise DefinitionError("不支持管道、重定向或多条命令；URL 中的 & 请放在引号内")
    headers, data, encoded, urls = {}, [], [], []
    form_parts = []
    method, timeout, get, head, force_json = None, 30, False, False, False
    value_flags = {
        "-X",
        "--request",
        "-H",
        "--header",
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--json",
        "--data-urlencode",
        "--url",
        "-m",
        "--max-time",
    }
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        inline = None
        if token.startswith("--") and "=" in token:
            token, inline = token.split("=", 1)
        elif token[:2] in ("-X", "-H", "-d", "-m") and len(token) > 2:
            token, inline = token[:2], token[2:]
        if token in value_flags:
            if inline is None:
                if i == len(tokens):
                    raise DefinitionError("cURL 选项缺少值")
                inline = tokens[i]
                i += 1
            if token in ("-X", "--request"):
                method = inline.upper()
            elif token in ("-H", "--header"):
                if ":" not in inline:
                    raise DefinitionError("Header 应为 名称: 值")
                key, value = inline.split(":", 1)
                key = key.strip().lower()
                if not key or key in headers:
                    raise DefinitionError("不支持空请求头名称或重复请求头，请先合并")
                if key == "content-length":
                    continue  # Recomputed by HTTP client after editing.
                headers[key] = value.strip()
            elif token == "--url":
                urls.append(inline)
            elif token in ("-m", "--max-time"):
                try:
                    timeout = float(inline)
                except ValueError as exc:
                    raise DefinitionError("超时时间必须是数字") from exc
            elif token == "--data-urlencode":
                if "=" not in inline:
                    raise DefinitionError("--data-urlencode 只支持 名称=值，不支持文件")
                encoded.append(tuple(inline.split("=", 1)))
                form_parts.append(("encoded", inline))
            else:
                if inline.startswith("@") and token != "--data-raw":
                    raise DefinitionError("不读取 cURL 引用的本地文件，请直接填写请求体")
                force_json = force_json or token == "--json"
                data.append(inline)
                form_parts.append(("data", inline))
        elif token in ("-G", "--get"):
            get = True
        elif token in ("-I", "--head"):
            head = True
        elif token in ("-s", "--silent", "-S", "--show-error", "-sS", "--compressed"):
            pass
        elif token.startswith("-"):
            raise DefinitionError("包含暂不支持的 cURL 选项，请删除该选项或改用表单填写")
        else:
            urls.append(token)
    if len(urls) != 1:
        raise DefinitionError("一次只能导入一个请求 URL")
    try:
        parts = urlsplit(urls[0])
    except ValueError as exc:
        raise DefinitionError("URL 格式无效") from exc
    query = list(parse_qsl(parts.query, keep_blank_values=True))
    request = {
        "method": method
        or ("HEAD" if head else "GET" if get else "POST" if data or encoded else "GET"),
        "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment)),
        "timeout": timeout,
    }
    if headers:
        request["headers"] = headers
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    json_body = force_json or content_type == "application/json" or content_type.endswith("+json")
    if json_body and (data or force_json):
        if get or encoded or len(data) != 1:
            raise DefinitionError("JSON 请求体不能与 -G、表单字段或多段数据混用")
        try:
            request["json"] = json.loads(data[0])
        except (ValueError, IndexError) as exc:
            raise DefinitionError("JSON 请求体无效") from exc
        if force_json:
            headers.setdefault("content-type", "application/json")
            headers.setdefault("accept", "application/json")
            request["headers"] = headers
    elif data or encoded:
        if any(piece and "=" not in piece for piece in "&".join(data).split("&")):
            raise DefinitionError(
                "请求体须为 key=value 表单；JSON 请使用 --json 或声明 Content-Type"
            )
        pairs = []
        for kind, piece in form_parts:
            if kind == "encoded":
                pairs.append(tuple(piece.split("=", 1)))
            else:
                pairs.extend(parse_qsl(piece, keep_blank_values=True))
        if get:
            query.extend(pairs)
        else:
            request["form"] = _pairs(pairs)
    if query:
        request["query"] = _pairs(query)
    draft = {
        "name": "imported_api",
        "description": "请填写接口用途，帮助模型选择此工具。",
        "enabled": False,
        "parameters": {"type": "object", "properties": {}},
        "request": request,
    }
    parse_definitions(json.dumps([draft], allow_nan=False))
    return draft
