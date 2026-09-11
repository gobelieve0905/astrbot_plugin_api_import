"""Server-owned quick connections; creating definitions never performs network I/O."""

import copy
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
    fields = ["country", "platform", "application"]
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
            }
        ]
    }


def build_connection(payload, existing):
    if payload.get("platform_id") != "applovin_report":
        raise DefinitionError("请选择支持的平台")
    display_name = payload.get("name", "")
    if not isinstance(display_name, str) or len(display_name.strip()) > 80:
        raise DefinitionError("接入名称最多 80 个字符")
    token = payload.get("token")
    if (
        not isinstance(token, str)
        or not token.strip()
        or len(token) > 4096
        or any(c.isspace() for c in token)
    ):
        raise DefinitionError("请填写有效的 Report Key / Token，不要包含空格或换行")
    enabled = payload.get("enabled_operations")
    known = {operation[0] for operation in OPERATIONS}
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
        definitions = [
            _definition(operation, token, suffix, operation[0] in enabled)
            for operation in OPERATIONS
        ]
        if not any(item["name"] in names for item in definitions):
            for item, operation in zip(definitions, OPERATIONS, strict=True):
                item["display_name"] = operation[1]
                item["connection"] = {
                    "id": suffix,
                    "name": display_name.strip() or "AppLovin 账户 " + suffix,
                    "platform": "applovin_report",
                    "operation": operation[0],
                }
            return copy.deepcopy(definitions)
