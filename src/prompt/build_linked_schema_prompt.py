from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# schema -> focused tables
# 若某个 schema 不在该字典中，则默认展开该 schema 的全部表
DEFAULT_FOCUS: dict[str, list[str]] = {
    # "NEW_YORK_CITIBIKE": [],
    # "CYCLISTIC": [],
    # "GEO_US_BOUNDARIES": [],
    "NOAA_GSOD": ["GSOD2014"],
}


DDL_COL_RE = re.compile(
    r'^\s*"?(?P<name>[^"\s]+)"?\s+(?P<dtype>[A-Z]+(?:\([^)]+\))?)',
    re.IGNORECASE,
)


@dataclass
class ColumnMeta:
    name: str
    dtype: str | None = None
    description: str | None = None
    example: str | None = None


@dataclass
class TableMeta:
    schema: str
    table: str
    fullname: str
    columns: list[ColumnMeta] = field(default_factory=list)


def clean_text(text: str | None) -> str:
    if text is None:
        return ""
    return " ".join(str(text).replace("\n", " ").split()).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def format_example(value: Any, limit: int = 60) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        s = value.replace("\n", " ")
        s = shorten(s, limit)
        return repr(s)
    if isinstance(value, (int, float, bool)):
        return str(value)
    s = json.dumps(value, ensure_ascii=False)
    return shorten(s, limit)


def split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_quotes = False
    prev = ""

    for ch in text:
        if ch == '"' and prev != "\\":
            in_quotes = not in_quotes
            buf.append(ch)
        elif not in_quotes and ch == "(":
            depth += 1
            buf.append(ch)
        elif not in_quotes and ch == ")":
            depth -= 1
            buf.append(ch)
        elif not in_quotes and depth == 0 and ch == ",":
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)
        prev = ch

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def extract_columns_from_ddl(ddl: str) -> list[tuple[str, str]]:
    ddl = ddl.strip()
    left = ddl.find("(")
    right = ddl.rfind(")")
    if left == -1 or right == -1 or right <= left:
        return []

    body = ddl[left + 1 : right]
    parts = split_top_level_commas(body)

    columns: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        upper = part.upper()
        if upper.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CONSTRAINT")):
            continue
        m = DDL_COL_RE.match(part)
        if m:
            columns.append((m.group("name"), m.group("dtype")))
    return columns


def load_ddl_columns(schema_dir: Path) -> dict[str, list[tuple[str, str]]]:
    ddl_path = schema_dir / "DDL.csv"
    ddl_map: dict[str, list[tuple[str, str]]] = {}

    with ddl_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            table_name = row["table_name"].strip()
            ddl = row["DDL"]
            ddl_map[table_name] = extract_columns_from_ddl(ddl)

    return ddl_map


def load_table_json(json_path: Path) -> dict[str, Any]:
    with json_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def first_non_null_sample(sample_rows: list[dict[str, Any]], col_name: str) -> Any:
    for row in sample_rows:
        if col_name in row and row[col_name] is not None:
            return row[col_name]
    return None


def append_unique(items: list[str], value: str | None) -> None:
    if value and value not in items:
        items.append(value)


def normalize_identifier(name: str) -> str:
    return name.replace("_", "").upper()


def clean_str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        s = clean_text(v)
        if s and s not in out:
            out.append(s)
    return out


def build_attribute_ref(entity_name: str, attribute_name: str) -> str:
    if entity_name and attribute_name:
        return f"{entity_name}.{attribute_name}"
    return attribute_name or entity_name


def build_table_meta(
    schema_dir: Path,
    json_path: Path,
    ddl_map: dict[str, list[tuple[str, str]]],
) -> TableMeta:
    data = load_table_json(json_path)
    schema = schema_dir.name
    table_name = json_path.stem
    fullname = f"{schema}.{table_name}"

    json_cols = data.get("column_names", [])
    json_types = data.get("column_types", [])
    json_descs = data.get("description", [])
    sample_rows = data.get("sample_rows", [])

    by_name: dict[str, ColumnMeta] = {}
    for idx, col_name in enumerate(json_cols):
        dtype = json_types[idx] if idx < len(json_types) else None
        desc = json_descs[idx] if idx < len(json_descs) else None
        example = first_non_null_sample(sample_rows, col_name)
        by_name[col_name] = ColumnMeta(
            name=col_name,
            dtype=dtype,
            description=clean_text(desc) or None,
            example=format_example(example) if example is not None else None,
        )

    ordered = ddl_map.get(table_name, [])
    columns: list[ColumnMeta] = []

    if ordered:
        seen = set()
        for col_name, ddl_dtype in ordered:
            meta = by_name.get(col_name, ColumnMeta(name=col_name))
            if not meta.dtype:
                meta.dtype = ddl_dtype
            columns.append(meta)
            seen.add(col_name)

        for col_name in json_cols:
            if col_name not in seen:
                columns.append(by_name[col_name])
    else:
        columns = [by_name[c] for c in json_cols]

    return TableMeta(
        schema=schema,
        table=table_name,
        fullname=fullname,
        columns=columns,
    )


