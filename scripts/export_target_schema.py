"""write the target platform's json schemas and field catalog into target_platform/schema/.
a unit test fails when these are stale, so run this after touching app/target/schema.py:

    uv run python scripts/export_target_schema.py
"""

from __future__ import annotations

import json
from pathlib import Path

from app.target.catalog import target_field_catalog
from app.target.schema import ENTITIES

OUT = Path(__file__).resolve().parents[1] / "target_platform" / "schema"


def render() -> dict[str, str]:
    files = {
        f"{name}.json": json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        for name, model in ENTITIES.items()
    }
    catalog = [f.as_dict() for f in target_field_catalog()]
    files["field_catalog.json"] = json.dumps(catalog, indent=2, sort_keys=True, default=str) + "\n"
    return files


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, content in render().items():
        (OUT / name).write_text(content)
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
