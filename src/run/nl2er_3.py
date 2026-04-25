from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.prompt.prompt_builder import PromptBuilder
from src.run.nl2er_2 import (
    DEFAULT_INPUT_PATH,
    DEFAULT_METADATA_ROOT,
    DEFAULT_PROMPT_DIR,
    NL2ERInput,
    read_input_payload,
    serialize_input_payload,
    write_case_metadata,
)
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import (
    build_timestamp,
    emit_step_done_log,
    format_elapsed_seconds,
    resolve_output_path,
    resolve_run_dir,
    resolve_run_log_dir,
    write_json,
)


DEFAULT_LOG_ROOT = Path("log/nl2er_3")
DEFAULT_OUTPUT_FILENAME = "nl2er_subques_gen_st1_output.json"
PROMPT_TEMPLATE_NAME = "NL2ER_subques_gen_st1_v0.0.md"

_DETERMINATE_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "Name": ("Name", "name"),
    "Original Wording": (
        "Original Wording",
        "Original wording",
        "original wording",
        "original_wording",
    ),
    "Meaning": ("Meaning", "meaning"),
}
_UNDERSPECIFIED_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "Name": ("Name", "name"),
    "Original Wording": (
        "Original Wording",
        "Original wording",
        "original wording",
        "original_wording",
    ),
    "Determined Part": (
        "Determined Part",
        "determined part",
        "determined_part",
    ),
    "Underspecified Part": (
        "Underspecified Part",
        "underspecified part",
        "underspecified_part",
    ),
}
_GROUP_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "Group ID": ("Group ID", "group id", "group_id"),
    "Decomposition Rationale": (
        "Decomposition Rationale",
        "decomposition rationale",
        "decomposition_rationale",
    ),
}
_SUBPROBLEM_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "Subproblem ID": ("Subproblem ID", "subproblem id", "subproblem_id"),
    "Question": ("Question", "question"),
    "Role": ("Role", "role"),
}
_TOP_LEVEL_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "Determinate Semantics": (
        "Determinate Semantics",
        "determinate semantics",
        "determinate_semantics",
    ),
    "Underspecified Semantics": (
        "Underspecified Semantics",
        "underspecified semantics",
        "underspecified_semantics",
    ),
    "Candidate Subproblem Groups": (
        "Candidate Subproblem Groups",
        "candidate subproblem groups",
        "candidate_subproblem_groups",
    ),
}


