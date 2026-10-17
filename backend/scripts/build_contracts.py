"""从 FastAPI / Pydantic 同源导出 OpenAPI、TypeScript 类型和接口核对表。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.main import create_app
from scripts.fixtures import fixtures


def typescript(schema):
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
    if "anyOf" in schema or "oneOf" in schema:
        return "(" + " | ".join(typescript(s) for s in schema.get("anyOf", schema.get("oneOf"))) + ")"
    if "allOf" in schema:
        return "(" + " & ".join(typescript(s) for s in schema["allOf"]) + ")"
    kind = schema.get("type")
    if kind == "array":
        if "prefixItems" in schema:
            return "[" + ", ".join(typescript(s) for s in schema["prefixItems"]) + "]"
        return f"Array<{typescript(schema.get('items', {}))}>"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "string":
        return "string"
    if kind == "null":
        return "null"
    if kind == "object" or "properties" in schema:
        required = set(schema.get("required", []))
        properties = [
            json.dumps(k, ensure_ascii=False) + ("" if k in required else "?") + ": " + typescript(v) + ";"
            for k, v in schema.get("properties", {}).items()
        ]
        if schema.get("additionalProperties") is not False:
            extra = schema.get("additionalProperties")
            properties.append(
                "[key: string]: " + (typescript(extra) if isinstance(extra, dict) else "unknown") + ";"
            )
        return "{ " + " ".join(properties) + " }"
    return "unknown"


def generate():
    schema = create_app().openapi()
    rows = []
    declarations = ["// 自动生成：python scripts/build_contracts.py；请勿手工编辑。"]
    for name, value in sorted(schema["components"]["schemas"].items()):
        declarations.append(f"export type {name} = {typescript(value)};")
    operations = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if "operationId" not in operation:
                continue
            op = operation["operationId"]
            success = {code: value for code, value in operation["responses"].items() if code.startswith("2")}
            response_types = []
            for response in success.values():
                content = response.get("content", {})
                response_types.append(
                    typescript(content["application/json"]["schema"])
                    if "application/json" in content
                    else "Blob"
                    if content
                    else "void"
                )
            request = operation.get("requestBody", {}).get("content", {})
            request_type = typescript(next(iter(request.values()))["schema"]) if request else "void"
            parameters = operation.get("parameters", [])
            param_types = {}
            for parameter in parameters:
                param_types.setdefault(parameter["in"], []).append(
                    json.dumps(parameter["name"])
                    + ("" if parameter.get("required") else "?")
                    + ": "
                    + typescript(parameter["schema"])
                    + ";"
                )
            params = " ".join(
                f"{key}: {{ {' '.join(values)} }};" for key, values in sorted(param_types.items())
            )
            operations.append(
                f'  {op}: {{ method: "{method.upper()}"; path: "{path}"; body: {request_type}; response: {" | ".join(dict.fromkeys(response_types))}; parameters: {{ {params} }} }};'
            )
            rows.append(
                {
                    "operation_id": op,
                    "method": method.upper(),
                    "path": path,
                    "success": sorted(success),
                    "version_required": any(p["name"] == "If-Match" for p in parameters),
                    "idempotency_required": any(p["name"] == "Idempotency-Key" for p in parameters),
                }
            )
    declarations += ["export interface Operations {", *operations, "}"]
    root = Path(__file__).resolve().parents[2]
    document = (root / "docs/04-后端详细设计与接口契约.md").read_text(encoding="utf-8")
    expected = set(re.findall(r"^\| (\w+) \| (GET|POST|PATCH|PUT|DELETE) \| (/[^ ]+) \|", document, re.M))
    actual = {(r["operation_id"], r["method"], r["path"]) for r in rows}
    if expected != actual:
        raise RuntimeError(f"接口与文档不一致：缺少 {expected - actual}，额外 {actual - expected}")
    by_id = {row["operation_id"]: row for row in rows}
    for line in document.splitlines():
        fields = [value.strip() for value in line.split("|")]
        if len(fields) < 8 or fields[1] not in by_id:
            continue
        row = by_id[fields[1]]
        constraints = fields[-2]
        if (
            fields[4] not in row["success"]
            or row["version_required"] != bool(re.search(r"\bV\b", constraints))
            or row["idempotency_required"] != bool(re.search(r"\bK\b", constraints))
        ):
            raise RuntimeError(f"成功状态或 V/K 约束不一致：{fields[1]}")
    return {
        "fixtures.json": json.dumps(fixtures(), ensure_ascii=False, indent=2) + "\n",
        "openapi.json": json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        "api-types.ts": "\n".join(declarations) + "\n",
        "route-audit.json": json.dumps(
            {
                "schema_version": "1.0",
                "operations": sorted(rows, key=lambda r: r["operation_id"]),
                "matched_document_routes": len(rows),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / "contracts"
    destination.mkdir(exist_ok=True)
    generated = generate()
    for name, text in generated.items():
        path = destination / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                raise SystemExit(f"契约产物已过期：{name}")
        else:
            path.write_text(text, encoding="utf-8")
    print(f"接口逐项核对通过，已{'检查' if args.check else '生成'} {len(generated)} 个契约产物")


if __name__ == "__main__":
    main()
