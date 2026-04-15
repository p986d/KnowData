from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def load_upstream_module(
    upstream_script: str | Path,
    *,
    module_name: str,
) -> ModuleType:
    script_path = Path(upstream_script).resolve()
    if not script_path.exists():
        raise FileNotFoundError(f"Upstream script not found: {script_path}")

    script_dir = str(script_path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load upstream module from {script_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def normalize_table_identity(
    full_name: str,
    *,
    backend: str | None = None,
) -> tuple[str, str, str]:
    text = str(full_name or "").strip()
    parts = [part.strip() for part in text.split(".") if part.strip()]
    if backend == "mysql" and len(parts) >= 3 and parts[0] == parts[1]:
        normalized_full_name = ".".join([parts[0], parts[-1]])
        return normalized_full_name, parts[0], parts[-1]
    if len(parts) >= 2:
        return text, ".".join(parts[:-1]), parts[-1]
    return text, "", text


def load_database_tables_from_db_root(
    module: ModuleType,
    *,
    db_root: str | Path,
    db_id: str,
    backend: str | None = None,
) -> list[Any]:
    resolved_db_root = Path(db_root).resolve()
    if not resolved_db_root.exists():
        raise FileNotFoundError(
            f"db_id '{db_id}' not found under explicit db_root: {resolved_db_root}"
        )

    table_metadata_cls = getattr(module, "TableMetadata", None)
    if table_metadata_cls is None:
        online_schema_linking_module = sys.modules.get("online_schema_linking")
        if online_schema_linking_module is not None:
            table_metadata_cls = getattr(
                online_schema_linking_module,
                "TableMetadata",
                None,
            )
    if table_metadata_cls is None:
        raise AttributeError("Unable to resolve upstream TableMetadata class.")

    load_json_fn = getattr(module, "load_json", None)
    if load_json_fn is None:
        online_schema_linking_module = sys.modules.get("online_schema_linking")
        if online_schema_linking_module is not None:
            load_json_fn = getattr(online_schema_linking_module, "load_json", None)
    if load_json_fn is None:
        load_json_fn = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))

    tables: list[Any] = []
    for json_path in sorted(resolved_db_root.rglob("*.json")):
        try:
            payload = load_json_fn(json_path)
        except json.JSONDecodeError:
            continue

        full_name = payload.get("table_fullname")
        column_names = payload.get("column_names")
        if not full_name or not isinstance(column_names, list) or not column_names:
            continue

        normalized_full_name, namespace, short_name = normalize_table_identity(
            str(full_name),
            backend=backend,
        )
        if not normalized_full_name or not short_name:
            continue

        column_types = payload.get("column_types") or []
        descriptions = payload.get("description") or []
        sample_rows = payload.get("sample_rows") or []

        if not isinstance(column_types, list):
            column_types = []
        if not isinstance(descriptions, list):
            descriptions = []
        if not isinstance(sample_rows, list):
            sample_rows = []

        if len(column_types) < len(column_names):
            column_types = list(column_types) + [""] * (len(column_names) - len(column_types))
        if len(descriptions) < len(column_names):
            descriptions = list(descriptions) + [""] * (len(column_names) - len(descriptions))

        tables.append(
            table_metadata_cls(
                full_name=normalized_full_name,
                namespace=namespace,
                short_name=short_name,
                column_names=[str(name) for name in column_names],
                column_types=[str(value) for value in column_types[: len(column_names)]],
                descriptions=[str(value) for value in descriptions[: len(column_names)]],
                sample_rows=sample_rows,
            )
        )

    if not tables:
        raise ValueError(
            f"No usable table JSON files found for db_id '{db_id}' under {resolved_db_root}"
        )
    return tables
