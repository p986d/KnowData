from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.nl2sql.providers.reforce_backend_patch import patch_nl2sql_upstream
from src.nl2sql.providers.reforce_wrapper_common import (
    load_database_tables_from_db_root,
    load_upstream_module,
)

DEFAULT_M1_SCHEMA_LINK_PROMPT_MODE = "ce_multi_sql"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Knowdata wrapper around ReFoRCE online_nl2sql.py with db_root support."
    )
    parser.add_argument("--api_key", required=True)
    parser.add_argument("--base_url", required=True)
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--db_id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--external_knowledge_path", default=None)
    parser.add_argument("--spider2_root", required=True)
    parser.add_argument("--upstream_script", required=True)
    parser.add_argument("--output_path", default=None)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--schema_link_temperature", type=float, default=0.0)
    parser.add_argument(
        "--schema_link_prompt_mode",
        choices=["ce_multi_sql", "single_sql"],
        default=DEFAULT_M1_SCHEMA_LINK_PROMPT_MODE,
    )
    parser.add_argument("--shortlist_trigger", type=int, default=18)
    parser.add_argument("--max_shortlist_tables", type=int, default=24)
    parser.add_argument("--sample_row_limit", type=int, default=2)
    parser.add_argument("--sample_value_max_chars", type=int, default=300)
    parser.add_argument("--similar_tables_hint_limit", type=int, default=12)
    parser.add_argument("--num_votes", type=int, default=4)
    parser.add_argument("--max_workers", type=int, default=4)
    parser.add_argument("--max_iter", type=int, default=5)
    parser.add_argument("--timeout_seconds", type=float, default=600.0)
    parser.add_argument("--generation_model", default=None)
    parser.add_argument("--column_exploration_model", default=None)
    parser.add_argument("--vote_model", default=None)
    parser.add_argument("--skip_schema_linking", action="store_true")
    parser.add_argument("--skip_self_refinement", action="store_true")
    parser.add_argument("--skip_column_exploration", action="store_true")
    parser.add_argument("--skip_vote", action="store_true")
    parser.add_argument("--disable_random_vote_for_tie", action="store_true")
    parser.add_argument("--disable_final_choose", action="store_true")
    parser.add_argument("--disable_early_stop", action="store_true")
    parser.add_argument("--return_candidates_only", action="store_true")
    parser.add_argument("--backend", default="snowflake")
    parser.add_argument("--dialect", default=None)
    parser.add_argument("--snowflake_account", default=None)
    parser.add_argument("--snowflake_user", default=None)
    parser.add_argument("--snowflake_password", default=None)
    parser.add_argument("--snowflake_role", default=None)
    parser.add_argument("--snowflake_warehouse", default=None)
    parser.add_argument("--mysql_host", default=None)
    parser.add_argument("--mysql_port", type=int, default=None)
    parser.add_argument("--mysql_user", default=None)
    parser.add_argument("--mysql_password", default=None)
    parser.add_argument("--mysql_database", default=None)
    parser.add_argument("--db_root", default=None)
    parser.add_argument("--database_source", default=None)
    return parser.parse_args()


def resolve_upstream_runner(
    upstream,
    *,
    schema_link_prompt_mode: str,
):
    if hasattr(upstream, "run_online_nl2sql"):
        return upstream.run_online_nl2sql, {}
    if hasattr(upstream, "run_online_nl2sql_gen_sl_m1"):
        return upstream.run_online_nl2sql_gen_sl_m1, {
            "schema_link_prompt_mode": schema_link_prompt_mode,
        }
    raise AttributeError(
        "Unsupported ReFoRCE upstream module: expected `run_online_nl2sql` "
        "or `run_online_nl2sql_gen_sl_m1`."
    )


def validate_backend_args(args: argparse.Namespace) -> tuple[str, str]:
    backend = str(args.backend or "snowflake").strip().lower()
    dialect = str(args.dialect or backend).strip().lower()
    if bool(getattr(args, "return_candidates_only", False)):
        if backend not in {"snowflake", "mysql"}:
            raise ValueError(f"Unsupported backend: {backend}")
        return backend, dialect

    if backend == "snowflake":
        required = {
            "snowflake_account": args.snowflake_account,
            "snowflake_user": args.snowflake_user,
            "snowflake_password": args.snowflake_password,
        }
    elif backend == "mysql":
        required = {
            "mysql_host": args.mysql_host,
            "mysql_port": args.mysql_port,
            "mysql_user": args.mysql_user,
            "mysql_password": args.mysql_password,
            "mysql_database": args.mysql_database,
        }
    else:
        raise ValueError(f"Unsupported backend: {backend}")

    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise ValueError(
            f"Missing required arguments for backend `{backend}`: {', '.join(missing)}"
        )
    return backend, dialect


