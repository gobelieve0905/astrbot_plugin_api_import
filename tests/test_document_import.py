"""Ordinary documentation extraction without real model or business requests."""

import importlib
import json
import unittest
from urllib.parse import quote

import httpx
from discovery_fixture import TEXT
from discovery_fixture import ordinary_spec as extracted
from test_discovery import D

M = importlib.import_module(D.__package__ + ".document_import")


class PageTextTest(unittest.TestCase):
    def test_html_preserves_table_code_and_removes_scripts_navigation(self):
        raw = "<html><head>HEAD</head><nav>NAV</nav><main><h1>Documentation</h1>"
        raw += "<p>" + TEXT + "</p><table><tr><td>start</td><td>required</td></tr></table>"
        raw += "<pre>curl https://api.example.test/report</pre><script>INJECTION</script></main></html>"
        text = M.readable_text(raw)
        self.assertIn("curl https://api.example.test/report", text)
        self.assertIn("start | required", text)
        for unwanted in ["HEAD", "NAV", "INJECTION"]:
            self.assertNotIn(unwanted, text)

    def test_size_bounds_and_no_silent_truncation(self):
        for value in ["", "x" * 60001, "x" * 2_000_001]:
            with self.assertRaises(D.DiscoveryError):
                M.readable_text(value)

    def test_unfounded_path_or_method_is_blocked(self):
        for field in ["x-evidence", "x-method-evidence"]:
            data = extracted()
            data["paths"]["/report"]["get"][field] = "invented"
            result = M.grounded_document(json.dumps(data), TEXT, "https://api.example.test")
            operation = D.convert_document(result, "https://api.example.test", api_key="secret")[0]
            self.assertFalse(operation["supported"])

    def test_model_prose_and_thinking_wrappers(self):
        body = json.dumps(extracted())
        for raw in [
            "以下为接口定义：\n```json\n" + body + "\n```\n请核对。",
            "<think>reasoning</think>" + body,
        ]:
            parsed = M.grounded_document(raw, TEXT, "https://api.example.test")
            self.assertIn("/report", parsed["paths"])
        for raw in [
            "",
            "<think>no answer</think>",
            body[:-4],
            body + json.dumps({**extracted(), "info": {"title": "other", "version": "2"}}),
        ]:
            with self.assertRaises(D.DiscoveryError):
                M.grounded_document(raw, TEXT, "https://api.example.test")

    def test_unfounded_auth_rejected_and_warning_preserved(self):
        data = extracted()
        data["components"]["securitySchemes"]["Key"]["x-evidence"] = "invented"
        with self.assertRaises(D.DiscoveryError):
            M.grounded_document(json.dumps(data), TEXT, "https://api.example.test")
        data = extracted()
        data["paths"]["/report"]["get"]["x-import-warning"] = "必填请求内容不明确"
        result = M.grounded_document(json.dumps(data), TEXT, "https://api.example.test")
        self.assertFalse(
            D.convert_document(result, "https://api.example.test", api_key="secret")[0]["supported"]
        )


class OrdinaryDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.prompts = []
        self.reply = json.dumps(extracted())
        self.page = "<html><main><h1>Report API</h1><pre>" + TEXT + "</pre></main></html>"
        self.status = 200

        def fetch(request):
            self.requests.append(request)
            return httpx.Response(self.status, text=self.page)

        async def reader(prompt, system):
            self.prompts.append((prompt, system))
            return self.reply

        self.discovery = D.Discovery(
            httpx.AsyncClient(transport=httpx.MockTransport(fetch)), reader
        )

    async def asyncTearDown(self):
        await self.discovery.close()

    async def test_public_webpage_generates_disabled_draft_with_local_key(self):
        result = await self.discovery.run(
            {
                "target_url": "https://api.example.test",
                "api_key": "PRIVATE_KEY",
                "document_url": "https://docs.example.test/reporting-guide",
            }
        )
        self.assertTrue(result["inferred"])
        operation = result["operations"][0]
        self.assertTrue(operation["supported"])
        definition = operation["definition"]
        self.assertFalse(definition["enabled"])
        self.assertEqual(definition["request"]["query"]["api_key"], "PRIVATE_KEY")
        self.assertNotIn("PRIVATE_KEY", str(self.prompts))
        self.assertNotIn("api_key", definition["parameters"]["properties"])
        self.assertEqual(definition["parameters"]["required"], ["start"])
        self.assertNotIn("default", definition["parameters"]["properties"]["start"])
        self.assertEqual(
            [(r.method, str(r.url)) for r in self.requests],
            [("GET", "https://docs.example.test/reporting-guide")],
        )

    async def test_pasted_text_and_encoded_keys_are_redacted_no_network(self):
        key = "secret/+value"
        result = await self.discovery.run(
            {
                "target_url": "https://api.example.test?api_key=" + quote(key, safe=""),
                "api_key": key,
                "document_text": TEXT + key + " " + quote(key, safe=""),
            }
        )
        self.assertTrue(result["inferred"])
        self.assertEqual(self.requests, [])
        self.assertNotIn(key, str(self.prompts))
        self.assertNotIn(quote(key, safe=""), str(self.prompts))

    async def test_no_model_clear_error(self):
        self.discovery.reader = None
        with self.assertRaisesRegex(D.DiscoveryError, "文本模型"):
            await self.discovery.run(
                {"target_url": "https://api.example.test", "document_text": TEXT}
            )

    async def test_invalid_model_output_and_cross_origin(self):
        self.reply = "not JSON"
        with self.assertRaises(D.DiscoveryError):
            await self.discovery.run(
                {"target_url": "https://api.example.test", "document_text": TEXT}
            )
        data = extracted()
        data["servers"] = [{"url": "https://other.test"}]
        self.reply = json.dumps(data)
        result = await self.discovery.run(
            {"target_url": "https://api.example.test", "document_text": TEXT, "api_key": "secret"}
        )
        self.assertFalse(result["operations"][0]["supported"])
        self.assertNotIn("secret", json.dumps(result))

    async def test_blocked_document_and_malformed_spec_do_not_call_model(self):
        self.status = 403
        with self.assertRaisesRegex(D.DiscoveryError, "无法读取"):
            await self.discovery.run(
                {
                    "target_url": "https://api.example.test",
                    "document_url": "https://docs.example.test",
                }
            )
        with self.assertRaises(D.DiscoveryError):
            await self.discovery.run(
                {"target_url": "https://api.example.test", "document_text": '{"openapi": invalid'}
            )
        self.assertEqual(self.prompts, [])
