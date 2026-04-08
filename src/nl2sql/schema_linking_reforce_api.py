from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from src.nl2sql.base import SchemaLinkingRequest as EngineSchemaLinkingRequest
from src.nl2sql.defaults import (
    DEFAULT_ENGINE_SCRIPT,
    DEFAULT_PROVIDER_LOG_ROOT as DEFAULT_LOG_ROOT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SPIDER2_ROOT,
)
from src.nl2sql.registry import get_engine_provider, resolve_engine_runtime


@dataclass
class SchemaLinkingRequest:
    api_key: str
    base_url: str
    db_id: str
    question: str
    run_prefix: str | None = None
    model: str = "qwen-plus"
    spider2_root: str = str(DEFAULT_SPIDER2_ROOT)
    output_path: str | None = None
    temperature: float = 0.0
    shortlist_trigger: int = 18
    max_shortlist_tables: int = 24
    sample_row_limit: int = 2
    sample_value_max_chars: int = 300
    similar_tables_hint_limit: int = 12
    reforce_root: str = str(DEFAULT_REFORCE_ROOT)
    engine_script: str = str(DEFAULT_ENGINE_SCRIPT)
    python_executable: str = sys.executable or "python"
    timeout_seconds: int | None = None


def run_schema_linking(
    request: SchemaLinkingRequest,
    *,
    raise_on_error: bool = False,
) -> dict[str, object]:
    provider = get_engine_provider("reforce")
    runtime = resolve_engine_runtime(
        provider_name="reforce",
        spider2_root=request.spider2_root,
        reforce_root=request.reforce_root,
        engine_script=request.engine_script,
        python_executable=request.python_executable,
    )
    result = provider.run_schema_linking(
        EngineSchemaLinkingRequest(
            api_key=request.api_key,
            base_url=request.base_url,
            db_id=request.db_id,
            question=request.question,
            run_prefix=request.run_prefix,
            model=request.model,
            output_path=request.output_path,
            temperature=request.temperature,
            shortlist_trigger=request.shortlist_trigger,
            max_shortlist_tables=request.max_shortlist_tables,
            sample_row_limit=request.sample_row_limit,
            sample_value_max_chars=request.sample_value_max_chars,
            similar_tables_hint_limit=request.similar_tables_hint_limit,
            timeout_seconds=request.timeout_seconds,
        ),
        runtime=runtime,
        raise_on_error=raise_on_error,
    )
    payload = result.to_payload()
    payload["request"] = asdict(request)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Thin Python API wrapper around ReFoRCE online_schema_linking.py"
    )
    parser.add_argument("--api_key", required=True, help="API key for the OpenAI-compatible endpoint.")
    parser.add_argument("--base_url", required=True, help="Base URL for the OpenAI-compatible endpoint.")
    parser.add_argument("--db_id", required=True, help="Snowflake db_id, for example NEW_YORK_CITIBIKE_1 or GA360.")
    parser.add_argument("--question", default=None, help="Natural-language question for schema linking.")
    parser.add_argument("--question_file", default=None, help="Optional text file containing the question.")
    parser.add_argument(
        "--question-id",
        default=None,
        help="Question ID used as the default log/output directory prefix when --output_path is omitted.",
    )
    parser.add_argument("--model", default="qwen-plus", help="Model name passed through to ReFoRCE.")
    parser.add_argument("--spider2_root", default=str(DEFAULT_SPIDER2_ROOT), help="Path to local Spider2 spider2-snow.")
    parser.add_argument("--output_path", default=None, help="Where ReFoRCE should save its result JSON.")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--shortlist_trigger", type=int, default=18)
    parser.add_argument("--max_shortlist_tables", type=int, default=24)
    parser.add_argument("--sample_row_limit", type=int, default=2)
    parser.add_argument("--sample_value_max_chars", type=int, default=300)
    parser.add_argument("--similar_tables_hint_limit", type=int, default=12)
    parser.add_argument("--reforce_root", default=str(DEFAULT_REFORCE_ROOT), help="ReFoRCE repository root.")
    parser.add_argument("--engine_script", default=str(DEFAULT_ENGINE_SCRIPT), help="Path to online_schema_linking.py.")
    parser.add_argument("--python_executable", default=sys.executable or "python", help="Python interpreter used to invoke ReFoRCE.")
    parser.add_argument("--timeout_seconds", type=int, default=None, help="Optional process timeout.")
    parser.add_argument("--api_output_path", default=None, help="Optional path to save the wrapper response JSON.")
    parser.add_argument("--raise_on_error", action="store_true", help="Raise an exception instead of returning ok=false.")
    return parser.parse_args()


def _load_question_from_args(args: argparse.Namespace) -> str:
    if args.question and args.question.strip():
        return args.question.strip()
    if args.question_file:
        return Path(args.question_file).read_text(encoding="utf-8").strip()
    raise ValueError("Either --question or --question_file must be provided.")


def main() -> None:
    args = parse_args()
    question = _load_question_from_args(args)

    request = SchemaLinkingRequest(
        api_key=args.api_key,
        base_url=args.base_url,
        db_id=args.db_id,
        question=question,
        run_prefix=args.question_id,
        model=args.model,
        spider2_root=args.spider2_root,
        output_path=args.output_path,
        temperature=args.temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.sample_row_limit,
        sample_value_max_chars=args.sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
        python_executable=args.python_executable,
        timeout_seconds=args.timeout_seconds,
    )

    response = run_schema_linking(request, raise_on_error=args.raise_on_error)

    if args.api_output_path:
        api_output_path = Path(args.api_output_path)
        api_output_path.parent.mkdir(parents=True, exist_ok=True)
        api_output_path.write_text(
            json.dumps(response, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(json.dumps(response, indent=2, ensure_ascii=False))

    if not response.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