def _get_value_by_alias(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in payload:
            return payload[alias]

    folded_index: dict[str, str] = {}
    for key in payload:
        if isinstance(key, str):
            folded_index[key.casefold()] = key

    for alias in aliases:
        resolved_key = folded_index.get(alias.casefold())
        if resolved_key is not None:
            return payload[resolved_key]
    return None


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_named_records(
    value: Any,
    *,
    field_name: str,
    key_aliases: dict[str, tuple[str, ...]],
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"`{field_name}` must be a list.")

    normalized_records: list[dict[str, str]] = []
    for index, record in enumerate(value):
        location = f"{field_name}[{index}]"
        if not isinstance(record, dict):
            raise ValueError(f"`{location}` must be an object.")

        normalized_record: dict[str, str] = {}
        for normalized_key, aliases in key_aliases.items():
            normalized_record[normalized_key] = _normalize_text(
                _get_value_by_alias(record, aliases)
            )
        normalized_records.append(normalized_record)

    return normalized_records


def normalize_subquestion_generation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(
            "NL2ER subquestion generation output must be a JSON object."
        )

    determinate_semantics = _normalize_named_records(
        _get_value_by_alias(
            payload,
            _TOP_LEVEL_KEY_ALIASES["Determinate Semantics"],
        ),
        field_name="Determinate Semantics",
        key_aliases=_DETERMINATE_KEY_ALIASES,
    )
    underspecified_semantics = _normalize_named_records(
        _get_value_by_alias(
            payload,
            _TOP_LEVEL_KEY_ALIASES["Underspecified Semantics"],
        ),
        field_name="Underspecified Semantics",
        key_aliases=_UNDERSPECIFIED_KEY_ALIASES,
    )

    raw_candidate_groups = _get_value_by_alias(
        payload,
        _TOP_LEVEL_KEY_ALIASES["Candidate Subproblem Groups"],
    )
    if raw_candidate_groups is None:
        raw_candidate_groups = []
    if not isinstance(raw_candidate_groups, list):
        raise ValueError("`Candidate Subproblem Groups` must be a list.")

    candidate_subproblem_groups: list[dict[str, Any]] = []
    for group_index, group in enumerate(raw_candidate_groups):
        location = f"Candidate Subproblem Groups[{group_index}]"
        if not isinstance(group, dict):
            raise ValueError(f"`{location}` must be an object.")

        raw_subproblems = _get_value_by_alias(group, ("Subproblems", "subproblems"))
        normalized_group = {
            "Group ID": _normalize_text(
                _get_value_by_alias(group, _GROUP_KEY_ALIASES["Group ID"])
            ),
            "Decomposition Rationale": _normalize_text(
                _get_value_by_alias(group, _GROUP_KEY_ALIASES["Decomposition Rationale"])
            ),
            "Subproblems": _normalize_named_records(
                raw_subproblems,
                field_name=f"{location}.Subproblems",
                key_aliases=_SUBPROBLEM_KEY_ALIASES,
            ),
        }
        candidate_subproblem_groups.append(normalized_group)

    return {
        "Determinate Semantics": determinate_semantics,
        "Underspecified Semantics": underspecified_semantics,
        "Candidate Subproblem Groups": candidate_subproblem_groups,
    }


def validate_subquestion_generation_integrity(
    payload: dict[str, Any],
) -> dict[str, Any]:
    determinate_semantics = list(payload.get("Determinate Semantics") or [])
    underspecified_semantics = list(payload.get("Underspecified Semantics") or [])
    candidate_groups = list(payload.get("Candidate Subproblem Groups") or [])

    errors: list[str] = []
    warnings: list[str] = []

    if not determinate_semantics:
        warnings.append("No determinate semantics were returned.")

    for index, record in enumerate(determinate_semantics):
        location = f"Determinate Semantics[{index}]"
        if not str(record.get("Name") or "").strip():
            errors.append(f"`{location}.Name` is empty.")
        if not str(record.get("Original Wording") or "").strip():
            errors.append(f"`{location}.Original Wording` is empty.")
        if not str(record.get("Meaning") or "").strip():
            errors.append(f"`{location}.Meaning` is empty.")

    for index, record in enumerate(underspecified_semantics):
        location = f"Underspecified Semantics[{index}]"
        if not str(record.get("Name") or "").strip():
            errors.append(f"`{location}.Name` is empty.")
        if not str(record.get("Original Wording") or "").strip():
            errors.append(f"`{location}.Original Wording` is empty.")
        if not str(record.get("Determined Part") or "").strip():
            errors.append(f"`{location}.Determined Part` is empty.")
        if not str(record.get("Underspecified Part") or "").strip():
            errors.append(f"`{location}.Underspecified Part` is empty.")

    if not candidate_groups:
        errors.append("No candidate subproblem groups were returned.")

    seen_group_ids: set[str] = set()
    total_subproblem_count = 0
    for group_index, group in enumerate(candidate_groups):
        location = f"Candidate Subproblem Groups[{group_index}]"
        group_id = str(group.get("Group ID") or "").strip()
        rationale = str(group.get("Decomposition Rationale") or "").strip()
        subproblems = list(group.get("Subproblems") or [])

        if not group_id:
            errors.append(f"`{location}.Group ID` is empty.")
        elif group_id in seen_group_ids:
            errors.append(f"Duplicate group id `{group_id}`.")
        else:
            seen_group_ids.add(group_id)

        if not rationale:
            errors.append(f"`{location}.Decomposition Rationale` is empty.")
        if not subproblems:
            errors.append(f"`{location}.Subproblems` is empty.")

        seen_subproblem_ids: set[str] = set()
        for subproblem_index, subproblem in enumerate(subproblems):
            sub_location = f"{location}.Subproblems[{subproblem_index}]"
            subproblem_id = str(subproblem.get("Subproblem ID") or "").strip()
            question = str(subproblem.get("Question") or "").strip()
            role = str(subproblem.get("Role") or "").strip()

            if not subproblem_id:
                errors.append(f"`{sub_location}.Subproblem ID` is empty.")
            elif subproblem_id in seen_subproblem_ids:
                errors.append(
                    f"Duplicate subproblem id `{subproblem_id}` within group `{group_id or group_index}`."
                )
            else:
                seen_subproblem_ids.add(subproblem_id)

            if not question:
                errors.append(f"`{sub_location}.Question` is empty.")
            if not role:
                errors.append(f"`{sub_location}.Role` is empty.")

        total_subproblem_count += len(subproblems)

    if not underspecified_semantics:
        warnings.append("No underspecified semantics were returned.")

    passed = not errors
    if passed and warnings:
        summary = "Subquestion generation output passed with warnings."
    elif passed:
        summary = "Subquestion generation output passed."
    else:
        summary = "Subquestion generation output failed integrity validation."

    return {
        "passed": passed,
        "summary": summary,
        "errors": errors,
        "warnings": warnings,
        "determinate_semantics_count": len(determinate_semantics),
        "underspecified_semantics_count": len(underspecified_semantics),
        "candidate_group_count": len(candidate_groups),
        "subproblem_count": total_subproblem_count,
    }


class NL2ERSubquestionGenerator:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload

        settings = load_settings()
        self.llm = LLMClient(settings.llm.get(model_config))
        self.build_prompt = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    def generate_candidate_subproblems(self) -> dict[str, Any]:
        self.build_prompt.register_template(
            name="step_1_generate_candidate_subproblems",
            template_name=PROMPT_TEMPLATE_NAME,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description=(
                "Generate determinate semantics, underspecified semantics, and "
                "candidate subproblem groups for the user intent."
            ),
        )
        prompt = self.build_prompt.build_text(
            "step_1_generate_candidate_subproblems",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
            },
        )

        step = 1
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        self._write_text_log(f"response_{step}.md", response)

        if not response.strip():
            raise RuntimeError(
                "NL2ER subquestion generation LLM returned an empty response. "
                "Check model connectivity, credentials, or prompt validity."
            )

        parsed_response = json_parse(response)
        write_json(self.log_dir / "subquestion_generation_raw.json", parsed_response)
        return normalize_subquestion_generation_payload(parsed_response)

    def run(self) -> dict[str, Any]:
        started_at = time.time()

        step_started_at = time.time()
        result = self.generate_candidate_subproblems()
        write_json(self.log_dir / "subquestion_generation.json", result)
        emit_step_done_log(
            prefix="NL2ER-3",
            step="generate_candidate_subproblems",
            elapsed_seconds=time.time() - step_started_at,
            determinate_count=len(result.get("Determinate Semantics") or []),
            underspecified_count=len(result.get("Underspecified Semantics") or []),
            group_count=len(result.get("Candidate Subproblem Groups") or []),
            subproblem_count=sum(
                len(group.get("Subproblems") or [])
                for group in result.get("Candidate Subproblem Groups") or []
            ),
        )

        step_started_at = time.time()
        integrity_report = validate_subquestion_generation_integrity(result)
        write_json(
            self.log_dir / "subquestion_generation_integrity.json",
            integrity_report,
        )
        emit_step_done_log(
            prefix="NL2ER-3",
            step="validate_subquestion_generation_integrity",
            elapsed_seconds=time.time() - step_started_at,
            ok=integrity_report["passed"],
            passed=integrity_report["passed"],
            error_count=len(integrity_report["errors"]),
            warning_count=len(integrity_report["warnings"]),
        )

        return {
            "result": result,
            "integrity_report": integrity_report,
            "elapsed_seconds": time.time() - started_at,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run NL2ER subquestion generation stage1 from a parameterized JSON input."
        )
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--model-config", default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_payload = read_input_payload(args.input_path, question_id=args.question_id)
    if args.question_id:
        input_payload = replace(input_payload, question_id=str(args.question_id).strip())
    if args.db_id:
        input_payload = replace(input_payload, db_id=str(args.db_id).strip())

    if not input_payload.question_id:
        raise ValueError(
            "`question_id` is required. Provide it in the input JSON or pass --question-id."
        )

    run_timestamp = build_timestamp()
    run_id = f"{input_payload.question_id}_{run_timestamp}"
    log_dir = resolve_run_log_dir(
        run_prefix=input_payload.question_id,
        log_dir=args.log_dir,
        log_root=args.log_root,
        timestamp=run_timestamp,
    )
    metadata_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=args.metadata_dir,
        run_root=args.metadata_root,
        timestamp=run_timestamp,
    )
    output_path = resolve_output_path(
        output_path=args.output_path,
        run_dir=metadata_dir,
        default_filename=DEFAULT_OUTPUT_FILENAME,
    )

    serialized_input = serialize_input_payload(input_payload)
    write_case_metadata(
        metadata_dir=metadata_dir,
        serialized_input=serialized_input,
        source_input_path=args.input_path,
        question_id=input_payload.question_id,
    )
    write_json(log_dir / "input.json", serialized_input)

    print(f"[NL2ER-3] run_id={run_id}")
    print(f"[NL2ER-3] question_id={input_payload.question_id}")
    print(f"[NL2ER-3] db_id={input_payload.db_id}")
    print(f"[NL2ER-3] prompt_template={PROMPT_TEMPLATE_NAME}")

    runner = NL2ERSubquestionGenerator(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=input_payload,
        model_config=args.model_config,
    )
    payload = runner.run()

    write_json(output_path, payload["result"])

    print(f"[NL2ER-3] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
    print(f"[NL2ER-3] output wrote to {output_path}")
    print(f"[NL2ER-3] logs wrote to {log_dir}")
    print(f"[NL2ER-3] metadata wrote to {metadata_dir}")

    if not payload["integrity_report"]["passed"]:
        print("[NL2ER-3] subquestion generation integrity check failed.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
