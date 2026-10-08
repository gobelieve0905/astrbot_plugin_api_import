import importlib
import sys
import types
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
package = types.ModuleType("api_import_test")
package.__path__ = [str(root)]
sys.modules.setdefault(package.__name__, package)

Module = importlib.import_module("api_import_test.proxy_manager_integration")


class ProxyManagerIntegrationTests(unittest.TestCase):
    def test_declaration_and_lease_are_strict(self):
        manager = types.SimpleNamespace(
            activated=True,
            star_cls=types.SimpleNamespace(
                get_proxy_manager_lease=lambda **kwargs: {
                    "protocol": "astrbot.proxy-manager/v1",
                    "plugin_id": "astrbot_plugin_api_import",
                    "mode": "astrbot-environment",
                    "protocols": ["http", "https"],
                    "http_proxy": "http://127.0.0.1:19000",
                    "https_proxy": "http://127.0.0.1:19000",
                    "revision": "rev-1",
                    "status": "configured",
                }
            ),
        )
        context = types.SimpleNamespace(get_registered_star=lambda name: manager)
        lease = Module.request_lease(context)
        self.assertEqual(
            Module.proxy_for_url(lease, "https://example.test/api"), lease["https_proxy"]
        )
        self.assertEqual(
            Module.proxy_for_url(lease, "http://example.test/api"), lease["http_proxy"]
        )

        bad = dict(lease, https_proxy="http://user:secret@127.0.0.1:19000")
        manager.star_cls.get_proxy_manager_lease = lambda **kwargs: bad
        with self.assertRaises(Module.ProxyManagerError):
            Module.request_lease(context)

    def test_missing_manager_fails_closed(self):
        context = types.SimpleNamespace(get_registered_star=lambda name: None)
        with self.assertRaises(Module.ProxyManagerError):
            Module.request_lease(context)

    def test_management_flag_defaults_off_and_rejects_non_boolean(self):
        self.assertFalse(Module.management_enabled({}))
        with self.assertRaises(Module.ProxyManagerError):
            Module.management_enabled({"manage_egress": "yes"})
