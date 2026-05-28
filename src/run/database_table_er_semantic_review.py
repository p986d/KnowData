from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.nl2er.er_semantic_review import (
    attach_table_group_contexts as component_attach_table_group_contexts,
    build_table_group_context as component_build_table_group_context,
    collect_column_references as component_collect_column_references,
    collect_er_schema_selection as component_collect_er_schema_selection,
    collect_relevant_columns_from_er_profile as component_collect_relevant_columns_from_er_profile,
    copy_first_present as component_copy_first_present,
    er_review_has_semantic_support as component_er_review_has_semantic_support,
    extract_er_review_response_payload as component_extract_er_review_response_payload,
    normalize_dict_list as component_normalize_dict_list,
    normalize_er_review_profile as component_normalize_er_review_profile,
    read_logical_model as component_read_logical_model,
    resolve_logical_model_path as component_resolve_logical_model_path,
    serialize_context_table_group as component_serialize_context_table_group,
    strip_entity_definitions as component_strip_entity_definitions,
    strip_relation_definitions as component_strip_relation_definitions,
    strip_relation_participants as component_strip_relation_participants,
)
from src.prompt.prompt_builder import PromptBuilder
from src.run import database_table_semantic_sketch as base
from src.er2data.schema_utils import read_json_object, resolve_path
from src.nl2er.input_payloads import positive_int
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


