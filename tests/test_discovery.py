import importlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from discovery_fixture import spec
from test_engine import Definitions, Engine

D = importlib.import_module("api_import_test.discovery")


class ConversionTest(unittest.TestCase):
    def convert(
        self,
        document=None,
        target="https://api.example.test/v1",
        key="synthetic-key",
        override=None,
    ):
        return D.convert_document(
            document or spec(), target, "https://api.example.test/openapi.json", key, override
        )

    def test_methods_parameters_and_key_not_in_model_schema(self):
        results = self.convert()
        self.assertEqual([r["method"] for r in results], ["GET", "POST", "GET", "DELETE"])
        self.assertTrue(all(r["supported"] for r in results), results)
        self.assertTrue(all(not r["definition"]["enabled"] for r in results))
        value = results[0]["definition"]
        self.assertEqual(value["parameters"]["properties"]["limit"]["default"], 20)
        self.assertNotIn("synthetic-key", json.dumps(value["parameters"]))
        self.assertEqual(value["request"]["headers"]["X-API-Key"], "synthetic-key")
        self.assertEqual(value["request"]["query"]["tags"], {"$param": "tags", "$join": ","})

    def test_body_refs_and_readonly_fields(self):
        result = self.convert()[1]["definition"]
        self.assertEqual(result["parameters"]["required"], ["body"])
        self.assertEqual(result["parameters"]["properties"]["body"]["required"], ["name"])
        self.assertNotIn("id", result["parameters"]["properties"]["body"]["properties"])

    def test_specific_target_defaults_and_only_matching_methods(self):
        results = self.convert(target="https://api.example.test/v1/items/42")
        self.assertEqual([r["method"] for r in results], ["GET", "DELETE"])
        self.assertEqual(
            results[0]["definition"]["parameters"]["properties"]["item_id"]["default"], 42
        )
        self.assertTrue(results[0]["definition"]["request"]["url"].endswith("{item_id}"))

    def test_cross_origin_server_does_not_receive_key(self):
        document = spec()
        document["servers"] = [{"url": "https://unrelated.test"}]
        result = self.convert(document)
        self.assertTrue(all(not r["supported"] for r in result))
        self.assertNotIn("synthetic-key", json.dumps(result))

    def test_missing_auth_requires_explicit_key_placement(self):
        document = spec()
        document.pop("security")
        self.assertTrue(all(not r["supported"] for r in self.convert(document)))
        results = self.convert(document, override={"mode": "query", "name": "api_key"})
        self.assertTrue(all(r["supported"] for r in results))
        self.assertEqual(results[0]["definition"]["request"]["query"]["api_key"], "synthetic-key")

    def test_documented_key_parameter(self):
        document = spec()
        document.pop("security")
        document["paths"]["/items"]["parameters"] = [
            {"name": "api_key", "in": "query", "required": True, "schema": {"type": "string"}}
        ]
        result = self.convert(document, target="https://api.example.test/v1/items")[0]
        self.assertTrue(result["supported"], result)
        self.assertNotIn("api_key", result["definition"]["parameters"]["properties"])
        self.assertEqual(result["definition"]["request"]["query"]["api_key"], "synthetic-key")

    def test_security_or_and_oauth_scopes(self):
        document = spec()
        document["components"]["securitySchemes"]["OAuth"] = {"type": "oauth2", "flows": {}}
        document["security"] = [{"Key": [], "Other": []}]
        self.assertTrue(all(not r["supported"] for r in self.convert(document)))
        document["security"] = [{"OAuth": ["items:read"]}]
        result = self.convert(document)[0]
        self.assertIn("items:read", result["auth"])
        self.assertEqual(
            result["definition"]["request"]["headers"]["Authorization"], "Bearer synthetic-key"
        )

    def test_external_and_recursive_refs_are_not_fetched(self):
        for ref in ["https://other.test/schema.json", "#/components/schemas/NewItem"]:
            document = spec()
            document["components"]["schemas"]["NewItem"] = {"$ref": ref}
            result = self.convert(document)[1]
            self.assertFalse(result["supported"])

    def test_swagger_query_key_and_array(self):
        document = {
            "swagger": "2.0",
            "host": "api.example.test",
            "basePath": "/v1",
            "schemes": ["https"],
            "securityDefinitions": {"Key": {"type": "apiKey", "in": "query", "name": "key"}},
            "security": [{"Key": []}],
            "paths": {
                "/items": {
                    "get": {
                        "parameters": [
                            {
                                "name": "ids",
                                "in": "query",
                                "type": "array",
                                "items": {"type": "integer"},
                            }
                        ]
                    }
                }
            },
        }
        result = self.convert(document)[0]
        self.assertTrue(result["supported"])
        self.assertEqual(result["definition"]["request"]["query"]["ids"]["$join"], ",")
        self.assertEqual(result["definition"]["request"]["query"]["key"], "synthetic-key")

    def test_bad_yaml_alias_and_unsupported_version(self):
        for text in [
            "openapi: 3.0.3\npaths: &x {a: *x}",
            '{"openapi":"9.0","paths":{}}',
            "<html>hello</html>",
        ]:
            with self.assertRaises(D.DiscoveryError):
                D.document_from_text(text)
        self.assertEqual(D.document_from_text(json.dumps(spec()))["openapi"], "3.0.3")


