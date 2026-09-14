"""Server-owned quick connections; creating definitions never performs network I/O."""

import copy
import re
import secrets

from .definitions import DefinitionError

BASE = "https://r.applovin.com"
REPORT_DOC = "https://support.applovin.com/en/growth/promoting-your-apps/api/reporting-api"
MAX_DOC = "https://support.applovin.com/en/max/reporting-apis/revenue-reporting-api"
COHORT_DOC = "https://support.applovin.com/en/max/reporting-apis/cohort-api"
OPERATIONS = (
    (
        "advertiser",
        "广告投放报表",
        "/report",
        "day,campaign,impressions,clicks,conversions,cost",
        REPORT_DOC,
    ),
    (
        "publisher",
        "发布商报表",
        "/report",
        "day,application,platform,impressions,clicks,revenue",
        REPORT_DOC,
    ),
    (
        "max_revenue",
        "MAX 收益报表",
        "/maxReport",
        "day,application,platform,impressions,estimated_revenue",
        MAX_DOC,
    ),
    (
        "cohort_revenue",
        "Cohort 收益报表",
        "/maxCohort",
        "day,application,installs,pub_revenue_0,rpi_0",
        COHORT_DOC,
    ),
    (
        "cohort_impressions",
        "Cohort 展示报表",
        "/maxCohort/imp",
        "day,application,installs,imp_0,imp_per_user_0",
        COHORT_DOC,
    ),
    (
        "cohort_sessions",
        "Cohort 会话与留存报表",
        "/maxCohort/session",
        "day,application,installs,session_count_0,retention_1",
        COHORT_DOC,
    ),
)


def _definition(operation, token, suffix, enabled):
    operation_id, title, path, columns, docs = operation
    properties = {
        "start": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "UTC 开始日期 YYYY-MM-DD，最近 45 天内。",
        },
        "end": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "UTC 结束日期 YYYY-MM-DD，包含当天，不早于 start，最近 45 天内。",
        },
        "columns": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
            "minItems": 1,
            "maxItems": 60,
            "uniqueItems": True,
            "default": columns.split(","),
            "description": f"官方报表字段名。可替换默认维度和指标，字段组合限制见 {docs}。",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 1000,
            "description": "单页行数；返回满页时用 offset 继续，不能把单页当总量。",
        },
        "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "分页偏移量。"},
        "not_zero": {
            "type": "integer",
            "enum": [0, 1],
            "description": "设为 1 排除数值指标全部为零的行。",
        },
    }
    # Common documented filters remain ordinary named parameters, never arbitrary query injection.
    fields = ["country", "platform"]
    if operation_id != "advertiser":
        fields += ["application"]
    if operation_id == "advertiser":
        fields += ["campaign", "campaign_id_external", "campaign_package_name"]
    else:
        fields += ["package_name"]
    if operation_id == "max_revenue":
        fields += ["network", "max_ad_unit_id", "ad_format"]
    for field in fields:
        properties["filter_" + field] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "pattern": "^[^,]+$"},
            "minItems": 1,
            "maxItems": 100,
            "description": f"按 {field} 精确筛选，多值为 OR；值需符合平台字段定义。",
        }
    if operation_id == "advertiser":
        properties["filter_campaign_package_name"]["description"] = (
            "被推广应用的真实包名或 Bundle ID，使用字符串数组。不要猜测包名；未知时先不加筛选，查询 campaign 和 campaign_package_name 确认。"
        )
        properties["filter_campaign"]["description"] = (
            "广告系列名称精确匹配，使用字符串数组；不是游戏显示名称。"
        )
    properties["sort_day"] = {
        "type": "string",
        "enum": ["ASC", "DESC"],
        "description": "按日期排序。",
    }
    if path == "/report":
        properties["day_column"] = {
            "type": "string",
            "enum": ["day"],
            "description": "仅在需要 cohort 口径时设为 day；缺省为实时指标口径。",
        }
        properties["having"] = {
            "type": "string",
            "maxLength": 2000,
            "description": "数值条件，例如 impressions > 0；填写原文，由客户端编码。可能增加耗时。",
        }
    query = {
        name: {"$param": name, **({"$join": ","} if schema["type"] == "array" else {})}
        for name, schema in properties.items()
    }
    query.update(api_key=token, format="json")
    if path == "/report":
        query["report_type"] = operation_id
    return {
        "name": f"applovin_{operation_id}_{suffix}",
        "description": f"AppLovin {title}（接入 {suffix}），只读 GET。UTC 日期最近 45 天；日期和报表字段在调用时填写。检查响应业务状态，不能把报错或单页当成完整数据；分页需继续调用。字段说明：{docs}",
        "enabled": enabled,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        "request": {"method": "GET", "url": BASE + path, "query": query, "timeout": 60},
        "response": {"preview_chars": 12000, "max_bytes": 5242880, "save": True},
    }


