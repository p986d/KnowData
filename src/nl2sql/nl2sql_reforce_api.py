from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from src.nl2sql.base import NL2SQLRequest as EngineNL2SQLRequest
from src.nl2sql.defaults import (
    DEFAULT_NL2SQL_ENGINE_SCRIPT as DEFAULT_ENGINE_SCRIPT,
    DEFAULT_PROVIDER_LOG_ROOT as DEFAULT_LOG_ROOT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SPIDER2_ROOT,
)
from src.nl2sql.registry import get_engine_provider, resolve_engine_runtime


@dataclass
class NL2SQLRequest:
    question: str
    db_id: str
    external_knowledge: str = ""
    run_prefix: str | None = None


def run_nl2sql(
    request: NL2SQLRequest,
    *,
    llm_config_name: str | None = None,
    spider2_root: str | Path = DEFAULT_SPIDER2_ROOT,
    reforce_root: str | Path = DEFAULT_REFORCE_ROOT,
    engine_script: str | Path = DEFAULT_ENGINE_SCRIPT,
    output_path: str | None = None,
    temperature: float = 0.7,
    schema_link_temperature: float = 0.0,
    shortlist_trigger: int = 18,
    max_shortlist_tables: int = 24,
    sample_row_limit: int = 2,
    sample_value_max_chars: int = 300,
    similar_tables_hint_limit: int = 12,
    num_votes: int = 4,
    max_workers: int = 4,
    max_iter: int = 5,
    timeout_seconds: float = 600.0,
    generation_model: str | None = None,
    column_exploration_model: str | None = None,
    return_candidates_only: bool = False,
    raise_on_error: bool = False,
) -> dict[str, object]:
    provider = get_engine_provider("reforce")
    runtime = resolve_engine_runtime(
        provider_name="reforce",
        spider2_root=spider2_root,
        reforce_root=reforce_root,
        nl2sql_engine_script=engine_script,
    )
    result = provider.run_nl2sql(
        EngineNL2SQLRequest(
            question=request.question,
            db_id=request.db_id,
            external_knowledge=request.external_knowledge,
            run_prefix=request.run_prefix,
            llm_config_name=llm_config_name,
            output_path=output_path,
            temperature=temperature,
            schema_link_temperature=schema_link_temperature,
            shortlist_trigger=shortlist_trigger,
            max_shortlist_tables=max_shortlist_tables,
            sample_row_limit=sample_row_limit,
            sample_value_max_chars=sample_value_max_chars,
            similar_tables_hint_limit=similar_tables_hint_limit,
            num_votes=num_votes,
            max_workers=max_workers,
            max_iter=max_iter,
            timeout_seconds=timeout_seconds,
            generation_model=generation_model,
            column_exploration_model=column_exploration_model,
            return_candidates_only=return_candidates_only,
        ),
        runtime=runtime,
        raise_on_error=raise_on_error,
    )
    payload = result.to_payload()
    payload["request"] = {
        "db_id": request.db_id,
        "question": request.question,
        "external_knowledge": request.external_knowledge,
    }
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Thin Python API wrapper around ReFoRCE online_nl2sql.py"
    )
    parser.add_argument("--db_id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--external_knowledge", default="")
    parser.add_argument(
        "--question-id",
        default=None,
        help="Question ID used as the default log/output directory prefix when --output_path is omitted.",
    )
    parser.add_argument("--llm_config_name", default=None)
    parser.add_argument("--spider2_root", default=str(DEFAULT_SPIDER2_ROOT))
    parser.add_argument("--reforce_root", default=str(DEFAULT_REFORCE_ROOT))
    parser.add_argument("--engine_script", default=str(DEFAULT_ENGINE_SCRIPT))
    parser.add_argument("--output_path", default=None)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--schema_link_temperature", type=float, default=0.0)
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
    parser.add_argument("--return_candidates_only", action="store_true")
    parser.add_argument("--raise_on_error", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    response = run_nl2sql(
        NL2SQLRequest(
            question=args.question,
            db_id=args.db_id,
            external_knowledge=args.external_knowledge,
            run_prefix=args.question_id,
        ),
        llm_config_name=args.llm_config_name,
        spider2_root=args.spider2_root,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
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
        timeout_seconds=args.timeout_seconds,
        generation_model=args.generation_model,
        column_exploration_model=args.column_exploration_model,
        return_candidates_only=args.return_candidates_only,
        raise_on_error=args.raise_on_error,
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))
    if not response.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