class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.response = None

        async def handler(request):
            self.calls.append(request)
            if self.response:
                return self.response(request)
            if request.url.path == "/openapi.json":
                return httpx.Response(200, json=spec())
            return httpx.Response(404)

        self.discovery = D.Discovery(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def asyncTearDown(self):
        await self.discovery.close()

    async def test_discovery_without_key_leak_or_business_calls(self):
        result = await self.discovery.run(
            {"target_url": "https://api.example.test/v1", "api_key": "secret-value"}
        )
        self.assertEqual(len(result["operations"]), 4)
        self.assertTrue(
            all(
                call.method == "GET" and call.url.path.endswith("openapi.json")
                for call in self.calls
            )
        )
        self.assertTrue(
            all("secret-value" not in str(call.url) + str(call.headers) for call in self.calls)
        )

    async def test_allow_does_not_generate_guessed_tools(self):
        self.response = lambda r: (
            httpx.Response(204, headers={"Allow": "GET, POST, DELETE"})
            if r.method == "OPTIONS"
            else httpx.Response(404)
        )
        result = await self.discovery.run(
            {"target_url": "https://api.example.test/report", "api_key": "secret"}
        )
        self.assertEqual(result["operations"], [])
        self.assertEqual(result["methods"], ["GET", "POST", "DELETE"])
        self.assertFalse(any(r.method in ("POST", "DELETE") for r in self.calls))

    async def test_redirects_not_followed(self):
        self.response = lambda r: httpx.Response(
            302, headers={"Location": "https://unrelated.test"}
        )
        result = await self.discovery.run({"target_url": "https://api.example.test/v1"})
        self.assertEqual(result["operations"], [])
        self.assertTrue(all(r.url.host == "api.example.test" for r in self.calls))

    async def test_pasted_document_does_not_request_network(self):
        result = await self.discovery.run(
            {
                "target_url": "https://api.example.test/v1",
                "api_key": "secret",
                "document_text": json.dumps(spec()),
            }
        )
        self.assertEqual(len(result["operations"]), 4)
        self.assertEqual(self.calls, [])

    async def test_query_join_and_optional_body_execute(self):
        results = D.convert_document(spec(), "https://api.example.test/v1", api_key="secret")
        first = results[0]["definition"]
        first["enabled"] = True
        requests = []

        def receive(request):
            requests.append(request)
            return httpx.Response(200, json={})

        with tempfile.TemporaryDirectory() as directory:
            executor = Engine.Executor(
                Path(directory), httpx.AsyncClient(transport=httpx.MockTransport(receive))
            )
            try:
                result = await executor.execute(
                    Definitions.parse_definitions(json.dumps([first]))[0], {"tags": ["one", "two"]}
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(requests[0].url.params["tags"], "one,two")
                body = results[1]["definition"]
                body["enabled"] = True
                body["parameters"]["required"] = []
                result = await executor.execute(
                    Definitions.parse_definitions(json.dumps([body]))[0], {}
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(requests[1].content, b"")
            finally:
                await executor.close()


if __name__ == "__main__":
    unittest.main()