def main() -> None:
    args = parse_args()
    backend, dialect = validate_backend_args(args)
    upstream = load_upstream_module(
        args.upstream_script,
        module_name="knowdata_reforce_online_nl2sql_upstream",
    )

    external_knowledge = ""
    if args.external_knowledge_path:
        external_knowledge = Path(args.external_knowledge_path).read_text(
            encoding="utf-8",
            errors="ignore",
        )

    mysql_credentials = None
    if backend == "mysql" and not args.return_candidates_only:
        mysql_credentials = {
            "host": args.mysql_host,
            "port": args.mysql_port,
            "user": args.mysql_user,
            "password": args.mysql_password,
            "database": args.mysql_database,
        }
    patch_nl2sql_upstream(
        upstream,
        backend=backend,
        dialect=dialect,
        mysql_credentials=mysql_credentials,
    )

    if args.db_root:
        resolved_db_root = Path(args.db_root).resolve()

        def _patched_load_database_tables(spider2_root, db_id):
            return load_database_tables_from_db_root(
                upstream,
                db_root=resolved_db_root,
                db_id=db_id,
                backend=backend,
            )

        upstream.load_database_tables = _patched_load_database_tables
        online_nl2sql_module = sys.modules.get("online_nl2sql")
        if online_nl2sql_module is not None:
            online_nl2sql_module.load_database_tables = _patched_load_database_tables
        online_schema_linking_module = sys.modules.get("online_schema_linking")
        if online_schema_linking_module is not None:
            online_schema_linking_module.load_database_tables = _patched_load_database_tables

    request = SimpleNamespace(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        db_id=args.db_id,
        question=args.question,
        external_knowledge=external_knowledge,
        spider2_root=args.spider2_root,
        output_path=args.output_path,
        temperature=args.temperature,
        schema_link_temperature=args.schema_link_temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.sample_row_limit,
        sample_value_max_chars=args.sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        num_votes=args.num_votes,
        max_workers=args.max_workers,
        max_iter=args.max_iter,
        early_stop=not args.disable_early_stop,
        do_schema_linking=not args.skip_schema_linking,
        do_self_refinement=not args.skip_self_refinement,
        do_column_exploration=not args.skip_column_exploration,
        do_vote=not args.skip_vote,
        random_vote_for_tie=not args.disable_random_vote_for_tie,
        final_choose=not args.disable_final_choose,
        return_candidates_only=args.return_candidates_only,
        generation_model=args.generation_model,
        column_exploration_model=args.column_exploration_model,
        vote_model=args.vote_model,
        timeout_seconds=args.timeout_seconds,
        snowflake_account=args.snowflake_account,
        snowflake_user=args.snowflake_user,
        snowflake_password=args.snowflake_password,
        snowflake_role=args.snowflake_role,
        snowflake_warehouse=args.snowflake_warehouse,
        backend=backend,
        dialect=dialect,
        mysql_host=args.mysql_host,
        mysql_port=args.mysql_port,
        mysql_user=args.mysql_user,
        mysql_password=args.mysql_password,
        mysql_database=args.mysql_database,
    )
    runner, runner_kwargs = resolve_upstream_runner(
        upstream,
        schema_link_prompt_mode=args.schema_link_prompt_mode,
    )

    try:
        response = runner(request, **runner_kwargs)
    except Exception as exc:
        upstream.progress(
            f"sql_generation_ok=False stage=none error={upstream.compact_text(str(exc), limit=160)}"
        )
        raise

    result_payload = response.get("result")
    if isinstance(result_payload, dict):
        metadata = result_payload.setdefault("metadata", {})
        metadata["vote_model"] = args.vote_model or args.model
        metadata["db_root"] = str(Path(args.db_root).resolve()) if args.db_root else None
        metadata["database_source"] = args.database_source
        metadata["backend"] = backend
        metadata["dialect"] = dialect
        metadata["upstream_script"] = str(Path(args.upstream_script).resolve())
        metadata["schema_link_prompt_mode"] = args.schema_link_prompt_mode

        schema_linking_payload = result_payload.get("schema_linking")
        if isinstance(schema_linking_payload, dict):
            schema_linking_metadata = schema_linking_payload.setdefault("metadata", {})
            schema_linking_metadata["db_root"] = (
                str(Path(args.db_root).resolve()) if args.db_root else None
            )
            schema_linking_metadata["database_source"] = args.database_source
            schema_linking_metadata["backend"] = backend
            schema_linking_metadata["dialect"] = dialect
            schema_linking_metadata["upstream_script"] = str(Path(args.upstream_script).resolve())
            schema_linking_metadata["schema_link_prompt_mode"] = args.schema_link_prompt_mode

        result_path = response.get("result_path")
        if isinstance(result_path, str) and result_path.strip():
            Path(result_path).write_text(
                json.dumps(result_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if not response.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