def load_database(db_root: Path) -> list[TableMeta]:
    tables: list[TableMeta] = []

    for schema_dir in sorted(p for p in db_root.iterdir() if p.is_dir()):
        ddl_csv = schema_dir / "DDL.csv"
        if not ddl_csv.exists():
            continue

        ddl_map = load_ddl_columns(schema_dir)
        for json_path in sorted(schema_dir.glob("*.json")):
            table_meta = build_table_meta(schema_dir, json_path, ddl_map)
            tables.append(table_meta)

    return sorted(tables, key=lambda t: (t.schema, t.table))


def build_inventory_block(tables: list[TableMeta], preview_cols: int = 6) -> str:
    lines = ["[SCHEMA INVENTORY]"]
    current_schema = None

    for t in tables:
        if t.schema != current_schema:
            current_schema = t.schema
            lines.append(f"\n## Schema: {t.schema}")

        preview = ", ".join(c.name for c in t.columns[:preview_cols])
        more = "" if len(t.columns) <= preview_cols else ", ..."
        lines.append(f"- {t.fullname} :: {preview}{more}")

    return "\n".join(lines)


def build_table_detail_block(
    table: TableMeta,
    include_examples: bool = True,
    include_desc: bool = True,
    max_cols: int | None = None,
) -> str:
    cols = table.columns if max_cols is None else table.columns[:max_cols]
    lines = [f"### {table.fullname}", "columns:"]

    for c in cols:
        piece = f"- {c.name} {c.dtype or '?'}"
        extras: list[str] = []
        if include_desc and c.description:
            extras.append(c.description)
        if include_examples and c.example is not None:
            extras.append(f"example={c.example}")
        if extras:
            piece += " -- " + " | ".join(extras)
        lines.append(piece)

    return "\n".join(lines)


def resolve_detail_tables(
    tables: list[TableMeta],
    focus_map: dict[str, list[str]] | None,
) -> list[TableMeta]:
    """
    规则：
    - schema 在 focus_map 中：只展开 focus_map[schema] 里的表
    - schema 不在 focus_map 中：默认展开该 schema 下全部表
    """
    focus_map = focus_map or {}

    schema_to_tables: dict[str, list[TableMeta]] = {}
    for t in tables:
        schema_to_tables.setdefault(t.schema, []).append(t)

    selected: list[TableMeta] = []

    for schema, schema_tables in schema_to_tables.items():
        if schema in focus_map:
            wanted = {normalize_identifier(x) for x in focus_map[schema]}
            for t in schema_tables:
                if normalize_identifier(t.table) in wanted:
                    selected.append(t)
        else:
            selected.extend(schema_tables)

    return sorted(selected, key=lambda t: (t.schema, t.table))


def build_prompt_text(
    db_name: str,
    tables: list[TableMeta],
    focus_map: dict[str, list[str]] | None,
    inventory_preview_cols: int = 6,
    detail_max_cols: int | None = None,
) -> str:
    detail_tables = resolve_detail_tables(tables, focus_map)

    lines = [
        f"[DATABASE] {db_name}",
        "Dialect: Snowflake",
        "",
        "You are given schema metadata for this database.",
        "The inventory below is filtered by the focus configuration.",
        "Use the detailed cards below as the primary grounding for relevant joins, filters, metrics, and units.",
        "",
        build_inventory_block(detail_tables, preview_cols=inventory_preview_cols),
        "",
        "[TABLE DETAILS]",
    ]

    current_schema = None
    for table in detail_tables:
        if table.schema != current_schema:
            current_schema = table.schema
            lines.append("")
            lines.append(f"## Detailed Schema: {current_schema}")

        lines.append("")
        lines.append(
            build_table_detail_block(
                table=table,
                include_examples=True,
                include_desc=True,
                max_cols=detail_max_cols,
            )
        )

    return "\n".join(lines).strip() + "\n"


