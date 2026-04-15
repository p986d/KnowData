from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.nl2sql.providers.reforce_wrapper_common import (
    load_database_tables_from_db_root,
    load_upstream_module,
)
from src.nl2sql.providers.reforce_backend_patch import patch_schema_linking_upstream


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Knowdata wrapper around ReFoRCE online_schema_linking.py with db_root support."
    )
    parser.add_argument("--api_key", required=True)
    parser.add_argument("--base_url", required=True)
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--db_id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--spider2_root", required=True)
    parser.add_argument("--upstream_script", required=True)
    parser.add_argument("--output_path", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--shortlist_trigger", type=int, default=18)
    parser.add_argument("--max_shortlist_tables", type=int, default=24)
    parser.add_argument("--sample_row_limit", type=int, default=2)
    parser.add_argument("--sample_value_max_chars", type=int, default=300)
    parser.add_argument("--similar_tables_hint_limit", type=int, default=12)
    parser.add_argument("--db_root", default=None)
    parser.add_argument("--database_source", default=None)
    parser.add_argument("--backend", default="snowflake")
    parser.add_argument("--dialect", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    upstream = load_upstream_module(
        args.upstream_script,
        module_name="knowdata_reforce_online_schema_linking_upstream",
    )
    patch_schema_linking_upstream(
        upstream,
        backend=str(args.backend or "snowflake").strip().lower(),
    )

    spider2_root = Path(args.spider2_root)
    output_path = upstream.ensure_output_path(args.db_id, args.output_path)

    if args.db_root:
        tables = load_database_tables_from_db_root(
            upstream,
            db_root=args.db_root,
            db_id=args.db_id,
            backend=str(args.backend or "snowflake").strip().lower(),
        )
    else:
        tables = upstream.load_database_tables(spider2_root, args.db_id)

    groups = upstream.build_table_groups(tables)
    external_knowledge, external_docs = upstream.load_external_knowledge(
        spider2_root,
        args.db_id,
    )

    client = upstream.create_client(args.api_key, args.base_url)
    shortlisted_groups = upstream.shortlist_groups(
        client=client,
        model=args.model,
        db_id=args.db_id,
        question=args.question,
        external_knowledge=external_knowledge,
        groups=groups,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        temperature=args.temperature,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
    )

    selected_groups = []
    for group in shortlisted_groups:
        is_linked, columns = upstream.link_group(
            client=client,
            model=args.model,
            group=group,
            question=args.question,
            external_knowledge=external_knowledge,
            sample_row_limit=args.sample_row_limit,
            sample_value_max_chars=args.sample_value_max_chars,
            similar_tables_hint_limit=args.similar_tables_hint_limit,
            temperature=args.temperature,
        )
        if is_linked:
            selected_groups.append((group, columns))

    linked_tables, linked_columns = upstream.expand_results(selected_groups)
    payload = {
        "db_id": args.db_id,
        "question": args.question,
        "linked_tables": linked_tables,
        "linked_columns": linked_columns,
        "metadata": {
            "model": args.model,
            "base_url": args.base_url,
            "spider2_root": str(spider2_root),
            "db_root": str(Path(args.db_root).resolve()) if args.db_root else None,
            "database_source": args.database_source,
            "backend": str(args.backend or "snowflake").strip().lower(),
            "dialect": str(args.dialect or args.backend or "snowflake").strip().lower(),
            "upstream_script": str(Path(args.upstream_script).resolve()),
            "external_docs": external_docs,
            "raw_table_count": len(tables),
            "representative_table_count": len(groups),
            "shortlisted_representative_table_count": len(shortlisted_groups),
            "expanded_linked_table_count": len(linked_tables),
            "expanded_linked_column_count": len(linked_columns),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

    print(f"Schema linking saved to: {output_path}")
    print(f"Linked tables: {len(linked_tables)}")
    print(f"Linked columns: {len(linked_columns)}")


if __name__ == "__main__":
    main()
