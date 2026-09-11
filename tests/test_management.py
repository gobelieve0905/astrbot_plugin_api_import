import copy
import importlib
import json
import unittest

from test_engine import Definitions

CatalogModule = importlib.import_module("api_import_test.catalog")
Importing = importlib.import_module("api_import_test.importing")


def sample(name="sample"):
    return {
        "name": name,
        "description": "测试",
        "request": {"method": "GET", "url": "https://example.test"},
    }


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.config = {"tools_json": "[]"}
        self.applied = []

        def apply(raw, definitions):
            self.config["tools_json"] = raw
            self.applied.append(definitions)

        self.catalog = CatalogModule.Catalog(self.config, apply)

    def save(self, value, original=None):
        return self.catalog.mutate(
            "save",
            {
                "revision": self.catalog.snapshot()["revision"],
                "original_name": original,
                "definition": value,
            },
        )

    def test_create_rename_disable_delete(self):
        self.save(sample())
        self.save({**sample("renamed"), "enabled": False}, "sample")
        self.assertEqual(self.catalog.snapshot()["items"][0]["name"], "renamed")
        self.assertFalse(self.applied[-1][0].enabled)
        result = self.catalog.mutate(
            "delete", {"revision": self.catalog.snapshot()["revision"], "name": "renamed"}
        )
        self.assertEqual(result["items"], [])

    def test_stale_revision_cannot_overwrite(self):
        old = self.catalog.snapshot()["revision"]
        self.save(sample())
        with self.assertRaises(CatalogModule.ConflictError):
            self.catalog.mutate("delete", {"revision": old, "name": "sample"})
        self.assertEqual(len(self.catalog.snapshot()["items"]), 1)

    def test_invalid_duplicate_and_missing_edit_are_atomic(self):
        self.save(sample())
        previous = copy.deepcopy(self.config)
        for value, original in [
            (sample(), None),
            ({**sample(), "name": "bad name"}, "sample"),
            (sample(), "missing"),
        ]:
            with self.assertRaises(Definitions.DefinitionError):
                self.save(value, original)
            self.assertEqual(self.config, previous)

    def test_existing_configuration_is_preserved(self):
        self.config["tools_json"] = json.dumps([sample("existing")])
        self.save(sample("new"))
        self.assertEqual(
            [item["name"] for item in self.catalog.snapshot()["items"]], ["existing", "new"]
        )

    def test_invalid_legacy_data_not_replaced(self):
        self.config["tools_json"] = "not json"
        self.assertIsNotNone(self.catalog.snapshot()["error"])
        with self.assertRaises(Definitions.DefinitionError):
            self.save(sample())
        self.assertEqual(self.config["tools_json"], "not json")

    def test_batch_and_permissions_are_atomic(self):
        revision = self.catalog.snapshot()["revision"]
        state = self.catalog.mutate(
            "batch",
            {
                "revision": revision,
                "definitions": [sample("get"), {**sample("post"), "enabled": False}],
            },
        )
        state = self.catalog.mutate(
            "permissions", {"revision": state["revision"], "enabled_names": ["post"]}
        )
        self.assertEqual([item["enabled"] for item in state["items"]], [False, True])
        before = copy.deepcopy(self.config)
        for action, payload in [
            ("batch", {"definitions": [sample("get")]}),
            ("permissions", {"enabled_names": ["missing"]}),
        ]:
            with self.assertRaises(Definitions.DefinitionError):
                self.catalog.mutate(action, {"revision": state["revision"], **payload})
            self.assertEqual(before, self.config)
        with self.assertRaises(CatalogModule.ConflictError):
            self.catalog.mutate("permissions", {"revision": revision, "enabled_names": []})

    def test_failed_apply_does_not_claim_success(self):
        def fail(raw, definitions):
            raise OSError("disk failure")

        self.catalog.apply = fail
        with self.assertRaises(OSError):
            self.save(sample())
        self.assertEqual(self.config["tools_json"], "[]")


class CurlTest(unittest.TestCase):
    def test_get_query_repeated_blank_and_header(self):
        result = Importing.import_curl(
            "curl 'https://example.test/items?tag=a&tag=b&empty=' -H 'X-Test: hello'"
        )
        self.assertFalse(result["enabled"])
        self.assertEqual(result["request"]["query"], {"tag": ["a", "b"], "empty": ""})
        self.assertEqual(result["request"]["headers"], {"x-test": "hello"})

    def test_json_multiline_and_explicit_method(self):
        result = Importing.import_curl(
            'curl \\\n --url https://example.test \\\n -XPATCH --json \'{"amount":2,"active":false}\''
        )
        self.assertEqual(result["request"]["method"], "PATCH")
        self.assertEqual(result["request"]["json"], {"amount": 2, "active": False})

    def test_form_and_get_data(self):
        result = Importing.import_curl(
            "curl https://example.test -d 'a=hello+world&a=2' --data-urlencode 'text=中 文'"
        )
        self.assertEqual(result["request"]["form"], {"a": ["hello world", "2"], "text": "中 文"})
        result = Importing.import_curl("curl -G https://example.test --data-urlencode 'text=中 文'")
        self.assertEqual(result["request"]["method"], "GET")
        self.assertEqual(result["request"]["query"], {"text": "中 文"})

    def test_unsupported_inputs_do_not_execute(self):
        for text in [
            "echo hi",
            "curl https://example.test ; touch /tmp/never",
            "curl https://example.test -d @/etc/passwd",
            "curl -K /tmp/config https://example.test",
            "curl -L https://example.test",
            "curl https://one.test https://two.test",
            "curl https://example.test -H",
            "curl 'unterminated",
            "curl https://example.test -F 'file=@a'",
            "curl https://example.test --data-urlencode '@secret'",
            "curl https://example.test --json '{bad}'",
        ]:
            with self.subTest(text=text), self.assertRaises(Definitions.DefinitionError):
                Importing.import_curl(text)

    def test_interleaved_form_order(self):
        result = Importing.import_curl(
            "curl https://example.test -d 'a=1' --data-urlencode 'a=2' -d 'a=3'"
        )
        self.assertEqual(result["request"]["form"]["a"], ["1", "2", "3"])

    def test_content_length_recomputed(self):
        result = Importing.import_curl(
            "curl https://example.test -H 'Content-Length: 999' --json '{}'"
        )
        self.assertNotIn("content-length", result["request"]["headers"])

    def test_empty_json_body_and_null(self):
        self.assertIsNone(
            Importing.import_curl("curl https://example.test --json null")["request"]["json"]
        )
        self.assertEqual(
            Importing.import_curl("curl https://example.test --json '{}' ")["request"]["json"], {}
        )

    def test_quoted_shell_text_is_literal(self):
        result = Importing.import_curl(
            'curl https://example.test --json \'{"text":"$(touch /tmp/never)"}\''
        )
        self.assertEqual(result["request"]["json"]["text"], "$(touch /tmp/never)")


if __name__ == "__main__":
    unittest.main()