def load_focus_map(focus_json: Path | None) -> dict[str, list[str]]:
    if focus_json is None:
        return DEFAULT_FOCUS

    with focus_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("focus json must be a dict[str, list[str]]")

    normalized: dict[str, list[str]] = {}
    for schema, tables in data.items():
        if not isinstance(schema, str):
            raise ValueError("focus json keys must be schema names")
        if not isinstance(tables, list) or not all(isinstance(x, str) for x in tables):
            raise ValueError(f"focus json value for schema={schema} must be list[str]")
        normalized[schema] = tables

    return normalized


def ensure_selected_table(
    selected_schema: dict[str, dict[str, dict[str, Any]]],
    schema: str,
    table: str,
) -> dict[str, Any]:
    schema_bucket = selected_schema.setdefault(schema, {})
    return schema_bucket.setdefault(
        table,
        {
            "columns": [],
            "support_role": [],
            "selected_by_entities": [],
            "selected_by_attributes": [],
            "entity_support": {},
            "attribute_support": {},
            "table_level_selected": False,
        },
    )


def build_selection_from_linking(
    linking_doc: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]]]:
    """
    只支持当前格式：

    {
      "entities": [
        {
          "entity_name": "...",
          "candidates": [
            {
              "schema": "...",
              "table": "...",
              "columns": [...],
              "role": "primary_definition",
              "evidence": "..."
            }
          ]
        }
      ],
      "attributes": [
        {
          "entity_name": "...",
          "attribute_name": "...",
          "candidates": [
            {
              "schema": "...",
              "table": "...",
              "column": "...",
              "evidence": "..."
            }
          ]
        }
      ]
    }
    """
    entity_outputs: list[dict[str, Any]] = []
    attribute_outputs: list[dict[str, Any]] = []
    selected_schema: dict[str, dict[str, dict[str, Any]]] = {}

    # -------------------------
    # entities
    # -------------------------
    for entity in linking_doc.get("entities", []):
        entity_name = clean_text(entity.get("entity_name"))
        if not entity_name:
            continue

        table_acc: dict[tuple[str, str], dict[str, Any]] = {}

        for cand in entity.get("candidates", []):
            schema = clean_text(cand.get("schema"))
            table = clean_text(cand.get("table"))
            if not schema or not table:
                continue

            cand_columns = clean_str_list(cand.get("columns", []))
            role = clean_text(cand.get("role")) or None

            key = (schema, table)
            entry = table_acc.setdefault(
                key,
                {
                    "schema": schema,
                    "table": table,
                    "columns": [],
                    "support_role": [],
                    "table_level_selected": False,
                },
            )

            append_unique(entry["support_role"], role)
            for col in cand_columns:
                append_unique(entry["columns"], col)

            if not cand_columns:
                entry["table_level_selected"] = True

            selected_entry = ensure_selected_table(selected_schema, schema, table)
            append_unique(selected_entry["selected_by_entities"], entity_name)
            append_unique(selected_entry["support_role"], role)

            for col in cand_columns:
                append_unique(selected_entry["columns"], col)

            if not cand_columns:
                selected_entry["table_level_selected"] = True

            entity_support = selected_entry["entity_support"].setdefault(
                entity_name,
                {
                    "roles": [],
                    "table_level_selected": False,
                },
            )
            append_unique(entity_support["roles"], role)
            if not cand_columns:
                entity_support["table_level_selected"] = True

        entity_outputs.append(
            {
                "entity_name": entity_name,
                "tables": sorted(
                    table_acc.values(),
                    key=lambda x: (x["schema"], x["table"]),
                ),
            }
        )

    # -------------------------
    # attributes
    # -------------------------
    for attr in linking_doc.get("attributes", []):
        entity_name = clean_text(attr.get("entity_name"))
        attribute_name = clean_text(attr.get("attribute_name"))
        if not entity_name or not attribute_name:
            continue

        attribute_ref = build_attribute_ref(entity_name, attribute_name)
        table_acc: dict[tuple[str, str], dict[str, Any]] = {}

        for cand in attr.get("candidates", []):
            schema = clean_text(cand.get("schema"))
            table = clean_text(cand.get("table"))
            column = clean_text(cand.get("column"))
            if not schema or not table or not column:
                continue

            key = (schema, table)
            entry = table_acc.setdefault(
                key,
                {
                    "schema": schema,
                    "table": table,
                    "columns": [],
                },
            )

            append_unique(entry["columns"], column)

            selected_entry = ensure_selected_table(selected_schema, schema, table)
            append_unique(selected_entry["columns"], column)
            append_unique(selected_entry["selected_by_attributes"], attribute_ref)

            attr_support = selected_entry["attribute_support"].setdefault(entity_name, [])
            append_unique(attr_support, attribute_name)

        attribute_outputs.append(
            {
                "entity_name": entity_name,
                "attribute_name": attribute_name,
                "table_columns": sorted(
                    table_acc.values(),
                    key=lambda x: (x["schema"], x["table"]),
                ),
            }
        )

    selection_output_dict = {
        "entity_table_mapping": entity_outputs,
        "attribute_table_column_mapping": attribute_outputs,
    }
    return selection_output_dict, selected_schema


