"""Browser checks against a disposable local catalog; never calls a business API.

Run separately with playwright installed, e.g. python -B tests/browser_check.py.
"""

import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from test_engine import Definitions, root

Catalog = importlib.import_module("api_import_test.catalog").Catalog
ConflictError = importlib.import_module("api_import_test.catalog").ConflictError
import_curl = importlib.import_module("api_import_test.importing").import_curl
Platforms = importlib.import_module("api_import_test.platforms")
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
        elif self.path == "/fixture/platforms":
            self.send(json.dumps(Platforms.platform_catalog()).encode())
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
        page.locator("#tool-name").fill("My_account_query")
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
        assert item["tool_name"] == "My_account_query"
        assert catalog.snapshot()["tool_names"][item["name"]] == "My_account_query"
        assert item["parameters"]["required"] == ["collection_id"]
        assert item["request"]["query"]["limit"] == 20
        page.get_by_role("button", name="停用", exact=True).click()
        page.locator(".badge.off").first.wait_for()
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
        page.locator("#tool-name").fill("My_account_query")
        page.locator("#description").fill("duplicate")
        page.locator("#url").fill("https://api.example.com")
        page.locator("#save").click()
        page.locator("#editor-error").wait_for(state="visible")
        assert page.locator("#name").input_value() == "query_items"
        page.locator("#cancel").click()
        page.locator("#confirm-yes").click()
        page.locator("#editor").wait_for(state="hidden")
        # Success feedback expires without moving the page; errors remain visible.
        page.locator("#refresh").click()
        page.locator("#notice").wait_for(state="visible")
        page.locator("#notice").wait_for(state="hidden", timeout=6000)
        # Presets require only the token and independent operation switches, including same-method operations.
        page.locator("#add").click()
        assert page.locator("#panel-platform").is_visible()
        assert page.locator("#tab-auto").count() == 0
        page.screenshot(path=str(output / "platform-market.png"), animations="disabled")
        page.locator("#platform-cards").get_by_role("button", name="添加账户").click()
        assert page.locator("#platform-operations .operation-row").count() == 6
        assert page.locator("#platform-operations input:checked").count() == 0
        page.locator("#save").click()
        assert page.locator("#editor-error").is_visible()
        page.locator("#platform-name").fill("国内投放账户")
        page.locator("#platform-call-name").fill("Ninety_Report")
        page.locator("#platform-token").fill("synthetic-key")
        page.locator('[data-operation-id="advertiser"]').check()
        page.locator('[data-operation-id="cohort_sessions"]').check()
        page.evaluate("document.documentElement.dataset.theme='light'")
        page.locator("#platform-token").dispatch_event("input")
        page.locator("#editor .dialog-body").evaluate("node => node.scrollTop = 0")
        page.screenshot(path=str(output / "platform-quick-light.png"), animations="disabled")
        before = len(catalog.snapshot()["items"])
        page.locator("#save").click()

        page.locator("#editor").wait_for(state="hidden")
        incoming = catalog.snapshot()["items"][before:]
        assert catalog.snapshot()["tool_names"][incoming[0]["name"]] == "Ninety_Report_advertiser"
        assert len(incoming) == 6 and sum(item["enabled"] for item in incoming) == 2
        assert page.locator("#platform-token").input_value() == ""
        page.locator("#method-filter").select_option("PATCH")
        assert page.locator(".card").count() == 1
        page.locator("#method-filter").select_option("")
        page.locator("#permissions").click()
        advertiser = incoming[0]["name"]
        page.locator(f'[data-operation-name="{advertiser}"]').uncheck()
        page.locator("#save-permissions").click()
        page.locator("#permissions-dialog").wait_for(state="hidden")
        assert sum(item["enabled"] for item in catalog.snapshot()["items"][before:]) == 1
        assert catalog.snapshot()["items"][-1]["enabled"]
        # Additional accounts do not collide, and all-off onboarding remains possible.
        page.locator("#add").click()
        page.locator("#platform-cards").get_by_role("button", name="添加账户").click()
        page.locator("#platform-name").fill("海外投放账户")
        page.locator("#platform-call-name").fill("Overseas_Report")
        page.locator("#platform-token").fill("second-synthetic-key")
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        assert len(catalog.snapshot()["items"]) == before + 12
        assert not any(item["enabled"] for item in catalog.snapshot()["items"][-6:])
        assert page.locator(".connection-card").count() == 2
        first_card = page.locator(".connection-card").filter(
            has=page.get_by_role("heading", name="国内投放账户", exact=True)
        )
        assert page.locator("#overview .connection-details").count() == 0
        first_card.get_by_role("button", name="进入账户").click()
        assert page.locator("#overview").is_hidden()
        assert page.locator("#account-operation-list .operation-card").count() == 6
        page.locator("#operation-search").fill("Ninety_Report_ADVERTISER")
        assert page.locator("#account-operation-list .operation-card").count() == 1
        page.locator("#operation-status").select_option("enabled")
        assert page.locator("#account-operation-list .operation-card").count() == 0
        page.locator("#clear-operation-filters").click()
        page.locator("#operation-search").fill("收益")
        assert page.locator("#account-operation-list .operation-card").count() == 2
        page.locator("#operation-method").select_option("POST")
        assert page.locator("#account-operation-list .operation-card").count() == 0
        page.get_by_role("button", name="查看全部操作", exact=True).click()
        assert page.locator("#account-operation-list .operation-card").count() == 6
        page.locator("#account-settings").click()
        assert page.locator("#connection-token").input_value() == ""
        page.locator("#connection-name").fill("国内主账户")
        page.locator("#connection-token").fill("rotated-key")
        page.locator("#connection-operations input").first.check()
        page.locator("#save-connection").click()
        page.locator("#connection-editor").wait_for(state="hidden")
        assert all(
            item["request"]["query"]["api_key"] == "rotated-key"
            for item in catalog.snapshot()["items"][before : before + 6]
        )
        assert all(
            item["request"]["query"]["api_key"] == "second-synthetic-key"
            for item in catalog.snapshot()["items"][-6:]
        )
        first_card = page.locator(".connection-card").filter(
            has=page.get_by_role("heading", name="国内主账户", exact=True)
        )
        page.locator("#account-operation-list .operation-card").first.get_by_role(
            "button", name="编辑", exact=True
        ).click()
        page.locator("#display-name").fill("投放花费日报")
        page.locator("#save").click()
        page.locator("#editor").wait_for(state="hidden")
        assert catalog.snapshot()["items"][before]["display_name"] == "投放花费日报"
        page.screenshot(path=str(output / "account-detail.png"), animations="disabled")
        page.locator("#back-accounts").click()
        page.locator("#search").fill("国内主账户")
        assert page.locator(".connection-card").count() == 1
        page.locator("#search").fill("")
        page.screenshot(path=str(output / "accounts-desktop.png"), animations="disabled")
        page.evaluate("document.documentElement.dataset.theme='dark'")
        page.screenshot(path=str(output / "accounts-dark.png"), animations="disabled")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "accounts-mobile.png"), animations="disabled")
        second_card = page.locator(".connection-card").filter(
            has=page.get_by_role("heading", name="海外投放账户", exact=True)
        )
        second_card.get_by_role("button", name="删除接入").click()
        page.locator("#confirm-yes").click()
        page.wait_for_function("document.querySelectorAll('.connection-card').length === 1")
        assert len(catalog.snapshot()["items"]) == before + 6
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
        "Browser checks passed: platform token onboarding, independent permissions, multiple accounts, form, JSON, cURL, CRUD, themes, mobile"
    )


if __name__ == "__main__":
    run()
