"""Presentation-only groupings of the pinned SDK; never used for authorization."""

GROUPS = {
    "advertising": ("广告账户与投放", "广告账户、广告系列、广告组和广告对象的查询与管理。"),
    "reporting": ("报表与实验", "效果统计、分析报表、实验和投放诊断。"),
    "creative": ("创意与媒体", "图片、视频、广告创意和相关媒体资产。"),
    "audience": ("受众与定位", "自定义受众、相似受众、定位和受众估算。"),
    "measurement": ("像素与转化", "像素、事件、转化及离线数据来源。"),
    "business": ("商务资产与权限", "企业资产归属、合作伙伴、用户和资产分配。"),
    "commerce": ("商品目录与商务", "商品、目录、库存、商店及商务相关资产。"),
    "social": ("主页与社交内容", "主页、帖子、评论、Instagram 和品牌合作内容。"),
    "messaging": ("消息与 WhatsApp", "消息、会话、WhatsApp 账户和模板。"),
    "generic": ("通用对象", "按对象 ID 查询、修改或删除；多个 SDK 对象共享这一权限项。"),
    "other": ("其他 SDK 接口", "未归入上述类别的接口，请结合适用对象和官方参考确认用途。"),
}
EXACT = {
    "adaccounts": ("advertising", "广告账户", "当前对象关联的广告账户。"),
    "campaigns": ("advertising", "广告系列", "当前账户关联的广告系列。"),
    "adsets": ("advertising", "广告组", "当前对象关联的广告组。"),
    "ads": ("advertising", "广告", "当前对象关联的广告。"),
    "insights": ("reporting", "效果报表", "查询当前对象的效果报表；指标由 fields 和请求参数决定。"),
    "ab_tests": ("reporting", "A/B 测试", "当前对象关联的 A/B 测试。"),
    "account_controls": (
        "advertising",
        "账户控制设置",
        "当前账户的控制设置；支持字段以官方接口为准。",
    ),
    "branded_content_advertisable_medias": (
        "social",
        "可投放的品牌合作媒体",
        "当前对象关联的可用于广告的品牌合作媒体。",
    ),
    "branded_content_media": ("social", "品牌合作媒体", "当前对象关联的品牌合作媒体内容。"),
    "branded_content_partner_promote": ("social", "品牌合作伙伴推广", "品牌合作伙伴推广相关接口。"),
    "branded_content_tag_approval": (
        "social",
        "品牌合作标记审批",
        "品牌合作内容标记审批相关设置。",
    ),
    "broadtargetingcategories": ("audience", "广泛定位类别", "当前对象提供的广泛定位类别。"),
}
RULES = (
    ("messaging", ("whatsapp", "message", "messaging", "conversation")),
    (
        "commerce",
        (
            "catalog",
            "product",
            "commerce",
            "inventory",
            "hotel",
            "vehicle",
            "home_listing",
            "shop",
            "order",
        ),
    ),
    ("measurement", ("pixel", "conversion", "offline", "dataset", "event_source")),
    ("audience", ("audience", "targeting", "reachestimate", "delivery_estimate")),
    ("reporting", ("insight", "report", "experiment", "ab_test", "study", "diagnostic")),
    ("creative", ("creative", "image", "video", "playable", "thumbnail")),
    (
        "social",
        ("branded_content", "instagram", "comment", "feed", "post", "photo", "page", "album"),
    ),
    ("advertising", ("campaign", "adset", "adaccount", "ad_account", "adpreview", "ad_", "ads")),
    (
        "business",
        (
            "business",
            "partner",
            "assigned",
            "permission",
            "system_user",
            "owned",
            "client",
            "asset",
        ),
    ),
)


def describe(op):
    edge = op["path"].strip("/")
    if not edge:
        category, label, explanation = "generic", "通用对象", GROUPS["generic"][1]
    elif edge in EXACT:
        category, label, explanation = EXACT[edge]
    else:
        category = next(
            (group for group, words in RULES if any(word in edge.lower() for word in words)), None
        )
        if category is None:
            sources = " ".join(s["object"].lower() for s in op["sources"])
            category = next(
                (
                    group
                    for group, words in RULES
                    if any(word.replace("_", "") in sources for word in words)
                ),
                "other",
            )
        label = edge
        explanation = f"当前对象的 {edge} 接口。" + GROUPS[category][1]
    return {
        "category": category,
        "category_label": GROUPS[category][0],
        "category_description": GROUPS[category][1],
        "explanation": explanation,
        "label": label,
    }