def platform_catalog():
    from .meta_platform import platform_entry

    return {
        "platforms": [
            {
                "id": "applovin_report",
                "name": "AppLovin Report",
                "token_label": "Report Key / Token",
                "description": "填写 AppLovin 后台账户 → Keys 中的 Report Key。逐项开启允许调用的报表，全部为只读 GET。日期、维度、筛选和分页在调用时提供；开关不会改变平台账号权限。",
                "operations": [
                    {
                        "id": operation[0],
                        "title": operation[1],
                        "method": "GET",
                        "path": operation[2],
                        "description": "默认字段：" + operation[3],
                        "documentation": operation[4],
                    }
                    for operation in OPERATIONS
                ],
            },
            platform_entry(),
        ]
    }


def build_connection(payload, existing):
    from . import meta_platform

    is_meta = payload.get("platform_id") == meta_platform.PLATFORM
    if payload.get("platform_id") not in ("applovin_report", meta_platform.PLATFORM):
        raise DefinitionError("请选择支持的平台")
    display_name = payload.get("name", "")
    if not isinstance(display_name, str) or len(display_name.strip()) > 80:
        raise DefinitionError("接入名称最多 80 个字符")
    call_name = payload.get("call_name")
    if call_name is not None and (
        not isinstance(call_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z_]{0,39}", call_name)
    ):
        raise DefinitionError("调用名称须为 1–40 位英文字母或下划线，并以英文字母开头")
    token = payload.get("token")
    if (
        not isinstance(token, str)
        or not token.strip()
        or len(token) > 4096
        or any(c.isspace() for c in token)
    ):
        raise DefinitionError("请填写有效的 Token，不要包含空格或换行")
    enabled = payload.get("enabled_operations")
    known = (
        set(meta_platform.operations()) if is_meta else {operation[0] for operation in OPERATIONS}
    )
    if (
        not isinstance(enabled, list)
        or any(not isinstance(item, str) for item in enabled)
        or len(enabled) != len(set(enabled))
        or not set(enabled).issubset(known)
    ):
        raise DefinitionError("操作开关无效，请刷新后重试")
    names = {item["name"] for item in existing}
    while True:
        suffix = secrets.token_hex(4)
        if is_meta:
            definitions = [
                meta_platform.definition(op, token, suffix, op["id"] in enabled)
                for op in meta_platform.operations().values()
            ]
            descriptors = [
                (op["id"], meta_platform.title(op)) for op in meta_platform.operations().values()
            ]
        else:
            definitions = [
                _definition(operation, token, suffix, operation[0] in enabled)
                for operation in OPERATIONS
            ]
            descriptors = OPERATIONS
        if not any(item["name"] in names for item in definitions):
            for item, operation in zip(definitions, descriptors, strict=True):
                item["display_name"] = operation[1]
                item["connection"] = {
                    "id": suffix,
                    "name": display_name.strip()
                    or ("Meta 账户 " if is_meta else "AppLovin 账户 ") + suffix,
                    "platform": payload["platform_id"],
                    "operation": operation[0],
                }
            if call_name:
                for item in definitions:
                    item["connection"]["call_name"] = call_name
            return copy.deepcopy(definitions)


def upgrade_advertiser(items):
    """Repair only recognizable preset fields; keep credentials and custom mappings intact."""
    items = copy.deepcopy(items)
    if not isinstance(items, list):
        return items
    for item in items:
        if not isinstance(item, dict):
            continue
        req = item.get("request", {})
        if (
            not isinstance(req, dict)
            or req.get("url") != BASE + "/report"
            or req.get("method") != "GET"
        ):
            continue
        query = req.get("query", {})
        if not isinstance(query, dict) or query.get("report_type") != "advertiser":
            continue
        if not re.fullmatch(r"applovin_advertiser_[0-9a-f]{8}", str(item.get("name", ""))):
            continue
        params = item.get("parameters", {})
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        if not isinstance(props, dict):
            continue
        old = props.get("filter_application", {})
        if (
            isinstance(old, dict)
            and "default" not in old
            and old.get("description")
            == "按 application 精确筛选，多值为 OR；值需符合平台字段定义。"
            and query.get("filter_application") == {"$param": "filter_application", "$join": ","}
            and "filter_application" not in params.get("required", [])
        ):
            del props["filter_application"]
            del query["filter_application"]
        descriptions = {
            "filter_campaign_package_name": "被推广应用的真实包名或 Bundle ID，使用字符串数组。不要猜测包名；未知时先不加筛选，查询 campaign 和 campaign_package_name 确认。",
            "filter_campaign": "广告系列名称精确匹配，使用字符串数组；不是游戏显示名称。",
        }
        for key, description in descriptions.items():
            field = props.get(key)
            if (
                isinstance(field, dict)
                and field.get("description")
                == f"按 {key[7:]} 精确筛选，多值为 OR；值需符合平台字段定义。"
            ):
                field["description"] = description
    return items
