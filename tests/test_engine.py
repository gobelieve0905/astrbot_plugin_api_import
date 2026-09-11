import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
package = types.ModuleType("api_import_test")
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
Definitions = importlib.import_module("api_import_test.definitions")
Engine = importlib.import_module("api_import_test.engine")


def definition(**changes):
    value = {
        "name": "sample",
        "description": "测试工具",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "default": 2},
                "active": {"type": "boolean"},
            },
            "required": ["id"],
        },
        "request": {"method": "GET", "url": "https://example.test/items/{id}"},
    }
    value.update(changes)
    return Definitions.parse_definitions(json.dumps([value]))[0]


class DefinitionsTest(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(
            len(Definitions.parse_definitions((root / "examples/tools.json").read_text())), 2
        )

    def test_invalid_configurations(self):
        cases = ["{}", "[null]", "[NaN]", "[{]", json.dumps([{"name": "x", "request": {}}])]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(Definitions.DefinitionError):
                Definitions.parse_definitions(raw)

    def test_invalid_definitions(self):
        cases = [
            {"request": {"method": "GET", "url": "https://{id}.test/"}},
            {"request": {"method": "GET", "url": "https://example.test/{missing}"}},
            {"request": {"method": "POST", "url": "https://example.test", "json": {}, "form": {}}},
            {
                "request": {
                    "method": "GET",
                    "url": "https://example.test",
                    "query": {"x": {"$param": "missing"}},
                }
            },
            {
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"$ref": "https://schema.test"}},
                }
            },
            {
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "integer", "default": "bad"}},
                }
            },
            {"response": {"pointer": "/bad~2"}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Definitions.DefinitionError):
                definition(**case)

    def test_duplicate_names(self):
        item = {
            "name": "x",
            "description": "x",
            "request": {"method": "GET", "url": "https://a.test"},
        }
        with self.assertRaises(Definitions.DefinitionError):
            Definitions.parse_definitions(json.dumps([item, item]))


class ExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.calls = []
        self.response = httpx.Response(200, json={"data": [{"id": 1}]})

        async def handle(request):
            self.calls.append(request)
            return self.response

        self.executor = Engine.Executor(
            Path(self.directory.name), httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    async def asyncTearDown(self):
        await self.executor.close()
        self.directory.cleanup()

    async def test_path_defaults_query_and_extraction(self):
        item = definition(
            request={
                "method": "GET",
                "url": "https://example.test/items/{id}?fixed=yes",
                "query": {"n": {"$param": "count"}, "active": {"$param": "active"}},
            },
            response={"pointer": "/data/0/id"},
        )
        result = await self.executor.execute(item, {"id": "a/b ?"})
        self.assertEqual(result["data"], 1)
        self.assertIn(b"a%2Fb%20%3F", self.calls[0].url.raw_path)
        self.assertEqual(dict(self.calls[0].url.params), {"fixed": "yes", "n": "2"})

    async def test_json_types_and_optional_omission(self):
        item = definition(
            request={
                "method": "PATCH",
                "url": "https://example.test/{id}",
                "json": {
                    "n": {"$param": "count"},
                    "active": {"$param": "active"},
                    "nested": {"v": None},
                },
            }
        )
        result = await self.executor.execute(item, {"id": "1", "active": False})
        self.assertTrue(result["ok"])
        self.assertEqual(
            json.loads(self.calls[0].content), {"n": 2, "active": False, "nested": {"v": None}}
        )

    async def test_form_encoding(self):
        item = definition(
            request={
                "method": "POST",
                "url": "https://example.test/{id}",
                "form": {"n": {"$param": "count"}, "tags": ["a", "b"]},
            }
        )
        result = await self.executor.execute(item, {"id": "1"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0].content, b"n=2&tags=a&tags=b")

    async def test_invalid_arguments_do_not_send(self):
        for arguments in (
            {},
            {"id": "1", "count": True},
            {"id": "1", "count": 0},
            {"id": "1", "extra": 1},
            {"id": ".."},
        ):
            self.assertFalse((await self.executor.execute(definition(), arguments))["ok"])
        self.assertEqual(self.calls, [])

    async def test_errors_and_redirects_not_retried(self):
        for status in (302, 401, 429, 500):
            self.response = httpx.Response(
                status, headers={"Location": "https://other.test"}, text="secret"
            )
            result = await self.executor.execute(definition(), {"id": "1"})
            self.assertFalse(result["ok"])
            self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(self.calls), 4)

    async def test_response_limit_and_missing_pointer(self):
        self.assertFalse(
            (await self.executor.execute(definition(response={"max_bytes": 1}), {"id": "1"}))["ok"]
        )
        self.assertFalse(
            (
                await self.executor.execute(
                    definition(response={"pointer": "/missing"}), {"id": "1"}
                )
            )["ok"]
        )

    async def test_save_full_response_and_preview(self):
        self.response = httpx.Response(200, json={"data": "中" * 500})
        result = await self.executor.execute(
            definition(response={"preview_chars": 100, "pointer": "/data", "save": True}),
            {"id": "1"},
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(json.loads(Path(result["file"]).read_text()), {"data": "中" * 500})
        self.assertEqual(len(result["preview"]), 100)
        self.assertNotIn("中", json.dumps(list(self.executor.history), ensure_ascii=False))

    async def test_empty_and_text(self):
        self.response = httpx.Response(204)
        self.assertIsNone((await self.executor.execute(definition(), {"id": "1"}))["data"])
        self.response = httpx.Response(200, text="plain")
        self.assertEqual((await self.executor.execute(definition(), {"id": "1"}))["data"], "plain")

    async def test_timeout_is_reported_once(self):
        async def timeout(request):
            self.calls.append(request)
            raise httpx.ReadTimeout("sensitive URL must not leak")

        await self.executor.client.aclose()
        self.executor.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        result = await self.executor.execute(definition(), {"id": "1"})
        self.assertFalse(result["ok"])
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(len(self.calls), 1)

    async def test_closed_and_disabled(self):
        self.assertFalse(
            (await self.executor.execute(definition(enabled=False), {"id": "1"}))["ok"]
        )
        await self.executor.close()
        self.assertFalse((await self.executor.execute(definition(), {"id": "1"}))["ok"])
        self.assertEqual(self.calls, [])

    async def test_null_json_body(self):
        item = definition(request={"method": "POST", "url": "https://example.test", "json": None})
        result = await self.executor.execute(item, {"id": "1"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0].content, b"null")
        self.assertEqual(self.calls[0].headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
