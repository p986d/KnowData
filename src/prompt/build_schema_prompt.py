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


def first_non_null_sample(sample_rows: list[dict[str, Any]], col_name: str) -> Any:
    for row in sample_rows:
        if col_name in row and row[col_name] is not None:
            return row[col_name]
    return None


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
            wanted = set(focus_map[schema])
            for t in schema_tables:
                if t.table in wanted:
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
        help="Optional JSON file: {\"SCHEMA\": [\"TABLE1\", \"TABLE2\"]}",
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

    focus_map = load_focus_map(args.focus_json)
    tables = load_database(args.db_root)

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