def has_primary_definition_role(table_selection: dict[str, Any]) -> bool:
    roles = table_selection.get("support_role", []) or []
    return "primary_definition" in roles


def get_selected_columns_for_table(
    table: TableMeta,
    table_selection: dict[str, Any],
) -> list[ColumnMeta]:
    selected_cols = table_selection.get("columns", []) or []

    # 显式提到列：只展示这些列
    if selected_cols:
        wanted = {normalize_identifier(col) for col in selected_cols}
        filtered = [
            col for col in table.columns
            if normalize_identifier(col.name) in wanted
        ]
        return filtered

    # 未提列：仅当 support_role 包含 primary_definition 才回退为全表
    if has_primary_definition_role(table_selection):
        return table.columns

    # 其他情况不展示
    return []


def should_display_table(
    table: TableMeta,
    table_selection: dict[str, Any],
) -> bool:
    visible_cols = get_selected_columns_for_table(table, table_selection)
    return len(visible_cols) > 0


def build_inventory_block_from_selection(
    tables: list[TableMeta],
    selected_schema: dict[str, dict[str, dict[str, Any]]],
    preview_cols: int = 6,
) -> str:
    lines = ["[SCHEMA INVENTORY]"]
    current_schema = None

    for t in tables:
        if t.schema != current_schema:
            current_schema = t.schema
            lines.append(f"\n## Schema: {t.schema}")

        table_selection = selected_schema[t.schema][t.table]
        visible_cols = get_selected_columns_for_table(t, table_selection)

        preview = ", ".join(c.name for c in visible_cols[:preview_cols])
        more = "" if len(visible_cols) <= preview_cols else ", ..."

        if preview:
            lines.append(f"- {t.fullname} :: {preview}{more}")
        else:
            lines.append(f"- {t.fullname}")

    return "\n".join(lines)


def build_selected_table_detail_block(
    table: TableMeta,
    table_selection: dict[str, Any],
    include_examples: bool = True,
    include_desc: bool = True,
    max_cols: int | None = None,
) -> str:
    cols = get_selected_columns_for_table(table, table_selection)
    if max_cols is not None:
        cols = cols[:max_cols]

    lines = [f"### {table.fullname}"]

    entity_support = table_selection.get("entity_support", {}) or {}
    if entity_support:
        lines.append("entity_support:")
        for entity_name in sorted(entity_support):
            roles = entity_support[entity_name].get("roles", []) or []
            role_text = ", ".join(roles) if roles else "-"
            lines.append(f"- {entity_name} :: {role_text}")

    attribute_support = table_selection.get("attribute_support", {}) or {}
    if attribute_support:
        lines.append("attribute_support:")
        for entity_name in sorted(attribute_support):
            attrs = attribute_support[entity_name] or []
            attr_text = ", ".join(attrs) if attrs else "-"
            lines.append(f"- {entity_name} :: {attr_text}")

    selected_cols = table_selection.get("columns", []) or []
    if not selected_cols and has_primary_definition_role(table_selection):
        lines.append("column_scope: all columns (fallback from primary_definition)")

    lines.append("columns:")

    for c in cols:
        piece = f"- {c.name} {c.dtype or '?'}"
        extras: list[str] = []
        if include_desc and c.description:
            extras.append(c.description)
        if include_examples and c.example is not None:
            extras.append(f"example={c.example}")
        if extras:
            piece += " -- " + " | ".join(extras)
        lines.append(piece)

    return "\n".join(lines)


