"""Public AstrBot proxy-manager lease handling for this plugin."""

from collections.abc import Mapping
from urllib.parse import urlsplit

PROTOCOL = "astrbot.proxy-manager/v1"
PLUGIN_ID = "astrbot_plugin_api_import"
MANAGER_ID = "astrbot_plugin_proxy_manage"
SUPPORTED_PROTOCOLS = ("http", "https")


class ProxyManagerError(ValueError):
    """A configured proxy-manager route is unavailable or invalid."""


def management_enabled(config) -> bool:
    value = config.get("manage_egress", False)
    if not isinstance(value, bool):
        raise ProxyManagerError("代理管理中心接入开关无效，请修正插件配置")
    return value


def request_lease(context) -> dict:
    try:
        registered = context.get_registered_star(MANAGER_ID)
    except Exception:
        registered = None
    if (
        not registered
        or not getattr(registered, "activated", False)
        or not getattr(registered, "star_cls", None)
    ):
        raise ProxyManagerError("代理管理中心未启用，受管理 API 请求已停止")

    get_lease = getattr(registered.star_cls, "get_proxy_manager_lease", None)
    if not callable(get_lease):
        raise ProxyManagerError("代理管理中心不支持插件接入协议，请更新后重载")
    try:
        lease = get_lease(
            plugin_id=PLUGIN_ID,
            enabled=True,
            protocols=list(SUPPORTED_PROTOCOLS),
        )
    except Exception:
        raise ProxyManagerError("代理管理中心入口不可用或策略未应用，API 请求已停止") from None

    protocols = lease.get("protocols") if isinstance(lease, Mapping) else None
    if (
        not isinstance(lease, Mapping)
        or lease.get("protocol") != PROTOCOL
        or lease.get("plugin_id") != PLUGIN_ID
        or lease.get("mode") != "astrbot-environment"
        or lease.get("status") != "configured"
        or not isinstance(lease.get("revision"), str)
        or not lease.get("revision")
        or not isinstance(protocols, (list, tuple))
        or any(not isinstance(item, str) for item in protocols)
        or not set(SUPPORTED_PROTOCOLS).issubset(protocols)
    ):
        raise ProxyManagerError("代理管理中心返回的接入授权无效，API 请求已停止")

    result = dict(lease)
    for key in ("http_proxy", "https_proxy"):
        value = result.get(key)
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "http"
                and bool(parsed.hostname)
                and parsed.port is not None
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ProxyManagerError("代理管理中心返回的 HTTP 入口无效，API 请求已停止")
    return result


def proxy_for_url(lease: Mapping, url: str) -> str:
    scheme = urlsplit(url).scheme.lower()
    if scheme == "http":
        proxy = lease["http_proxy"]
    elif scheme == "https":
        proxy = lease["https_proxy"]
    else:
        raise ProxyManagerError("受管理 API 仅允许 HTTP 或 HTTPS 请求")
    return proxy
