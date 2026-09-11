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