DEFAULT_METADATA_DIR = Path("metadata")
DEFAULT_LOG_ROOT = Path("log/database_table_er_semantic_review")
DEFAULT_OUTPUT_FILENAME = "database_table_er_semantic_review.json"
DEFAULT_SCHEMA_LINKING_OUTPUT_FILENAME = "database_table_er_schema_linking.json"
DEFAULT_LOGICAL_MODEL_FILENAME = base.DEFAULT_NL2ER_OUTPUT_FILENAME
GROUPING_METHOD = base.GROUPING_METHOD
OUTPUT_SOURCE = "database_table_er_semantic_review"
TEMPLATE_KEY = "database_table_er_semantic_review"
TEMPLATE_NAME = "Database_table_er_semantic_review_v0.1.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review every database table group against a question-specific "
            "NL2ER logical model, including direct ER support, intermediate "
            "representations, derived semantics, relevance, and evidence columns."
        )
    )
    parser.add_argument("--input-path", type=Path, default=None)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--logical-model-path", type=Path, default=None)
    parser.add_argument("--logical-model-filename", default=DEFAULT_LOGICAL_MODEL_FILENAME)
    parser.add_argument("--nl2er-output-path", type=Path, default=None)
    parser.add_argument("--nl2er-output-filename", default=base.DEFAULT_NL2ER_OUTPUT_FILENAME)
    parser.add_argument("--question", default=None)
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run from --input-path or --metadata-dir. Supports comma-separated values.",
    )
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--db-hint", default="")
    parser.add_argument("--external-knowledge", default="")
    parser.add_argument(
        "--table-fullname",
        nargs="+",
        default=None,
        help="Optional table fullname filters. A group is kept when any member matches.",
    )
    parser.add_argument(
        "--table-context-scope",
        choices=("none", "schema", "database"),
        default="schema",
        help=(
            "Table-group name context passed to each prompt. `schema` includes "
            "groups in the same namespace as the target group; `database` "
            "includes every discovered group; `none` disables this context."
        ),
    )
    parser.add_argument("--prompt-dir", type=Path, default=base.DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument("--database-root", type=Path, default=None)
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument(
        "--sample-values-per-column",
        type=positive_int,
        default=base.DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    )
    parser.add_argument("--sample-row-limit", type=int, default=base.DEFAULT_SAMPLE_ROW_LIMIT)
    parser.add_argument(
        "--sample-value-max-chars",
        type=int,
        default=base.DEFAULT_SAMPLE_VALUE_MAX_CHARS,
    )
    parser.add_argument("--max-workers", type=positive_int, default=None)
    parser.add_argument(
        "--max-table-concurrency",
        type=positive_int,
        default=None,
        help=(
            "Maximum parallel group review LLM requests. Defaults to "
            f"{base.DEFAULT_TABLE_SKETCH_CONCURRENCY} and is capped at "
            f"{base.MAX_TABLE_SKETCH_CONCURRENCY}."
        ),
    )
    parser.add_argument(
        "--write-schema-linking",
        action="store_true",
        help="Write a schema-linking-compatible file next to the output.",
    )
    parser.add_argument(
        "--schema-linking-output-filename",
        default=DEFAULT_SCHEMA_LINKING_OUTPUT_FILENAME,
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def resolve_logical_model_path(
    *,
    case_dir: Path,
    args: argparse.Namespace,
) -> Path:
    explicit_path = getattr(args, "logical_model_path", None) or getattr(args, "nl2er_output_path", None)
    if explicit_path is not None:
        return resolve_path(explicit_path)
    filename = (
        str(getattr(args, "logical_model_filename", "") or "").strip()
        or str(getattr(args, "nl2er_output_filename", "") or "").strip()
        or DEFAULT_LOGICAL_MODEL_FILENAME
    )
    return (case_dir / filename).resolve()


def read_logical_model(
    *,
    case_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    logical_model_path = resolve_logical_model_path(case_dir=case_dir, args=args)
    if not logical_model_path.exists():
        raise FileNotFoundError(f"Logical model file does not exist: {logical_model_path}")
    payload = read_json_object(logical_model_path)
    standard_keys = {"entities", "relations", "conditions", "operations", "resolve_process"}
    extra = {
        key: value
        for key, value in payload.items()
        if key not in standard_keys
    }
    return {
        "source_path": str(logical_model_path),
        "entities": strip_entity_definitions(payload.get("entities")),
        "relations": strip_relation_definitions(payload.get("relations")),
        "conditions": payload.get("conditions") if isinstance(payload.get("conditions"), list) else [],
        "operations": payload.get("operations") if isinstance(payload.get("operations"), list) else [],
        "resolve_process": payload.get("resolve_process") if isinstance(payload.get("resolve_process"), list) else [],
        "extra": extra,
    }


def copy_first_present(source: dict[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key in source:
            target[key] = source[key]
            return


def strip_entity_definitions(value: Any) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for entity in base.ensure_dict_list(value):
        if not isinstance(entity, dict):
            continue
        definition: dict[str, Any] = {}
        copy_first_present(entity, definition, "entity_name", "name")
        copy_first_present(entity, definition, "desc", "description")
        copy_first_present(entity, definition, "grain")
        definitions.append(definition)
    return definitions


def strip_relation_participants(value: Any) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    for participant in base.ensure_dict_list(value):
        if not isinstance(participant, dict):
            continue
        definition: dict[str, Any] = {}
        copy_first_present(participant, definition, "role")
        copy_first_present(participant, definition, "entity", "entity_name")
        if definition:
            participants.append(definition)
    return participants


def strip_relation_definitions(value: Any) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for relation in base.ensure_dict_list(value):
        if not isinstance(relation, dict):
            continue
        definition: dict[str, Any] = {}
        copy_first_present(relation, definition, "relation_name", "name")
        copy_first_present(relation, definition, "desc", "description")
        copy_first_present(relation, definition, "grain")
        participants = strip_relation_participants(relation.get("participants"))
        if participants:
            definition["participants"] = participants
        definitions.append(definition)
    return definitions


def normalize_er_review_context(
    *,
    case_input: base.DatabaseCaseInput,
    args: argparse.Namespace,
) -> dict[str, Any]:
    context: dict[str, Any] = base.normalize_context(case_input=case_input, args=args)
    logical_model = read_logical_model(case_dir=case_input.case_dir, args=args)
    context["logical_model"] = logical_model
    if not str(context.get("sub_questions") or "").strip():
        context["sub_questions"] = "\n".join(
            str(item or "").strip()
            for item in logical_model.get("resolve_process") or []
            if str(item or "").strip()
        )
    return context


def serialize_context_table_group(group: base.TableGroup) -> dict[str, Any]:
    return {
        "representative_table": group.representative.full_name,
        "member_tables": [member.full_name for member in group.members],
    }


def build_table_group_context(
    *,
    target_group: base.TableGroup,
    table_groups: list[base.TableGroup],
    scope: str,
) -> dict[str, Any]:
    normalized_scope = str(scope or "schema").strip().casefold()
    if normalized_scope == "none":
        context_groups: list[base.TableGroup] = []
    elif normalized_scope == "database":
        context_groups = table_groups
    else:
        target_namespace = target_group.representative.namespace.casefold()
        context_groups = [
            group
            for group in table_groups
            if group.representative.namespace.casefold() == target_namespace
        ]
        normalized_scope = "schema"

    return {
        "scope": normalized_scope,
        "target_group_id": target_group.group_id,
        "target_namespace": target_group.representative.namespace,
        "table_group_count": len(context_groups),
        "table_groups": [
            serialize_context_table_group(group)
            for group in context_groups
        ],
    }


def attach_table_group_contexts(
    *,
    table_groups: list[base.TableGroup],
    group_payloads: list[dict[str, Any]],
    scope: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for group, payload in zip(table_groups, group_payloads):
        target_table = dict(payload.get("target_table") or {})
        target_table["table_group_context"] = build_table_group_context(
            target_group=group,
            table_groups=table_groups,
            scope=scope,
        )
        output.append({**payload, "target_table": target_table})
    return output


def extract_er_review_response_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "table_er_semantic_review",
        "database_table_er_semantic_review",
        "er_semantic_review",
        "table_group_profile",
        "group_profile",
        "profile",
        "result",
    ):
        value = parsed.get(key)
        if isinstance(value, dict):
            merged = dict(value)
            for metadata_key in (
                "group_id",
                "representative_table",
                "is_relevant_supportive",
                "is_relevant",
                "member_table_scope",
                "selected_member_tables",
                "linked_member_tables",
            ):
                if metadata_key in parsed and metadata_key not in merged:
                    merged[metadata_key] = parsed[metadata_key]
            return merged
    return parsed


def normalize_dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in base.ensure_dict_list(value) if isinstance(item, dict)]


def collect_column_references(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return base.unique_nonempty_strings(value)
    if not isinstance(value, list):
        return []

    columns: list[str] = []
    for item in value:
        if isinstance(item, str):
            columns.append(item)
            continue
        if not isinstance(item, dict):
            continue
        columns.extend(
            base.unique_nonempty_strings(
                [
                    item.get("column_fullname"),
                    item.get("column_name"),
                    item.get("name"),
                    item.get("field_name"),
                ]
            )
        )
        columns.extend(base.unique_nonempty_strings(item.get("evidence_columns")))
    return base.unique_nonempty_strings(columns)


def er_review_has_semantic_support(
    *,
    supported_logical_entities: list[dict[str, Any]],
    supported_logical_relations: list[dict[str, Any]],
    derived_representations: list[dict[str, Any]],
    intermediate_representations: list[dict[str, Any]],
    derived_semantics: list[dict[str, Any]],
) -> bool:
    return any(
        (
            supported_logical_entities,
            supported_logical_relations,
            derived_representations,
            intermediate_representations,
            derived_semantics,
        )
    )


def normalize_er_review_profile(
    *,
    parsed: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    payload = extract_er_review_response_payload(parsed)
    supported_logical_entities = normalize_dict_list(payload.get("supported_logical_entities"))
    supported_logical_relations = normalize_dict_list(payload.get("supported_logical_relations"))
    derived_representations = normalize_dict_list(payload.get("derived_representations"))
    intermediate_representations = normalize_dict_list(payload.get("intermediate_representations"))
    derived_semantics = normalize_dict_list(payload.get("derived_semantics"))
    is_relevant = er_review_has_semantic_support(
        supported_logical_entities=supported_logical_entities,
        supported_logical_relations=supported_logical_relations,
        derived_representations=derived_representations,
        intermediate_representations=intermediate_representations,
        derived_semantics=derived_semantics,
    )

    profile_input = dict(payload)
    profile_input["is_relevant_supportive"] = is_relevant
    profile_input["is_relevant"] = is_relevant
    if is_relevant and str(profile_input.get("member_table_scope") or "").strip().casefold() == "none":
        profile_input["member_table_scope"] = "all_members"

    profile = base.normalize_database_group_profile(parsed=profile_input, item=item)
    profile["is_relevant_supportive"] = is_relevant
    profile["is_relevant"] = is_relevant
    profile["supported_logical_entities"] = supported_logical_entities
    profile["supported_logical_relations"] = supported_logical_relations
    profile["derived_representations"] = derived_representations
    profile["intermediate_representations"] = intermediate_representations
    profile["derived_semantics"] = derived_semantics
    profile["other_columns"] = collect_column_references(payload.get("other_columns"))
    profile["linked_columns"] = collect_relevant_columns_from_er_profile(profile)
    profile.pop("semantic_units", None)

    if not is_relevant:
        profile["member_table_scope"] = "none"
        profile["selected_member_tables"] = []
        profile["supported_question_semantics"] = []
        profile["supported_logical_entities"] = []
        profile["supported_logical_relations"] = []
        profile["derived_representations"] = []
        profile["intermediate_representations"] = []
        profile["derived_semantics"] = []
        profile["other_columns"] = []
        profile["linked_columns"] = []
        profile.pop("semantic_units", None)
    return profile


def collect_relevant_columns_from_er_profile(profile: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    columns.extend(base.unique_nonempty_strings(profile.get("linked_columns")))
    columns.extend(base.unique_nonempty_strings(profile.get("other_columns")))
    for key in (
        "supported_logical_entities",
        "supported_logical_relations",
        "derived_representations",
        "intermediate_representations",
        "derived_semantics",
    ):
        for item in normalize_dict_list(profile.get(key)):
            columns.extend(base.unique_nonempty_strings(item.get("evidence_columns")))
            for participant in normalize_dict_list(item.get("supported_participants")):
                columns.extend(base.unique_nonempty_strings(participant.get("evidence_columns")))
    for unit in normalize_dict_list(profile.get("semantic_units")):
        columns.extend(base.collect_evidence_columns_from_unit(unit))
    return base.unique_nonempty_strings(columns)


def collect_er_schema_selection(
    *,
    group_payloads: list[dict[str, Any]],
    group_profiles: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    linked_tables: list[str] = []
    linked_columns: list[str] = []
    seen_tables: set[str] = set()
    seen_columns: set[str] = set()

    def add_table(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_tables:
            return
        linked_tables.append(text)
        seen_tables.add(text)

    def add_column(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_columns:
            return
        linked_columns.append(text)
        seen_columns.add(text)
        table_fullname = base.table_fullname_from_column(text)
        if table_fullname:
            add_table(table_fullname)

    for group_payload, profile in zip(group_payloads, group_profiles):
        if not base.profile_is_relevant_supportive(profile):
            continue
        selected_member_tables = base.normalize_selected_member_tables(profile, group_payload)
        for table_fullname in selected_member_tables:
            add_table(table_fullname)
        for column_fullname in base.resolve_group_column_fullnames(
            collect_relevant_columns_from_er_profile(profile),
            group_payload=group_payload,
            selected_member_tables=selected_member_tables,
        ):
            add_column(column_fullname)
    return linked_tables, linked_columns


class DatabaseTableERSemanticReviewRunner(base.DatabaseTableSemanticSketchRunner):
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        database_root: str | Path | None = None,
        spider2_root: str | Path | None = None,
        sample_values_per_column: int = base.DEFAULT_SAMPLE_VALUES_PER_COLUMN,
        dry_run: bool = False,
        initialize_llm: bool = True,
    ) -> None:
        super().__init__(
            prompt_dir=prompt_dir,
            log_dir=log_dir,
            model_config=model_config,
            reasoning_mode=reasoning_mode,
            database_root=database_root,
            spider2_root=spider2_root,
            sample_values_per_column=sample_values_per_column,
            dry_run=dry_run,
            initialize_llm=initialize_llm,
        )
        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self.prompt_builder.register_template(
            name=TEMPLATE_KEY,
            template_name=TEMPLATE_NAME,
            required_vars=[
                "user_intent",
                "sub_questions",
                "db_id",
                "db_hint",
                "external_knowledge",
                "logical_model",
                "table_group_context",
                "target_table_group",
            ],
        )

    def build_prompt(
        self,
        *,
        context: dict[str, Any],
        target_table: dict[str, Any],
    ) -> str:
        prompt_target_table = dict(target_table)
        sub_questions = str(context.get("sub_questions") or "").strip()
        if sub_questions:
            prompt_target_table["question_context"] = {"sub_questions": sub_questions}
        table_group_context = prompt_target_table.pop("table_group_context", {})
        return self.prompt_builder.build_text(
            TEMPLATE_KEY,
            vars={
                "user_intent": context.get("user_intent", ""),
                "sub_questions": sub_questions,
                "db_id": context.get("db_id", ""),
                "db_hint": context.get("db_hint", ""),
                "external_knowledge": context.get("external_knowledge", ""),
                "logical_model": context.get("logical_model") or {},
                "table_group_context": table_group_context,
                "target_table_group": prompt_target_table,
                "target_table": prompt_target_table,
            },
        )

    @staticmethod
    def build_dry_run_group_profile(item: dict[str, Any]) -> dict[str, Any]:
        profile = base.DatabaseTableSemanticSketchRunner.build_dry_run_group_profile(item)
        semantic_units = base.ensure_dict_list(profile.get("semantic_units"))
        supported_logical_entities = [
            {
                "entity_name": str(unit.get("unit_name") or "").strip(),
                "supported_attributes": [
                    str(attribute.get("name") or "").strip()
                    for attribute in base.ensure_dict_list(unit.get("attributes"))
                    if str(attribute.get("name") or "").strip()
                ],
                "evidence_columns": base.collect_evidence_columns_from_unit(unit),
                "reason": "Dry-run placeholder.",
            }
            for unit in semantic_units
            if str(unit.get("unit_type") or "").strip().casefold() == "entity"
        ]
        is_relevant = bool(supported_logical_entities)
        profile.update(
            {
                "is_relevant_supportive": is_relevant,
                "is_relevant": is_relevant,
                "supported_logical_entities": supported_logical_entities,
                "supported_logical_relations": [],
                "derived_representations": [],
                "intermediate_representations": [],
                "derived_semantics": [],
            }
        )
        profile.pop("semantic_units", None)
        return profile

    def parse_group_responses(
        self,
        *,
        items: list[dict[str, Any]],
        raw_responses: list[str],
    ) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for item, raw_response in zip(items, raw_responses):
            response_path = Path(item["response_path"])
            response_path.write_text(raw_response or "", encoding="utf-8")
            if not raw_response or not raw_response.strip():
                profiles.append(
                    base.DatabaseTableSemanticSketchRunner.build_error_group_profile(
                        item=item,
                        error="LLM returned empty response.",
                    )
                )
                continue
            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                profiles.append(
                    base.DatabaseTableSemanticSketchRunner.build_error_group_profile(
                        item=item,
                        error=f"Failed to parse database table ER semantic review JSON: {exc}",
                    )
                )
                continue
            profile = normalize_er_review_profile(parsed=parsed, item=item)
            profiles.append(profile)
        return profiles

    def prepare_case(
        self,
        *,
        case_input: base.DatabaseCaseInput,
        args: argparse.Namespace,
    ) -> base.PreparedDatabaseCase:
        started_at = time.perf_counter()
        context = normalize_er_review_context(case_input=case_input, args=args)
        if not context.get("user_intent"):
            raise ValueError("Question text is required. Provide --question or an input.json question.")
        if not context.get("db_id"):
            raise ValueError("db_id is required. Provide --db-id or include it in inputs.")

        tables = base.load_database_tables(
            db_id=str(context["db_id"]),
            database_root=self.database_root,
            spider2_root=self.spider2_root,
        )
        table_groups = base.build_table_groups(tables)
        table_groups = base.filter_groups_by_table_fullname(
            table_groups,
            table_filters=base.normalize_casefold_set(args.table_fullname),
        )
        if not table_groups:
            raise ValueError("No table groups were found to analyze.")

        group_payloads = base.group_payloads_from_groups(
            table_groups,
            sample_row_limit=args.sample_row_limit,
            sample_values_per_column=self.sample_values_per_column,
            sample_value_max_chars=args.sample_value_max_chars,
        )
        group_payloads = attach_table_group_contexts(
            table_groups=table_groups,
            group_payloads=group_payloads,
            scope=str(getattr(args, "table_context_scope", "schema") or "schema"),
        )
        return base.PreparedDatabaseCase(
            case_input=case_input,
            runner=self,
            context=context,
            table_groups=table_groups,
            group_payloads=group_payloads,
            started_at=started_at,
        )

    def finalize_case(
        self,
        *,
        prepared: base.PreparedDatabaseCase,
        group_profiles: list[dict[str, Any]],
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        linked_tables, linked_columns = collect_er_schema_selection(
            group_payloads=prepared.group_payloads,
            group_profiles=group_profiles,
        )
        output_path = base.database_case_output_path(
            case_input=prepared.case_input,
            log_dir=self.log_dir,
            output_filename=args.output_filename,
        )
        ok = all(not profile.get("error") for profile in group_profiles)
        logical_model = prepared.context.get("logical_model")
        output_payload = {
            "ok": ok,
            "method": OUTPUT_SOURCE,
            "grouping_method": GROUPING_METHOD,
            "question_id": prepared.context.get("question_id", ""),
            "db_id": prepared.context.get("db_id", ""),
            "question": prepared.context.get("user_intent", ""),
            "sub_questions": prepared.context.get("sub_questions", ""),
            "logical_entity_relation_model": logical_model,
            "output_path": str(output_path),
            "log_dir": str(self.log_dir),
            "raw_table_count": sum(len(group.members) for group in prepared.table_groups),
            "group_count": len(prepared.table_groups),
            "relevant_group_count": sum(
                1 for profile in group_profiles if base.profile_is_relevant_supportive(profile)
            ),
            "linked_tables": linked_tables,
            "linked_columns": linked_columns,
            "group_profiles": group_profiles,
        }
        write_json(output_path, output_payload)
        write_json(self.log_dir / "output.json", output_payload)

        if bool(getattr(args, "write_schema_linking", False)):
            output_linked_tables = base.normalize_schema_linking_names_for_output(
                linked_tables,
                db_id=str(prepared.context.get("db_id", "")),
            )
            output_linked_columns = base.normalize_schema_linking_names_for_output(
                linked_columns,
                db_id=str(prepared.context.get("db_id", "")),
            )
            schema_linking_payload = base.build_schema_linking_with_overall_result(
                schema_linking_payload={},
                context=prepared.context,
                linked_tables=output_linked_tables,
                linked_columns=output_linked_columns,
                source=OUTPUT_SOURCE,
            )
            schema_linking_output_path = (
                output_path.parent
                / str(getattr(args, "schema_linking_output_filename", DEFAULT_SCHEMA_LINKING_OUTPUT_FILENAME))
            ).resolve()
            write_json(schema_linking_output_path, schema_linking_payload)
            write_json(self.log_dir / "schema_linking.json", schema_linking_payload)

        return {
            "ok": ok,
            "question_id": prepared.context.get("question_id", ""),
            "db_id": prepared.context.get("db_id", ""),
            "group_count": len(prepared.table_groups),
            "relevant_group_count": output_payload["relevant_group_count"],
            "output_path": str(output_path),
            "log_dir": str(self.log_dir),
        }


def run_single_case(
    *,
    case_input: base.DatabaseCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    runner = DatabaseTableERSemanticReviewRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        database_root=args.database_root,
        spider2_root=args.spider2_root,
        sample_values_per_column=args.sample_values_per_column,
        dry_run=bool(args.dry_run),
    )
    payload = runner.run_case(case_input=case_input, args=args)
    output_path = base.database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    return {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id", ""),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "group_count": int(payload.get("group_count") or 0),
        "relevant_group_count": int(payload.get("relevant_group_count") or 0),
        "dry_run": bool(args.dry_run),
    }


def prepare_single_case_for_batch_safe(
    *,
    case_input: base.DatabaseCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> base.PreparedDatabaseCase | dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    output_path = base.database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    try:
        runner = DatabaseTableERSemanticReviewRunner(
            prompt_dir=args.prompt_dir,
            log_dir=log_dir,
            model_config=args.model_config,
            reasoning_mode=args.reasoning_mode,
            database_root=args.database_root,
            spider2_root=args.spider2_root,
            sample_values_per_column=args.sample_values_per_column,
            dry_run=bool(args.dry_run),
            initialize_llm=False,
        )
        return runner.prepare_case(case_input=case_input, args=args)
    except Exception as exc:
        log_dir.mkdir(parents=True, exist_ok=True)
        failure_payload = {
            "ok": False,
            "question_id": case_input.case_dir.name,
            "case_dir": str(case_input.case_dir),
            "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "dry_run": bool(args.dry_run),
        }
        write_json(log_dir / "error.json", failure_payload)
        write_json(output_path, failure_payload)
        return failure_payload


def summarize_case_payload(
    *,
    payload: dict[str, Any],
    case_input: base.DatabaseCaseInput,
    log_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_path = base.database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    return {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id", ""),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "group_count": int(payload.get("group_count") or 0),
        "relevant_group_count": int(payload.get("relevant_group_count") or 0),
        "dry_run": bool(args.dry_run),
    }


def finalize_prepared_case(
    *,
    prepared: base.PreparedDatabaseCase,
    group_profiles: list[dict[str, Any]],
    args: argparse.Namespace,
    review_elapsed_seconds: float,
) -> dict[str, Any]:
    emit_step_done_log(
        prefix="DATABASE_TABLE_ER_SEMANTIC_REVIEW",
        step="group_review",
        elapsed_seconds=review_elapsed_seconds,
        groups=len(group_profiles),
        dry_run=bool(prepared.runner.dry_run),
    )
    payload = prepared.runner.finalize_case(
        prepared=prepared,
        group_profiles=group_profiles,
        args=args,
    )
    return summarize_case_payload(
        payload=payload,
        case_input=prepared.case_input,
        log_dir=prepared.runner.log_dir,
        args=args,
    )


# Compatibility exports for callers that still import algorithm helpers from
# this run module. Implementations live in src.nl2er.er_semantic_review.
def resolve_logical_model_path(
    *,
    case_dir: Path,
    args: argparse.Namespace,
) -> Path:
    return component_resolve_logical_model_path(
        case_dir=case_dir,
        logical_model_path=getattr(args, "logical_model_path", None),
        nl2er_output_path=getattr(args, "nl2er_output_path", None),
        logical_model_filename=str(getattr(args, "logical_model_filename", "") or ""),
        nl2er_output_filename=str(getattr(args, "nl2er_output_filename", "") or ""),
        default_filename=DEFAULT_LOGICAL_MODEL_FILENAME,
    )


def read_logical_model(
    *,
    case_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    return component_read_logical_model(
        case_dir=case_dir,
        logical_model_path=getattr(args, "logical_model_path", None),
        nl2er_output_path=getattr(args, "nl2er_output_path", None),
        logical_model_filename=str(getattr(args, "logical_model_filename", "") or ""),
        nl2er_output_filename=str(getattr(args, "nl2er_output_filename", "") or ""),
        default_filename=DEFAULT_LOGICAL_MODEL_FILENAME,
    )


copy_first_present = component_copy_first_present
strip_entity_definitions = component_strip_entity_definitions
strip_relation_participants = component_strip_relation_participants
strip_relation_definitions = component_strip_relation_definitions
serialize_context_table_group = component_serialize_context_table_group
build_table_group_context = component_build_table_group_context
attach_table_group_contexts = component_attach_table_group_contexts
extract_er_review_response_payload = component_extract_er_review_response_payload
normalize_dict_list = component_normalize_dict_list
collect_column_references = component_collect_column_references
er_review_has_semantic_support = component_er_review_has_semantic_support
normalize_er_review_profile = component_normalize_er_review_profile
collect_relevant_columns_from_er_profile = component_collect_relevant_columns_from_er_profile
collect_er_schema_selection = component_collect_er_schema_selection


def main() -> None:
    args = parse_args()
    requested_question_ids = base.normalize_name_filters(args.question_id)
    batch_root, case_inputs = base.discover_database_case_inputs(
        input_path=args.input_path,
        metadata_dir=args.metadata_dir,
        output_path=args.output_path,
        output_filename=args.output_filename,
        requested_question_ids=requested_question_ids,
    )
    case_inputs = base.filter_cases_by_question_id(
        cases=case_inputs,
        requested_question_ids=requested_question_ids,
    )
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()

    print(f"[DATABASE_TABLE_ER_SEMANTIC_REVIEW] batch_root={batch_root}")
    print(f"[DATABASE_TABLE_ER_SEMANTIC_REVIEW] discovered_cases={len(case_inputs)}")
    print(f"[DATABASE_TABLE_ER_SEMANTIC_REVIEW] run_timestamp={run_timestamp}")
    print(f"[DATABASE_TABLE_ER_SEMANTIC_REVIEW] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[DATABASE_TABLE_ER_SEMANTIC_REVIEW] log_root={resolve_path(args.log_root)}")
    print(f"[DATABASE_TABLE_ER_SEMANTIC_REVIEW] dry_run={bool(args.dry_run)}")

    completed_summaries: list[dict[str, Any]] = []
    prepared_cases: list[base.PreparedDatabaseCase] = []
    for case_input in case_inputs:
        prepared_or_failure = prepare_single_case_for_batch_safe(
            case_input=case_input,
            run_timestamp=run_timestamp,
            args=args,
        )
        if isinstance(prepared_or_failure, base.PreparedDatabaseCase):
            prepared_cases.append(prepared_or_failure)
        else:
            completed_summaries.append(prepared_or_failure)

    global_prompts: list[str] = []
    case_items: list[list[dict[str, Any]]] = []
    case_raw_responses: list[list[str]] = [[] for _ in prepared_cases]
    item_case_indexes: list[int] = []

    for case_index, prepared in enumerate(prepared_cases):
        items, prompts = prepared.runner.prepare_group_prompts(
            context=prepared.context,
            group_payloads=prepared.group_payloads,
        )
        case_items.append(items)
        for prompt in prompts:
            global_prompts.append(prompt)
            item_case_indexes.append(case_index)

    print(
        "[DATABASE_TABLE_ER_SEMANTIC_REVIEW] prepared "
        f"cases={len(prepared_cases)} "
        f"failed_cases={len(completed_summaries)} "
        f"group_prompt_count={len(global_prompts)}"
    )

    review_started_at = time.perf_counter()
    if args.dry_run:
        for case_index, _prepared in enumerate(prepared_cases):
            case_raw_responses[case_index] = []
    elif global_prompts:
        requested_concurrency = args.max_table_concurrency or args.max_workers
        max_concurrency = base.resolve_table_sketch_concurrency(requested_concurrency)
        print(
            "[DATABASE_TABLE_ER_SEMANTIC_REVIEW] global_group_review_batch "
            f"case_count={len(prepared_cases)} "
            f"group_prompt_count={len(global_prompts)} "
            f"max_concurrency={max_concurrency}"
        )
        settings = load_settings()
        config = apply_reasoning_mode(settings.llm.get(args.model_config), args.reasoning_mode)
        llm = LLMClient(config)
        raw_responses = llm.batch_single_turn(
            global_prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(global_prompts) > 1,
            progress_desc="Database table ER semantic review",
        )
        for case_index, raw_response in zip(item_case_indexes, raw_responses):
            case_raw_responses[case_index].append(raw_response)

    review_elapsed_seconds = time.perf_counter() - review_started_at
    for case_index, prepared in enumerate(prepared_cases):
        if args.dry_run:
            group_profiles = [
                prepared.runner.build_dry_run_group_profile(item)
                for item in case_items[case_index]
            ]
        else:
            group_profiles = prepared.runner.parse_group_responses(
                items=case_items[case_index],
                raw_responses=case_raw_responses[case_index],
            )
        completed_summaries.append(
            finalize_prepared_case(
                prepared=prepared,
                group_profiles=group_profiles,
                args=args,
                review_elapsed_seconds=review_elapsed_seconds,
            )
        )

    success_count = sum(1 for item in completed_summaries if item.get("ok"))
    print(
        "[DATABASE_TABLE_ER_SEMANTIC_REVIEW] finalized "
        f"cases={len(completed_summaries)} "
        f"ok_cases={success_count} "
        f"failed_cases={len(completed_summaries) - success_count}"
    )
    batch_summary = {
        "timestamp": run_timestamp,
        "batch_root": str(batch_root),
        "log_root": str(resolve_path(args.log_root)),
        "case_count": len(case_inputs),
        "ok_cases": success_count,
        "failed_cases": len(case_inputs) - success_count,
        "dry_run": bool(args.dry_run),
        "cases": completed_summaries,
    }
    batch_summary_path = resolve_path(args.log_root) / run_timestamp / "batch_summary.json"
    write_json(batch_summary_path, batch_summary)
    emit_step_done_log(
        prefix="DATABASE_TABLE_ER_SEMANTIC_REVIEW",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
