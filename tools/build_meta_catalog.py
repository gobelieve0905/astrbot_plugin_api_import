"""Generate the checked-in route catalog from the pinned official Meta Business SDK.

Usage: python tools/build_meta_catalog.py /path/to/facebook-python-business-sdk
The source checkout must match SOURCE_COMMIT; never execute downloaded SDK code.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

SOURCE_COMMIT = "5286888addfe3ba3718db65fbf132bd66de3ddfe"
SOURCE_FINGERPRINT = "9f4c4ed6c7f6c3924ed7dc17f62862f6c82dbc6f2de6b63f6bc13b9d985d8645"
ROOT = Path(__file__).resolve().parents[1]


def generate(source):
    paths = sorted((source / "facebook_business/adobjects").glob("*.py")) + [
        source / "facebook_business/apiconfig.py",
        source / "LICENSE",
    ]
    fingerprint = hashlib.sha256()
    for path in paths:
        fingerprint.update(str(path.relative_to(source)).encode())
        fingerprint.update(b"\0")
        fingerprint.update(path.read_bytes())
    if fingerprint.hexdigest() != SOURCE_FINGERPRINT:
        raise ValueError("Source files do not match the pinned official SDK")
    operations, fields = {}, set()
    for path in sorted((source / "facebook_business/adobjects").glob("*.py")):
        if path.name in {"abstractcrudobject.py", "abstractobject.py"}:
            continue
        tree = ast.parse(path.read_text())
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for nested in cls.body:
                if isinstance(nested, ast.ClassDef) and nested.name == "Field":
                    for node in ast.walk(nested):
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                            if isinstance(node.value.value, str):
                                fields.add(node.value.value)
            for function in (node for node in cls.body if isinstance(node, ast.FunctionDef)):
                for call in ast.walk(function):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "FacebookRequest"
                    ):
                        continue
                    args = {kw.arg: kw.value for kw in call.keywords}
                    method, endpoint = (
                        ast.literal_eval(args[key]) for key in ("method", "endpoint")
                    )
                    key = (method, endpoint.rstrip("/"))
                    op = operations.setdefault(
                        key,
                        {
                            "method": method,
                            "path": endpoint.rstrip("/"),
                            "params": {},
                            "sources": [],
                        },
                    )
                    op["sources"].append(
                        {"object": cls.name, "method": function.name, "file": path.name}
                    )
                    for node in function.body:
                        if isinstance(node, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == "param_types" for t in node.targets
                        ):
                            for name, typ in ast.literal_eval(node.value).items():
                                op["params"].setdefault(name, set()).add(typ)
    output = []
    for (method, endpoint), op in sorted(operations.items()):
        ident = method.lower() + "_" + (endpoint.strip("/").replace("/", "_") or "node")
        if len(ident) > 23:
            ident = ident[:14] + "_" + hashlib.sha256(ident.encode()).hexdigest()[:8]
        op["id"] = ident
        op["params"] = {key: sorted(types) for key, types in sorted(op["params"].items())}
        output.append(op)
    assert len({op["id"] for op in output}) == len(output)
    version_tree = ast.parse((source / "facebook_business/apiconfig.py").read_text())
    config = next(
        ast.literal_eval(node.value) for node in version_tree.body if isinstance(node, ast.Assign)
    )
    return {
        "source_commit": SOURCE_COMMIT,
        "api_version": config["API_VERSION"],
        "sdk_version": config["SDK_VERSION"],
        "fields": sorted(fields),
        "operations": output,
    }


if __name__ == "__main__":
    source = Path(sys.argv[1])
    catalog = generate(source)
    (ROOT / "data/meta_operations.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    )
    (ROOT / "data/META_SDK_LICENSE.txt").write_text((source / "LICENSE").read_text())
    print(
        f"Generated {len(catalog['operations'])} canonical operations, SDK {catalog['sdk_version']}"
    )
