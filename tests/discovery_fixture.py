"""Synthetic OpenAPI fixture for offline tests; not a platform configuration pack."""


def spec():
    return {
        "openapi": "3.0.3",
        "info": {"title": "Example API", "version": "1"},
        "servers": [{"url": "https://api.example.test/v1"}],
        "security": [{"Key": []}],
        "components": {
            "securitySchemes": {"Key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
            "schemas": {
                "NewItem": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "id": {"type": "integer", "readOnly": True},
                    },
                    "required": ["name", "id"],
                }
            },
        },
        "paths": {
            "/items": {
                "get": {
                    "operationId": "list_items",
                    "summary": "查询条目列表",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "limit",
                            "schema": {"type": "integer", "default": 20, "minimum": 1},
                        },
                        {
                            "in": "query",
                            "name": "tags",
                            "schema": {"type": "array", "items": {"type": "string"}},
                            "explode": False,
                        },
                    ],
                },
                "post": {
                    "operationId": "create_item",
                    "summary": "创建条目",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/NewItem"}}
                        },
                    },
                },
            },
            "/items/{item-id}": {
                "parameters": [
                    {
                        "in": "path",
                        "name": "item-id",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "get": {"operationId": "get_item", "summary": "查看条目"},
                "delete": {"operationId": "delete_item", "summary": "删除条目"},
            },
        },
    }


TEXT = """Example reporting API
GET https://api.example.test/report
Set the query parameter api_key to your Report Key.
start: required string, first UTC day, YYYY-MM-DD. Example 2023-03-25.
limit: optional integer, default 20.
"""


def ordinary_spec():
    return {
        "openapi": "3.0.3",
        "info": {"title": "Reports", "version": "1"},
        "servers": [{"url": "https://api.example.test"}],
        "security": [{"Key": []}],
        "components": {
            "securitySchemes": {
                "Key": {
                    "type": "apiKey",
                    "in": "query",
                    "name": "api_key",
                    "x-evidence": "Set the query parameter api_key to your Report Key.",
                }
            }
        },
        "paths": {
            "/report": {
                "get": {
                    "summary": "查询报表",
                    "x-evidence": "GET https://api.example.test/report",
                    "x-method-evidence": "GET https://api.example.test/report",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "start",
                            "required": True,
                            "schema": {"type": "string", "description": "UTC 日期 YYYY-MM-DD"},
                        },
                        {
                            "in": "query",
                            "name": "limit",
                            "schema": {"type": "integer", "default": 20},
                        },
                    ],
                }
            }
        },
    }
