"""Deterministic TypeScript type generation from Baby's authoritative OpenAPI schema.

Converts OpenAPI 3.1 component schemas and route definitions to TypeScript interfaces.
Guarantees:
- Pure Python generator (zero external node_modules or dependencies required).
- Perfectly deterministic output.
- Exact mapping: Python models -> OpenAPI schema -> TypeScript types.

Usage:
    python scripts/generate_ts_types.py           # regenerate docs/openapi.json and types.ts
    python scripts/generate_ts_types.py --check   # exit 1 if either committed file is stale
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "docs" / "openapi.json"
TS_TYPES_PATH = ROOT / "clients" / "typescript" / "src" / "types.ts"


def openapi_type_to_ts(schema: Dict[str, Any], components: Dict[str, Any], indent_level: int = 1) -> str:
    """Recursively convert an OpenAPI schema to a TypeScript type string."""
    if "$ref" in schema:
        ref_name = schema["$ref"].split("/")[-1]
        return ref_name

    if "anyOf" in schema:
        types = [openapi_type_to_ts(s, components, indent_level) for s in schema["anyOf"]]
        return " | ".join(types)

    if "oneOf" in schema:
        types = [openapi_type_to_ts(s, components, indent_level) for s in schema["oneOf"]]
        return " | ".join(types)

    if "allOf" in schema:
        types = [openapi_type_to_ts(s, components, indent_level) for s in schema["allOf"]]
        return " & ".join(types)

    t = schema.get("type")

    if t == "string":
        if "enum" in schema:
            return " | ".join(json.dumps(val) for val in schema["enum"])
        return "string"

    if t in ("integer", "number"):
        return "number"

    if t == "boolean":
        return "boolean"

    if t == "null":
        return "null"

    if t == "array":
        items = schema.get("items", {})
        item_ts = openapi_type_to_ts(items, components, indent_level)
        if " | " in item_ts or " & " in item_ts:
            return f"({item_ts})[]"
        return f"{item_ts}[]"

    if t == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        add_props = schema.get("additionalProperties", None)

        if not properties and add_props is not None:
            if add_props is True or add_props == {}:
                return "Record<string, unknown>"
            val_ts = openapi_type_to_ts(add_props, components, indent_level)
            return f"Record<string, {val_ts}>"

        if not properties and add_props is None:
            return "Record<string, unknown>"

        lines = ["{"]
        indent = "  " * indent_level
        for prop_name, prop_schema in properties.items():
            is_req = prop_name in required
            opt_mark = "" if is_req else "?"
            prop_ts = openapi_type_to_ts(prop_schema, components, indent_level + 1)
            description = prop_schema.get("description")
            if description:
                lines.append(f"{indent}/** {description} */")
            lines.append(f"{indent}{prop_name}{opt_mark}: {prop_ts};")
        if add_props is True:
            lines.append(f"{indent}[key: string]: unknown;")
        elif isinstance(add_props, dict):
            val_ts = openapi_type_to_ts(add_props, components, indent_level + 1)
            lines.append(f"{indent}[key: string]: {val_ts};")
        lines.append(("  " * (indent_level - 1)) + "}")
        return "\n".join(lines)

    return "unknown"


def generate_typescript_definitions(openapi_schema: Dict[str, Any]) -> str:
    """Generate the complete TypeScript types file content from an OpenAPI schema dict."""
    components = openapi_schema.get("components", {}).get("schemas", {})

    output: List[str] = [
        "/**",
        " * Auto-generated TypeScript API contracts for BABY AI Operating System.",
        " * Generated directly from OpenAPI 3.1 specification.",
        " * DO NOT EDIT MANUALLY. Regenerate with `npm run generate-types`.",
        " */",
        "",
    ]

    # 1. Enums and Schema types
    for name, schema in sorted(components.items()):
        doc = schema.get("description", f"Schema for {name}")
        output.append(f"/** {doc} */")

        # Enum string type
        if schema.get("type") == "string" and "enum" in schema:
            variants = " | ".join(json.dumps(v) for v in schema["enum"])
            output.append(f"export type {name} = {variants};")
            # Also export an object enum for convenient runtime use
            output.append(f"export const {name} = {{")
            for val in schema["enum"]:
                key = val.upper().replace("-", "_").replace(" ", "_")
                output.append(f"  {key}: {json.dumps(val)},")
            output.append("} as const;")
            output.append(f"export type {name}Value = typeof {name}[keyof typeof {name}];")
            output.append("")
            continue

        # Object / interface type
        ts_type = openapi_type_to_ts(schema, components)
        if ts_type.startswith("{\n"):
            output.append(f"export interface {name} {ts_type}")
        else:
            output.append(f"export type {name} = {ts_type};")
        output.append("")

    return "\n".join(output)


def build_contract() -> Tuple[str, str]:
    """Return (openapi_json_text, typescript_text) generated from the FastAPI app, without writing."""
    from baby.api.openapi import export_openapi_schema

    schema = export_openapi_schema()
    return json.dumps(schema, indent=2), generate_typescript_definitions(schema)


def stale_contract_files() -> List[Path]:
    """Committed contract files whose content differs from freshly generated output.

    Files are compared as text with universal newlines, so a CRLF checkout (e.g. Windows
    with core.autocrlf) is not reported as drift.
    """
    stale: List[Path] = []
    for path, expected in zip((OPENAPI_PATH, TS_TYPES_PATH), build_contract()):
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != expected:
            stale.append(path)
    return stale


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the OpenAPI contract and TypeScript types")
    parser.add_argument("--check", action="store_true", help="fail if committed files differ from generated output")
    args = parser.parse_args(argv)

    if args.check:
        stale = stale_contract_files()
        for path in stale:
            print(f"Stale generated contract file: {path.relative_to(ROOT)}", file=sys.stderr)
        if stale:
            print("Run `python scripts/generate_ts_types.py` and commit the result.", file=sys.stderr)
            return 1
        print("OpenAPI schema and TypeScript types are up to date.")
        return 0

    openapi_text, ts_content = build_contract()
    for path, text in ((OPENAPI_PATH, openapi_text), (TS_TYPES_PATH, ts_content)):
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": identical bytes on every OS (no CRLF translation on Windows).
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    print(f"Successfully generated OpenAPI schema -> {OPENAPI_PATH}")
    print(f"Successfully generated TypeScript types -> {TS_TYPES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
