"""Bounded local windows of already-authorized Meta responses; no arbitrary file access."""

import copy
import hashlib
import json

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "columns": {"type": "array", "maxItems": 100, "items": {"type": "string"}},
    },
    "description": "读取本工具已返回 result_id 的本地数据窗口。保留原 node_id，仅传 result_page，不传 fields/params；不会请求 Meta。offset 默认为 0，limit 默认为 20。",
}


def supported(definition):
    return definition.connection.get("platform") == "meta_marketing" and definition.connection.get(
        "operation"
    ) in {"get_insights", "get_adaccounts"}


def tool_parameters(definition):
    params = copy.deepcopy(definition.parameters)
    if supported(definition):
        params["properties"]["result_page"] = copy.deepcopy(SCHEMA)
    return params


def window(text, options):
    data = json.loads(text)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("此结果没有可分段读取的数据行")
    offset, limit = options.get("offset", 0), options.get("limit", 20)
    if offset > len(rows):
        raise ValueError("读取起点超出结果行数")
    columns = options.get("columns")
    if columns is not None and (
        not columns or set(columns) - {k for r in rows if isinstance(r, dict) for k in r}
    ):
        raise ValueError("请选择当前结果中实际存在的字段")
    output, size = [], 0
    for row in rows[offset : offset + limit]:
        if columns is not None:
            row = {k: v for k, v in row.items() if k in columns}
        count = len(json.dumps(row, ensure_ascii=False))
        if size + count > 16000:
            if not output:
                raise ValueError("单行过大，请用 result_page.columns 选择较少字段")
            break
        output.append(row)
        size += count
    end = offset + len(output)
    return {
        "data": output,
        "total_rows": len(rows),
        "offset": offset,
        "next_offset": end if end < len(rows) else None,
        "window_complete": end == len(rows),
        "source_scope": "仅此 result_id 对应的单次 HTTP 响应；读取窗口不代表已查全所有账户或远端分页",
    }


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()