def build_prompt_text_from_selection(
    db_name: str,
    tables: list[TableMeta],
    selected_schema: dict[str, dict[str, dict[str, Any]]],
    inventory_preview_cols: int = 6,
    detail_max_cols: int | None = None,
) -> str:
    selected_tables_all = [
        t for t in tables
        if t.schema in selected_schema and t.table in selected_schema[t.schema]
    ]

    selected_tables = [
        t for t in selected_tables_all
        if should_display_table(t, selected_schema[t.schema][t.table])
    ]

    lines = [
        f"[DATABASE] {db_name}",
        "Dialect: Snowflake",
        "",
        "You are given filtered schema metadata selected from a schema-linking result.",
        "Only the mentioned tables and columns are retained.",
        "If a table is selected only by role=primary_definition and no concrete columns are mentioned, all columns of that table are shown.",
        "",
        build_inventory_block_from_selection(
            selected_tables,
            selected_schema,
            preview_cols=inventory_preview_cols,
        ),
        "",
        "[TABLE DETAILS]",
    ]

    current_schema = None
    for table in selected_tables:
        if table.schema != current_schema:
            current_schema = table.schema
            lines.append("")
            lines.append(f"## Detailed Schema: {current_schema}")

        lines.append("")
        lines.append(
            build_selected_table_detail_block(
                table=table,
                table_selection=selected_schema[table.schema][table.table],
                include_examples=True,
                include_desc=True,
                max_cols=detail_max_cols,
            )
        )

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-root",
        type=Path,
        required=True,
        help="Path like: spider2-snow/resource/databases/NEW_YORK_CITIBIKE_1",
    )
    parser.add_argument(
        "--db-name",
        type=str,
        default="NEW_YORK_CITIBIKE_1",
    )
    parser.add_argument(
        "--focus-json",
        type=Path,
        default=None,
        help='Optional JSON file: {"SCHEMA": ["TABLE1", "TABLE2"]}',
    )
    parser.add_argument(
        "--linking-json",
        type=Path,
        default=None,
        help="Schema linking json with top-level keys: entities / attributes.",
    )
    parser.add_argument(
        "--selection-output",
        type=Path,
        default=Path("schema_linking_selection.json"),
        help="Output json for extracted entity/attribute/schema selection.",
    )
    parser.add_argument(
        "--inventory-preview-cols",
        type=int,
        default=6,
    )
    parser.add_argument(
        "--detail-max-cols",
        type=int,
        default=None,
        help="Optional cap for number of detailed columns per table.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("schema_injection_prompt.txt"),
    )
    args = parser.parse_args()

    tables = load_database(args.db_root)

    # -------------------------
    # linking-based mode
    # -------------------------
    if args.linking_json is not None:
        linking_doc = load_json(args.linking_json)
        selection_output_dict, selected_schema = build_selection_from_linking(linking_doc)

        prompt_text = build_prompt_text_from_selection(
            db_name=args.db_name,
            tables=tables,
            selected_schema=selected_schema,
            inventory_preview_cols=args.inventory_preview_cols,
            detail_max_cols=args.detail_max_cols,
        )

        args.selection_output.write_text(
            json.dumps(selection_output_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        args.output.write_text(prompt_text, encoding="utf-8")

        selected_table_count = sum(len(v) for v in selected_schema.values())
        selected_col_count = sum(
            len(tbl_info["columns"])
            for schema_info in selected_schema.values()
            for tbl_info in schema_info.values()
        )

        visible_table_count = 0
        for t in tables:
            if t.schema in selected_schema and t.table in selected_schema[t.schema]:
                if should_display_table(t, selected_schema[t.schema][t.table]):
                    visible_table_count += 1

        print(f"selection json wrote: {args.selection_output}")
        print(f"prompt wrote: {args.output}")
        print(f"selected tables (raw): {selected_table_count}")
        print(f"visible tables in prompt: {visible_table_count}")
        print(f"explicitly selected columns: {selected_col_count}")
        return

    # -------------------------
    # original focus-based mode
    # -------------------------
    focus_map = load_focus_map(args.focus_json)

    text = build_prompt_text(
        db_name=args.db_name,
        tables=tables,
        focus_map=focus_map,
        inventory_preview_cols=args.inventory_preview_cols,
        detail_max_cols=args.detail_max_cols,
    )

    args.output.write_text(text, encoding="utf-8")

    total_tables = len(tables)
    total_cols = sum(len(t.columns) for t in tables)
    detail_tables = resolve_detail_tables(tables, focus_map)

    print(f"wrote: {args.output}")
    print(f"tables: {total_tables}")
    print(f"columns: {total_cols}")
    print(f"detailed tables: {len(detail_tables)}")
    print("focus map:")
    print(json.dumps(focus_map, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()