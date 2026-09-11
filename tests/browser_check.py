"""Browser checks against a disposable local catalog; never calls a business API.

Run separately with playwright installed, e.g. python -B tests/browser_check.py.
"""

import asyncio
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from discovery_fixture import TEXT, ordinary_spec, spec
from playwright.sync_api import sync_playwright
from test_engine import Definitions, root

Catalog = importlib.import_module("api_import_test.catalog").Catalog
ConflictError = importlib.import_module("api_import_test.catalog").ConflictError
import_curl = importlib.import_module("api_import_test.importing").import_curl
DiscoveryModule = importlib.import_module("api_import_test.discovery")
config = {"tools_json": "[]"}


def apply(raw, definitions):
    config["tools_json"] = raw


catalog = Catalog(config, apply)
fixture_bridge = """<script>
window.AstrBotPluginPage = {
 ready: async () => ({}),
 apiGet: async (path) => (await fetch('/fixture/' + path)).json(),
 apiPost: async (path, body) => {
  const response = await fetch('/fixture/' + path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const value = await response.json(); if(!response.ok) throw new Error(value.message); return value;
 }
};
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content_type="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/fixture/catalog":
            self.send(json.dumps(catalog.snapshot()).encode())
        elif self.path == "/fixture/document-models":
            self.send(
                json.dumps({"models": [{"id": "fixture-model", "model": "fixture"}]}).encode()
            )
        elif self.path in ("/", "/app.js", "/style.css"):
            filename = "index.html" if self.path == "/" else self.path[1:]
            content = (root / "pages/manage" / filename).read_text()
            if filename == "index.html":
                content = content.replace("</head>", fixture_bridge + "</head>")
            self.send(
                content.encode(),
                {
                    "index.html": "text/html; charset=utf-8",
                    "app.js": "text/javascript",
                    "style.css": "text/css",
                }[filename],
            )
        else:
            self.send(b"{}", status=404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            action = self.path.split("/")[-1]
            if action == "import-curl":
                result = {"definition": import_curl(body["text"])}
            elif action == "discover":

                async def discover():
                    import httpx

                    async def reader(prompt, system):
                        assert "synthetic-key" not in prompt
                        return json.dumps(ordinary_spec())

                    def respond(req):
                        if req.url.host == "docs.example.test":
                            return httpx.Response(200, text="<main><pre>" + TEXT + "</pre></main>")
                        if "/undocumented/" in req.url.path or req.url.path == "/undocumented":
                            return httpx.Response(404)
                        return httpx.Response(200, json=spec())

                    engine = DiscoveryModule.Discovery(
                        httpx.AsyncClient(transport=httpx.MockTransport(respond)), reader=reader
                    )
                    try:
                        return await engine.run(body)
                    finally:
                        await engine.close()

                result = asyncio.run(discover())
            else:
                result = catalog.mutate(action, body)
            self.send(json.dumps(result).encode())
        except (Definitions.DefinitionError, ConflictError) as exc:
            self.send(json.dumps({"message": str(exc)}).encode(), status=400)


def run():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output = Path("/tmp/api-import-browser")
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
        )
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        page.get_by_text("接入你的第一个 API", exact=True).wait_for()
        page.locator("#add").click()
        page.locator("#tab-form").click()
        page.locator("#name").fill("query_items")
        page.locator("#description").fill("查询集合中的条目，按指定数量返回结果。")
        page.locator("#url").fill("https://api.example.com/collections/{collection_id}/items")
        page.locator("#detect-path").click()
        assert page.locator("#params .param-row").count() == 1
        page.locator('[data-add-map="query"]').click()
        page.locator("#map-query input").nth(0).fill("limit")
        page.locator("#map-query select").select_option("number")
        page.locator("#map-query input").nth(1).fill("20")
        page.locator("#enabled").check()
        page.screenshot(path=str(output / "form-light.png"))
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        item = catalog.snapshot()["items"][0]
        assert item["parameters"]["required"] == ["collection_id"]
        assert item["request"]["query"]["limit"] == 20
        page.get_by_role("button", name="停用", exact=True).click()
        page.get_by_text("已停用", exact=True).wait_for()
        # JSON source retains complex nested constraints through the form editor.
        page.locator("#add").click()
        page.locator("#tab-json").click()
        nested = {
            "name": "update_item",
            "description": "修改条目的名称和状态。",
            "enabled": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                },
                "additionalProperties": False,
            },
            "request": {
                "method": "PATCH",
                "url": "https://api.example.com/items/1",
                "json": {"count": {"$param": "count"}, "meta": {"source": "test"}},
            },
        }
        page.locator("#json-text").fill(json.dumps(nested))
        page.locator("#tab-form").click()
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        saved = catalog.snapshot()["items"][1]
        assert saved["parameters"]["properties"]["count"]["maximum"] == 100
        assert saved["request"]["json"] == nested["request"]["json"]
        # cURL parsing creates an editable draft; no writes until save.
        page.locator("#add").click()
        page.locator("#tab-curl").click()
        page.locator("#curl-text").fill(
            "curl 'https://api.example.com/reports?start=2026-09-01' -H 'Accept: application/json'"
        )
        page.locator("#parse-curl").click()
        page.locator("#panel-form").wait_for(state="visible")
        assert len(catalog.snapshot()["items"]) == 2
        page.locator("#name").fill("daily_report")
        page.locator("#description").fill("获取指定日期范围内的业务报表。")
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        assert len(catalog.snapshot()["items"]) == 3
        page.screenshot(path=str(output / "list-light.png"))
        page.evaluate("document.documentElement.dataset.theme='dark'")
        page.screenshot(path=str(output / "list-dark.png"))
        # Edit + rename; removal uses in-page confirmation (iframe compatible).
        card = page.locator(".card").filter(has_text="api_daily_report")
        card.get_by_role("button", name="编辑", exact=True).click()
        page.locator("#name").fill("renamed_report")
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        assert catalog.snapshot()["items"][2]["name"] == "renamed_report"
        page.locator("#search").fill("renamed")
        assert page.locator(".card").count() == 1
        page.locator(".card").get_by_role("button", name="删除", exact=True).click()
        page.locator("#confirm-yes").click()
        page.get_by_text("没有匹配的 API", exact=True).wait_for()
        assert len(catalog.snapshot()["items"]) == 2
        page.locator("#search").fill("")
        # Duplicate rejected; draft stays open and unchanged.
        page.locator("#add").click()
        page.locator("#tab-form").click()
        page.locator("#name").fill("query_items")
        page.locator("#description").fill("duplicate")
        page.locator("#url").fill("https://api.example.com")
        page.locator("#save").click()
        page.locator("#editor-error").wait_for(state="visible")
        assert page.locator("#name").input_value() == "query_items"
        page.locator("#cancel").click()
        page.locator("#confirm-yes").click()
        page.locator("#editor").wait_for(state="hidden")
        # Default two-field automatic discovery, grouped opt-in methods, and runtime permission management.
        page.locator("#add").click()
        assert page.locator("#panel-auto").is_visible()
        page.locator("#auto-url").fill("https://api.example.test/v1")
        page.locator("#auto-key").fill("synthetic-key")
        page.locator("#discover").click()
        page.locator("#discovered-operations .operation-row").nth(3).wait_for()
        assert page.locator("#discovered-operations input:checked").count() == 0
        page.locator("#discovered-operations .operation-group").filter(
            has=page.locator(".section-head", has_text="GET（")
        ).locator(".section-head input").check()
        page.screenshot(path=str(output / "automatic-operations.png"))
        before = len(catalog.snapshot()["items"])
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        incoming = catalog.snapshot()["items"][before:]
        assert len(incoming) == 4
        assert all(item["enabled"] == (item["request"]["method"] == "GET") for item in incoming)
        page.locator("#method-filter").select_option("POST")
        assert all(
            card.locator(".method").inner_text() == "POST" for card in page.locator(".card").all()
        )
        page.locator("#method-filter").select_option("")
        page.locator("#permissions").click()
        group = page.locator("#permission-groups .operation-group").filter(
            has=page.locator(".section-head", has_text="https://api.example.test · GET")
        )
        group.locator(".section-head input").uncheck()
        page.screenshot(path=str(output / "permissions.png"))
        page.locator("#save-permissions").click()
        page.locator("#permissions-dialog").wait_for(state="hidden")
        assert all(not item["enabled"] for item in catalog.snapshot()["items"][before:])
        # Changing a key invalidates cached discovery definitions.
        page.locator("#add").click()
        page.locator("#auto-url").fill("https://api.example.test/v1")
        page.locator("#auto-key").fill("synthetic-key")
        page.locator("#discover").click()
        page.locator("#discovered-operations .operation-row").nth(3).wait_for()
        page.locator("#auto-key").fill("changed-key")
        assert page.locator("#save").is_disabled()
        assert page.locator("#discovered-operations .operation-row").count() == 0
        page.locator("#cancel").click()
        page.locator("#confirm-yes").click()
        page.locator("#editor").wait_for(state="hidden")
        # Ordinary platform webpage is a primary field, with source evidence in a disabled draft.
        page.locator("#add").click()
        assert page.locator("#auto-doc-url").is_visible()
        page.locator("#auto-url").fill("https://api.example.test")
        page.locator("#auto-key").fill("synthetic-key")
        page.locator("#auto-doc-url").fill("https://docs.example.test/reporting-guide")
        page.locator("#auto-more summary").click()
        page.locator("#auto-model").select_option("fixture-model")
        page.locator("#discover").click()
        page.locator("#discovered-operations .operation-row").wait_for()
        assert "模型" in page.locator("#discovery-message").inner_text()
        assert page.locator("#discovered-operations input:checked").count() == 0
        page.locator("#discovered-operations summary").click()
        assert (
            "GET https://api.example.test/report"
            in page.locator("#discovered-operations").inner_text()
        )
        assert "UTC 日期" in page.locator("#discovered-operations").inner_text()
        page.screenshot(path=str(output / "ordinary-document.png"))
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        assert catalog.snapshot()["items"][-1]["request"]["query"]["api_key"] == "synthetic-key"
        assert not catalog.snapshot()["items"][-1]["enabled"]
        # Narrow screen layout, modal and body do not overflow horizontally.
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#add").click()
        page.locator("#tab-form").click()
        page.locator("#add-param").click()
        page.locator('[data-add-map="query"]').click()
        assert page.locator("#editor").evaluate("(node) => node.scrollWidth <= node.clientWidth")
        page.screenshot(path=str(output / "form-mobile.png"))
        assert not errors, errors
        browser.close()
    server.shutdown()
    print(
        "Browser checks passed: form, JSON, cURL, edit, toggle, delete, search, duplicate, constraints, themes, mobile"
    )


if __name__ == "__main__":
    run()
