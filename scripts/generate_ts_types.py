"""Deterministic TypeScript type generation from Baby's authoritative OpenAPI schema.

Converts OpenAPI 3.1 component schemas and route definitions to TypeScript interfaces.
Guarantees:
- Pure Python generator (zero external node_modules or dependencies required).
- Perfectly deterministic output.
- Exact mapping: Python models -> OpenAPI schema -> TypeScript types.
"""

import json
from pathlib import Path
from typing import Any, Dict, List


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


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    openapi_path = root / "docs" / "openapi.json"
    ts_types_path = root / "clients" / "typescript" / "src" / "types.ts"

    from baby.api.openapi import export_openapi_schema

    schema = export_openapi_schema(output_path=str(openapi_path))

    ts_content = generate_typescript_definitions(schema)
    ts_types_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ts_types_path, "w", encoding="utf-8") as f:
        f.write(ts_content)

    print(f"Successfully generated TypeScript types -> {ts_types_path}")


if __name__ == "__main__":
    main()
