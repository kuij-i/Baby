"""OpenAPI schema generation and export utility for TypeScript client preparation.

Provides automated export and validation of Baby's OpenAPI schema for code generation
tools (e.g. openapi-typescript, openapi-generator) without requiring frontend build code.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI


def get_openapi_schema(application: FastAPI) -> Dict[str, Any]:
    """Generate and return the complete OpenAPI 3.1.0 schema dictionary from the FastAPI app."""
    return application.openapi()


def export_openapi_schema(
    application: Optional[FastAPI] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Export the OpenAPI schema to a JSON file or return it.

    Args:
        application: FastAPI instance (defaults to baby.api.app.app)
        output_path: Optional file path to write openapi.json to

    Returns:
        The OpenAPI schema dictionary
    """
    if application is None:
        from baby.api.app import app

        application = app

    schema = get_openapi_schema(application)

    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(schema, indent=2), encoding="utf-8")

    return schema


if __name__ == "__main__":
    schema = export_openapi_schema(output_path="docs/openapi.json")
    print(f"OpenAPI schema successfully exported with {len(schema.get('paths', {}))} paths.")
