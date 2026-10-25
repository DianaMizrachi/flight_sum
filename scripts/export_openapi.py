#!/usr/bin/env python3
"""Write the OpenAPI spec of the aura API to docs/openapi.{json,yaml}.

The exported files are committed so the contract is reviewable in a diff and
viewable (Swagger Editor, ReDoc, codegen) without running the service.
``tests/test_api.py`` fails when they drift from the app, in the same spirit
as stage 0's rule that CI breaks if a schema changes without its examples.

    python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aura.api import create_app  # noqa: E402

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
JSON_PATH = DOCS_DIR / "openapi.json"
YAML_PATH = DOCS_DIR / "openapi.yaml"


def export() -> dict:
    spec = create_app().openapi()
    DOCS_DIR.mkdir(exist_ok=True)
    JSON_PATH.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    YAML_PATH.write_text(
        yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )
    return spec


if __name__ == "__main__":
    exported = export()
    print(f"{JSON_PATH.relative_to(Path.cwd())}: {len(exported['paths'])} paths")
    print(f"{YAML_PATH.relative_to(Path.cwd())}: {len(exported['paths'])} paths")
