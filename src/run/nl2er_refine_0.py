from __future__ import annotations

import argparse
import csv
import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.nl2sql.defaults import DEFAULT_REFORCE_ROOT, DEFAULT_SPIDER2_ROOT
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


DEFAULT_METADATA_DIR = Path("metadata")
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/nl2er_refine_0")
DEFAULT_INPUT_FILENAME = "nl2er_output.json"
DEFAULT_SCHEMA_LINKING_FILENAME = "schema_linking.json"
DEFAULT_OUTPUT_FILENAME = "nl2er_refine_0_output.json"
UNIT_ANALYSIS_TEMPLATE_KEY = "nl2er_refine_0_unit_analysis"
UNIT_ANALYSIS_TEMPLATE_NAME = "NL2ER_refine_0_unit_analysis.md"
GLOBAL_REFINE_TEMPLATE_KEY = "nl2er_refine_0_global_refine"
GLOBAL_REFINE_TEMPLATE_NAME = "NL2ER_refine_0_global_refine.md"
SUPPORTED_UNIT_TYPES = {"entity", "relationship", "connection"}
_KEY_NORMALIZE_RE = re.compile(r"[^0-9a-z]+")
DEFAULT_SAMPLE_VALUES_PER_COLUMN = 5


@dataclass(slots=True)
class CaseInput:
    input_path: Path
    schema_linking_path: Path
    case_dir: Path
    relative_case_dir: Path


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a positive integer, got `{value}`."
        ) from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.")
    return parsed


def resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    return resolved


def read_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object at {file_path}, got {type(payload).__name__}."
        )
    return payload


def discover_case_inputs(metadata_dir: str | Path) -> tuple[Path, list[CaseInput]]:
    root_dir = resolve_path(metadata_dir)
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata path does not exist: {root_dir}")

    if root_dir.is_file():
        if root_dir.name != DEFAULT_INPUT_FILENAME:
            raise ValueError(
                f"Expected `{DEFAULT_INPUT_FILENAME}` when a file path is provided, "
                f"got `{root_dir.name}`."
            )
        schema_linking_path = root_dir.with_name(DEFAULT_SCHEMA_LINKING_FILENAME)
        return root_dir.parent, [
            CaseInput(
                input_path=root_dir,
                schema_linking_path=schema_linking_path,
                case_dir=root_dir.parent,
                relative_case_dir=Path(root_dir.parent.name),
            )
        ]

    direct_input = root_dir / DEFAULT_INPUT_FILENAME
    if direct_input.exists():
        return root_dir, [
            CaseInput(
                input_path=direct_input.resolve(),
                schema_linking_path=(root_dir / DEFAULT_SCHEMA_LINKING_FILENAME).resolve(),
                case_dir=root_dir,
                relative_case_dir=Path(root_dir.name),
            )
        ]

    input_paths = sorted({path.resolve() for path in root_dir.rglob(DEFAULT_INPUT_FILENAME)})
    if not input_paths:
        raise FileNotFoundError(
            f"No `{DEFAULT_INPUT_FILENAME}` files were found under {root_dir}."
        )

    case_inputs: list[CaseInput] = []
    for input_path in input_paths:
        case_dir = input_path.parent
        case_inputs.append(
            CaseInput(
                input_path=input_path,
                schema_linking_path=(case_dir / DEFAULT_SCHEMA_LINKING_FILENAME).resolve(),
                case_dir=case_dir,
                relative_case_dir=case_dir.relative_to(root_dir),
            )
        )
    return root_dir, case_inputs


def normalize_key(value: str) -> str:
    return _KEY_NORMALIZE_RE.sub("", str(value or "").casefold())


def unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        candidates = [values]
    elif isinstance(values, list):
        candidates = values
    else:
        return []

    output: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_attribute_list(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []

    raw_items: list[Any]
    if isinstance(value, dict):
        raw_items = [{"name": key, "semantics": val} for key, val in value.items()]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    attrs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            attr_name = item.strip()
            semantics = ""
        elif isinstance(item, dict):
            attr_name = str(
                item.get("name")
                or item.get("attr")
                or item.get("attribute_name")
                or item.get("field_name")
                or ""
            ).strip()
            semantics = str(
                item.get("semantics")
                or item.get("semantic")
                or item.get("desc")
                or item.get("description")
                or ""
            ).strip()
        else:
            continue

        if not attr_name or attr_name in seen:
            continue
        payload = {"name": attr_name}
        if semantics:
            payload["semantics"] = semantics
        attrs.append(payload)
        seen.add(attr_name)
    return attrs


def normalize_participants(value: Any) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    for item in ensure_dict_list(value):
        entity_name = str(item.get("entity") or item.get("entity_name") or "").strip()
        if not entity_name:
            continue
        payload: dict[str, Any] = {"entity": entity_name}
        role = str(item.get("role") or "").strip()
        if role:
            payload["role"] = role
        anchors = unique_strings(
            item.get("anchor_attribute")
            or item.get("anchor_attributes")
            or item.get("identifier_attrs")
            or item.get("primary_key")
        )
        if anchors:
            payload["anchor_attribute"] = anchors
        participants.append(payload)
    return participants


def normalize_entity(entity: dict[str, Any], index: int) -> dict[str, Any]:
    name = str(entity.get("entity_name") or entity.get("name") or "").strip()
    if not name:
        name = f"entity_{index + 1}"
    attributes = normalize_attribute_list(entity.get("attributes") or entity.get("attrs"))
    primary_key = unique_strings(
        entity.get("primary_key") or entity.get("identifier_attrs")
    )
    return {
        "unit_type": "entity",
        "name": name,
        "desc": str(entity.get("desc") or entity.get("description") or "").strip(),
        "grain": str(entity.get("grain") or "").strip(),
        "attributes": attributes,
        "primary_key": primary_key,
    }


def normalize_relation(relation: dict[str, Any], index: int, *, unit_type: str) -> dict[str, Any]:
    name_key = "relation_name" if unit_type == "relationship" else "connection_name"
    name = str(relation.get(name_key) or relation.get("name") or "").strip()
    if not name:
        name = f"{unit_type}_{index + 1}"
    return {
        "unit_type": unit_type,
        "name": name,
        "desc": str(relation.get("desc") or relation.get("description") or "").strip(),
        "grain": str(relation.get("grain") or "").strip(),
        "participants": normalize_participants(relation.get("participants")),
        "link_condition": str(relation.get("link_condition") or "").strip(),
        "attributes": normalize_attribute_list(relation.get("attributes") or relation.get("attrs")),
    }


def normalize_initial_er(er_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "entities": list(er_payload.get("entities") or []),
        "relations": list(er_payload.get("relations") or []),
        "conditions": list(er_payload.get("conditions") or []),
        "connections": list(er_payload.get("connections") or []),
    }


def build_units(er_payload: dict[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for index, entity in enumerate(ensure_dict_list(er_payload.get("entities"))):
        units.append(normalize_entity(entity, index))
    for index, relation in enumerate(ensure_dict_list(er_payload.get("relations"))):
        units.append(normalize_relation(relation, index, unit_type="relationship"))
    for index, connection in enumerate(ensure_dict_list(er_payload.get("connections"))):
        units.append(normalize_relation(connection, index, unit_type="connection"))
    return units


def get_unit_name(unit: dict[str, Any]) -> str:
    return str(
        unit.get("name")
        or unit.get("unit_name")
        or unit.get("entity_name")
        or unit.get("relation_name")
        or unit.get("connection_name")
        or ""
    ).strip()


def extract_linked_items(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = str(item.get("fullname") or item.get("full_name") or item.get("name") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                output.append(text)
        return output
    return []


def normalize_schema_linking_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "linked_tables" in payload or "linked_columns" in payload or "tables" in payload or "columns" in payload:
        return {
            "__question__": {
                "db_id": str(payload.get("db_id") or "").strip(),
                "question": str(payload.get("question") or "").strip(),
                "linked_tables": extract_linked_items(payload, "linked_tables")
                or extract_linked_items(payload, "tables"),
                "linked_columns": extract_linked_items(payload, "linked_columns")
                or extract_linked_items(payload, "columns"),
                "error": str(payload.get("error") or "").strip(),
            }
        }

    normalized: dict[str, Any] = {}
    for unit_name, unit_payload in payload.items():
        if not isinstance(unit_payload, dict):
            continue
        normalized[str(unit_name)] = {
            "db_id": str(unit_payload.get("db_id") or "").strip(),
            "question": str(unit_payload.get("question") or "").strip(),
            "linked_tables": extract_linked_items(unit_payload, "linked_tables")
            or extract_linked_items(unit_payload, "tables"),
            "linked_columns": extract_linked_items(unit_payload, "linked_columns")
            or extract_linked_items(unit_payload, "columns"),
            "error": str(unit_payload.get("error") or "").strip(),
        }
    return normalized


def split_qualified_name(value: str) -> list[str]:
    return [part.strip().strip('"').strip("`") for part in str(value or "").split(".") if part.strip()]


def table_name_from_fullname(value: str) -> str:
    parts = split_qualified_name(value)
    return parts[-1] if parts else ""


def table_fullname_from_column(value: str) -> str:
    parts = split_qualified_name(value)
    if len(parts) <= 1:
        return ""
    return ".".join(parts[:-1])


def column_name_from_fullname(value: str) -> str:
    parts = split_qualified_name(value)
    return parts[-1] if parts else ""


def collect_linked_schema_names(
    schema_linking_by_unit: dict[str, Any],
    question_schema_linking: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    table_set: set[str] = set()
    column_set: set[str] = set()

    def collect_from_payloads(payloads: dict[str, Any]) -> None:
        for payload in payloads.values():
            if not isinstance(payload, dict):
                continue
            tables = unique_strings(payload.get("linked_tables"))
            columns = unique_strings(payload.get("linked_columns"))
            table_set.update(tables)
            column_set.update(columns)
            for column_fullname in columns:
                table_fullname = table_fullname_from_column(column_fullname)
                if table_fullname:
                    table_set.add(table_fullname)

    collect_from_payloads(schema_linking_by_unit)
    if question_schema_linking:
        collect_from_payloads(normalize_schema_linking_payload(question_schema_linking))

    return sorted(table_set), sorted(column_set)


def resolve_snapshot_search_roots(
    *,
    db_id: str,
    database_root: str | Path | None = None,
    spider2_root: str | Path | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    if database_root is not None:
        root = resolve_path(database_root)
        candidates.extend([root, root / db_id])

    spider_candidates: list[Path] = []
    if spider2_root is not None:
        spider_candidates.append(resolve_path(spider2_root))
    spider_candidates.extend(
        [
            DEFAULT_REFORCE_ROOT / "spider2-snow",
            DEFAULT_SPIDER2_ROOT,
        ]
    )

    for root in spider_candidates:
        candidates.extend(
            [
                root / "resource" / "databases" / db_id,
                root / "databases" / db_id,
            ]
        )

    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = resolve_path(candidate)
        key = str(path).casefold()
        if key in seen or not path.exists():
            continue
        resolved.append(path)
        seen.add(key)
    return resolved


def normalize_fullname(value: str) -> str:
    return ".".join(part.upper() for part in split_qualified_name(value))


def find_snapshot_table_path(search_roots: list[Path], table_fullname: str) -> Path | None:
    table_name = table_name_from_fullname(table_fullname)
    if not table_name:
        return None

    target_fullname = normalize_fullname(table_fullname)
    fallback: Path | None = None
    for root in search_roots:
        for candidate in root.rglob(f"{table_name}.json"):
            if fallback is None:
                fallback = candidate
            try:
                payload = read_json_object(candidate)
            except Exception:
                continue
            snapshot_fullname = normalize_fullname(str(payload.get("table_fullname") or ""))
            if snapshot_fullname and snapshot_fullname == target_fullname:
                return candidate
    return fallback


def load_table_descriptions(search_roots: list[Path]) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for root in search_roots:
        for ddl_path in root.rglob("DDL.csv"):
            try:
                with ddl_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        table_name = str(row.get("table_name") or "").strip()
                        description = str(row.get("description") or "").strip()
                        if table_name and description:
                            descriptions[normalize_key(table_name)] = description
            except Exception:
                continue
    return descriptions


def sample_column_values(
    sample_rows: Any,
    column_name: str,
    *,
    limit: int,
) -> list[Any]:
    if not isinstance(sample_rows, list):
        return []
    values: list[Any] = []
    seen: set[str] = set()
    for row in sample_rows:
        if not isinstance(row, dict) or column_name not in row:
            continue
        value = row.get(column_name)
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        values.append(value)
        seen.add(key)
        if len(values) >= limit:
            break
    return values


def build_table_schema_evidence(
    *,
    table_fullname: str,
    linked_columns: list[str],
    search_roots: list[Path],
    table_descriptions: dict[str, str],
    sample_values_per_column: int,
) -> dict[str, Any]:
    table_name = table_name_from_fullname(table_fullname)
    table_path = find_snapshot_table_path(search_roots, table_fullname)
    if table_path is None:
        return {
            "table_fullname": table_fullname,
            "table_name": table_name,
            "description": "",
            "snapshot_path": "",
            "available_columns": [],
            "linked_columns": [
                {
                    "column_fullname": column_fullname,
                    "column_name": column_name_from_fullname(column_fullname),
                    "data_type": "",
                    "description": "",
                    "sample_values": [],
                }
                for column_fullname in linked_columns
            ],
            "sample_rows": [],
            "warning": "No schema snapshot JSON was found for this linked table.",
        }

    snapshot = read_json_object(table_path)
    column_names = [str(item) for item in list(snapshot.get("column_names") or [])]
    column_types = [str(item or "") for item in list(snapshot.get("column_types") or [])]
    descriptions = list(snapshot.get("description") or [])
    sample_rows = list(snapshot.get("sample_rows") or [])
    column_index = {normalize_key(name): index for index, name in enumerate(column_names)}
    linked_column_names = {
        normalize_key(column_name_from_fullname(column_fullname))
        for column_fullname in linked_columns
    }

    available_columns: list[dict[str, Any]] = []
    for column_name in column_names:
        normalized_column_name = normalize_key(column_name)
        index = column_index.get(normalized_column_name, -1)
        data_type = column_types[index] if 0 <= index < len(column_types) else ""
        description = descriptions[index] if 0 <= index < len(descriptions) else ""
        column_fullname = f"{str(snapshot.get('table_fullname') or table_fullname)}.{column_name}"
        available_columns.append(
            {
                "column_fullname": column_fullname,
                "column_name": column_name,
                "data_type": str(data_type or ""),
                "description": str(description or ""),
                "sample_values": sample_column_values(
                    sample_rows,
                    column_name,
                    limit=sample_values_per_column,
                ),
                "is_linked_column": normalized_column_name in linked_column_names,
            }
        )

    evidence_columns: list[dict[str, Any]] = []
    for column in available_columns:
        if not bool(column.get("is_linked_column")):
            continue
        evidence_columns.append({key: value for key, value in column.items() if key != "is_linked_column"})

    sample_row_projection: list[dict[str, Any]] = []
    for row in sample_rows[:sample_values_per_column]:
        if not isinstance(row, dict):
            continue
        projected_row = {
            column["column_name"]: row.get(column["column_name"])
            for column in evidence_columns
            if column["column_name"] in row
        }
        if projected_row:
            sample_row_projection.append(projected_row)

    description = str(snapshot.get("table_description") or "").strip()
    if not description:
        description = table_descriptions.get(normalize_key(table_name), "")

    return {
        "table_fullname": str(snapshot.get("table_fullname") or table_fullname),
        "table_name": str(snapshot.get("table_name") or table_name),
        "description": description,
        "snapshot_path": str(table_path),
        "available_columns": available_columns,
        "linked_columns": evidence_columns,
        "linked_sample_rows": sample_row_projection,
        "sample_rows": sample_rows[:sample_values_per_column],
    }


def build_schema_evidence(
    *,
    db_id: str,
    schema_linking_by_unit: dict[str, Any],
    question_schema_linking: dict[str, Any] | None = None,
    database_root: str | Path | None = None,
    spider2_root: str | Path | None = None,
    sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
) -> dict[str, Any]:
    linked_tables, linked_columns = collect_linked_schema_names(
        schema_linking_by_unit,
        question_schema_linking,
    )
    linked_columns_by_table: dict[str, list[str]] = {}
    for column_fullname in linked_columns:
        table_fullname = table_fullname_from_column(column_fullname)
        if not table_fullname:
            continue
        linked_columns_by_table.setdefault(table_fullname, []).append(column_fullname)

    search_roots = resolve_snapshot_search_roots(
        db_id=db_id,
        database_root=database_root,
        spider2_root=spider2_root,
    )
    table_descriptions = load_table_descriptions(search_roots)
    tables = [
        build_table_schema_evidence(
            table_fullname=table_fullname,
            linked_columns=linked_columns_by_table.get(table_fullname, []),
            search_roots=search_roots,
            table_descriptions=table_descriptions,
            sample_values_per_column=sample_values_per_column,
        )
        for table_fullname in linked_tables
    ]

    resolved_column_fullnames = {
        normalize_fullname(column["column_fullname"])
        for table in tables
        for column in ensure_dict_list(table.get("linked_columns"))
    }
    unresolved_columns = [
        column_fullname
        for column_fullname in linked_columns
        if normalize_fullname(column_fullname) not in resolved_column_fullnames
    ]

    return {
        "db_id": db_id,
        "evidence_scope": "whole_question_linked_schema",
        "snapshot_search_roots": [str(path) for path in search_roots],
        "table_count": len(tables),
        "linked_column_count": sum(
            len(ensure_dict_list(table.get("linked_columns"))) for table in tables
        ),
        "tables": tables,
        "unresolved_linked_columns": unresolved_columns,
    }


def read_sidecar_context(case_dir: Path) -> dict[str, str]:
    context = {
        "question_id": "",
        "user_intent": "",
        "db_id": "",
        "db_hint": "",
        "external_knowledge": "",
    }
    for filename in ("input.json", "nl2er_input.json", "run_context.json"):
        candidate_path = case_dir / filename
        if not candidate_path.exists():
            continue
        try:
            payload = read_json_object(candidate_path)
        except Exception:
            continue
        if not context["question_id"]:
            context["question_id"] = str(
                payload.get("question_id") or payload.get("instance_id") or ""
            ).strip()
        if not context["user_intent"]:
            context["user_intent"] = str(
                payload.get("user_intent") or payload.get("instruction") or ""
            ).strip()
        if not context["db_id"]:
            context["db_id"] = str(payload.get("db_id") or "").strip()
        if not context["db_hint"]:
            context["db_hint"] = str(payload.get("db_hint") or "").strip()
        if not context["external_knowledge"]:
            external_knowledge = payload.get("external_knowledge")
            if isinstance(external_knowledge, str):
                context["external_knowledge"] = external_knowledge.strip()
    return context


def safe_file_stem(value: str) -> str:
    normalized = re.sub(r"[^\w.-]+", "_", str(value or "").strip(), flags=re.UNICODE)
    normalized = normalized.strip("._")
    return normalized or "unit"


class NL2ERRefine0Runner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        model_config: str | None = None,
        unit_analysis_template_name: str = UNIT_ANALYSIS_TEMPLATE_NAME,
        global_refine_template_name: str = GLOBAL_REFINE_TEMPLATE_NAME,
        database_root: str | Path | None = None,
        spider2_root: str | Path | None = None,
        sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
        dry_run: bool = False,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = resolve_path(log_dir)
        self.model_config = model_config
        self.database_root = database_root
        self.spider2_root = spider2_root
        self.sample_values_per_column = sample_values_per_column
        self.dry_run = dry_run
        self.unit_prompt_dir = self.log_dir / "unit_analysis_prompts"
        self.unit_response_dir = self.log_dir / "unit_analysis_responses"
        self.global_prompt_dir = self.log_dir / "global_refine_prompts"
        self.global_response_dir = self.log_dir / "global_refine_responses"
        for directory in (
            self.log_dir,
            self.unit_prompt_dir,
            self.unit_response_dir,
            self.global_prompt_dir,
            self.global_response_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.llm: LLMClient | None = None
        if not self.dry_run:
            settings = load_settings()
            self.llm = LLMClient(settings.llm.get(model_config))

        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self.prompt_builder.register_template(
            name=UNIT_ANALYSIS_TEMPLATE_KEY,
            template_name=unit_analysis_template_name,
            required_vars=[
                "user_intent",
                "db_id",
                "db_hint",
                "external_knowledge",
                "target_unit",
                "schema_evidence",
            ],
        )
        self.prompt_builder.register_template(
            name=GLOBAL_REFINE_TEMPLATE_KEY,
            template_name=global_refine_template_name,
            required_vars=[
                "user_intent",
                "db_id",
                "db_hint",
                "external_knowledge",
                "initial_er",
                "schema_evidence",
                "unit_analyses",
            ],
        )

    def build_unit_prompt(
        self,
        *,
        context: dict[str, str],
        unit: dict[str, Any],
        schema_evidence: dict[str, Any],
    ) -> str:
        return self.prompt_builder.build_text(
            UNIT_ANALYSIS_TEMPLATE_KEY,
            vars={
                "user_intent": context.get("user_intent", ""),
                "db_id": context.get("db_id", ""),
                "db_hint": context.get("db_hint", ""),
                "external_knowledge": context.get("external_knowledge", ""),
                "target_unit": unit,
                "schema_evidence": schema_evidence,
            },
        )

    @staticmethod
    def build_dry_run_unit_analysis(
        *,
        unit: dict[str, Any],
        schema_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        tables = [
            str(table.get("table_fullname") or table.get("table_name") or "").strip()
            for table in ensure_dict_list(schema_evidence.get("tables"))
            if str(table.get("table_fullname") or table.get("table_name") or "").strip()
        ]
        columns = [
            str(column.get("column_fullname") or column.get("column_name") or "").strip()
            for table in ensure_dict_list(schema_evidence.get("tables"))
            for column in ensure_dict_list(table.get("linked_columns"))
            if str(column.get("column_fullname") or column.get("column_name") or "").strip()
        ]
        support_status = "supported" if columns or tables else "unsupported"
        unit_name = get_unit_name(unit)
        attribute_names = unique_strings(unique_strings(unit.get("primary_key")) + [
            str(attr.get("name") or "").strip()
            for attr in ensure_dict_list(unit.get("attributes"))
            if str(attr.get("name") or "").strip()
        ])
        return {
            "unit_name": unit_name,
            "unit_type": str(unit.get("unit_type") or "").strip(),
            "semantic_requirements": [
                "Dry-run placeholder: inspect the target unit semantics manually."
            ],
            "unit_semantic_support": {
                "support_status": support_status,
                "implemented_semantics": [
                    "Dry-run placeholder: linked schema evidence exists."
                ] if support_status == "supported" else [],
                "missing_semantics": [] if support_status == "supported" else [
                    "Dry-run placeholder: no linked schema evidence was found."
                ],
                "misgrounded_semantics": [],
                "evidence_tables": tables,
                "evidence_columns": columns,
            },
            "attribute_support": [
                {
                    "attribute_name": attr_name,
                    "support_status": support_status,
                    "evidence_columns": columns,
                    "reason": "Dry-run placeholder attribute support.",
                }
                for attr_name in attribute_names
            ],
            "implemented_semantics": [
                "Dry-run placeholder: linked schema evidence exists."
            ] if support_status == "supported" else [],
            "missing_semantics": [] if support_status == "supported" else [
                "Dry-run placeholder: no linked schema evidence was found."
            ],
            "misgrounded_semantics": [],
            "support_status": support_status,
            "recommended_changes": [
                {
                    "action": "keep_unit" if support_status == "supported" else "mark_unimplemented",
                    "target": unit_name,
                    "description": "Dry-run placeholder recommendation.",
                    "evidence_columns": columns,
                }
            ],
            "notes": "Generated without calling the LLM because --dry-run was set.",
        }

    def analyze_units(
        self,
        *,
        context: dict[str, str],
        units: list[dict[str, Any]],
        schema_evidence: dict[str, Any],
        max_concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        prompts: list[str] = []
        unit_payloads: list[dict[str, Any]] = []

        for unit in units:
            unit_type = str(unit.get("unit_type") or "").strip()
            if unit_type not in SUPPORTED_UNIT_TYPES:
                continue
            unit_name = get_unit_name(unit)
            prompt = self.build_unit_prompt(
                context=context,
                unit=unit,
                schema_evidence=schema_evidence,
            )
            prompt_path = self.unit_prompt_dir / f"{safe_file_stem(unit_type + '_' + unit_name)}.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            unit_payloads.append(
                {
                    "unit": unit,
                    "prompt_path": str(prompt_path),
                    "response_path": str(
                        self.unit_response_dir / f"{safe_file_stem(unit_type + '_' + unit_name)}.md"
                    ),
                }
            )
            prompts.append(prompt)

        if self.dry_run:
            return [
                {
                    **self.build_dry_run_unit_analysis(
                        unit=item["unit"],
                        schema_evidence=schema_evidence,
                    ),
                    "analysis_status": "dry_run",
                    "prompt_path": item["prompt_path"],
                    "response_path": item["response_path"],
                }
                for item in unit_payloads
            ]

        if self.llm is None:
            raise RuntimeError("LLM client is not initialized. Disable --dry-run to call the LLM.")

        raw_responses = self.llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(prompts) > 1,
            progress_desc="NL2ER refine unit analysis",
        )

        analyses: list[dict[str, Any]] = []
        for item, raw_response in zip(unit_payloads, raw_responses):
            response_path = Path(item["response_path"])
            response_path.write_text(raw_response or "", encoding="utf-8")
            unit = item["unit"]
            unit_name = get_unit_name(unit)
            unit_type = str(unit.get("unit_type") or "").strip()

            if not raw_response or not raw_response.strip():
                analyses.append(
                    self.build_error_unit_analysis(
                        unit_name=unit_name,
                        unit_type=unit_type,
                        error="LLM returned empty response.",
                        item=item,
                    )
                )
                continue

            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                analyses.append(
                    self.build_error_unit_analysis(
                        unit_name=unit_name,
                        unit_type=unit_type,
                        error=f"Failed to parse unit analysis JSON: {exc}",
                        item=item,
                    )
                )
                continue

            parsed.setdefault("unit_name", unit_name)
            parsed.setdefault("unit_type", unit_type)
            parsed["analysis_status"] = "ok"
            parsed["prompt_path"] = item["prompt_path"]
            parsed["response_path"] = item["response_path"]
            analyses.append(parsed)

        return analyses

    @staticmethod
    def build_error_unit_analysis(
        *,
        unit_name: str,
        unit_type: str,
        error: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "unit_name": unit_name,
            "unit_type": unit_type,
            "semantic_requirements": [],
            "unit_semantic_support": {
                "support_status": "unsupported",
                "implemented_semantics": [],
                "missing_semantics": [error],
                "misgrounded_semantics": [],
                "evidence_tables": [],
                "evidence_columns": [],
            },
            "attribute_support": [],
            "implemented_semantics": [],
            "missing_semantics": [],
            "misgrounded_semantics": [],
            "support_status": "unsupported",
            "recommended_changes": [
                {
                    "action": "mark_unimplemented",
                    "target": unit_name,
                    "description": error,
                    "evidence_columns": [],
                }
            ],
            "notes": error,
            "analysis_status": "error",
            "prompt_path": item.get("prompt_path"),
            "response_path": item.get("response_path"),
        }

    def refine_global(
        self,
        *,
        context: dict[str, str],
        initial_er: dict[str, Any],
        schema_evidence: dict[str, Any],
        unit_analyses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prompt = self.prompt_builder.build_text(
            GLOBAL_REFINE_TEMPLATE_KEY,
            vars={
                "user_intent": context.get("user_intent", ""),
                "db_id": context.get("db_id", ""),
                "db_hint": context.get("db_hint", ""),
                "external_knowledge": context.get("external_knowledge", ""),
                "initial_er": initial_er,
                "schema_evidence": schema_evidence,
                "unit_analyses": unit_analyses,
            },
        )
        prompt_path = self.global_prompt_dir / "global_refine.md"
        response_path = self.global_response_dir / "global_refine.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        if self.dry_run:
            return {
                "refined_er": initial_er,
                "refinement_report": {
                    "global_realizability": "partially_realizable",
                    "kept_units": [
                        str(item.get("unit_name") or "")
                        for item in unit_analyses
                        if str(item.get("support_status") or "") == "supported"
                    ],
                    "rewritten_units": [],
                    "added_units": [],
                    "removed_units": [],
                    "unsupported_semantics": [
                        str(item.get("unit_name") or "")
                        for item in unit_analyses
                        if str(item.get("support_status") or "") != "supported"
                    ],
                    "implementation_notes": [
                        "Dry-run placeholder: global refinement was not generated by an LLM."
                    ],
                },
                "refinement_status": "dry_run",
                "prompt_path": str(prompt_path),
                "response_path": str(response_path),
            }

        if self.llm is None:
            raise RuntimeError("LLM client is not initialized. Disable --dry-run to call the LLM.")

        raw_response = self.llm.single_turn(prompt, check_func=json_check)
        response_path.write_text(raw_response or "", encoding="utf-8")
        if not raw_response or not raw_response.strip():
            return {
                "refined_er": {},
                "refinement_report": {
                    "global_realizability": "not_realizable",
                    "kept_units": [],
                    "rewritten_units": [],
                    "added_units": [],
                    "removed_units": [],
                    "unsupported_semantics": ["Global refinement LLM returned empty response."],
                    "implementation_notes": [],
                },
                "refinement_status": "error",
                "error": "LLM returned empty response.",
                "prompt_path": str(prompt_path),
                "response_path": str(response_path),
            }

        try:
            parsed = json_parse(raw_response)
        except Exception as exc:
            return {
                "refined_er": {},
                "refinement_report": {
                    "global_realizability": "not_realizable",
                    "kept_units": [],
                    "rewritten_units": [],
                    "added_units": [],
                    "removed_units": [],
                    "unsupported_semantics": [
                        "Global refinement response could not be parsed as JSON."
                    ],
                    "implementation_notes": [],
                },
                "refinement_status": "error",
                "error": str(exc),
                "prompt_path": str(prompt_path),
                "response_path": str(response_path),
            }

        parsed["refinement_status"] = "ok"
        parsed["prompt_path"] = str(prompt_path)
        parsed["response_path"] = str(response_path)
        return parsed

    def run_case(
        self,
        *,
        input_path: str | Path,
        schema_linking_path: str | Path,
        output_path: str | Path,
        question_schema_linking_path: str | Path | None = None,
        max_unit_concurrency: int | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        input_path = resolve_path(input_path)
        schema_linking_path = resolve_path(schema_linking_path)
        case_dir = input_path.parent

        er_payload = read_json_object(input_path)
        schema_linking_payload = (
            read_json_object(schema_linking_path) if schema_linking_path.exists() else {}
        )
        question_schema_linking_payload = None
        if question_schema_linking_path is not None:
            resolved_question_schema_linking_path = resolve_path(question_schema_linking_path)
            if resolved_question_schema_linking_path.exists():
                question_schema_linking_payload = read_json_object(
                    resolved_question_schema_linking_path
                )

        context = read_sidecar_context(case_dir)
        if not context["user_intent"]:
            context["user_intent"] = str(
                er_payload.get("user_intent") or er_payload.get("instruction") or ""
            ).strip()
        schema_linking_by_unit = normalize_schema_linking_payload(schema_linking_payload)
        if not context["db_id"]:
            for payload in schema_linking_by_unit.values():
                if isinstance(payload, dict) and str(payload.get("db_id") or "").strip():
                    context["db_id"] = str(payload.get("db_id") or "").strip()
                    break
        if not context["question_id"]:
            context["question_id"] = case_dir.name

        units = build_units(er_payload)
        initial_er = normalize_initial_er(er_payload)
        schema_evidence = build_schema_evidence(
            db_id=context["db_id"],
            schema_linking_by_unit=schema_linking_by_unit,
            question_schema_linking=question_schema_linking_payload,
            database_root=self.database_root,
            spider2_root=self.spider2_root,
            sample_values_per_column=self.sample_values_per_column,
        )

        write_json(self.log_dir / "input_er.json", initial_er)
        write_json(self.log_dir / "schema_linking.json", schema_linking_payload)
        write_json(self.log_dir / "schema_evidence.json", schema_evidence)
        write_json(self.log_dir / "run_context.json", context)

        unit_started_at = time.perf_counter()
        unit_analyses = self.analyze_units(
            context=context,
            units=units,
            schema_evidence=schema_evidence,
            max_concurrency=max_unit_concurrency,
        )
        emit_step_done_log(
            prefix="NL2ER_REFINE_0",
            step="unit_analysis",
            elapsed_seconds=time.perf_counter() - unit_started_at,
            units=len(unit_analyses),
            dry_run=self.dry_run,
        )

        global_started_at = time.perf_counter()
        global_refinement = self.refine_global(
            context=context,
            initial_er=initial_er,
            schema_evidence=schema_evidence,
            unit_analyses=unit_analyses,
        )
        emit_step_done_log(
            prefix="NL2ER_REFINE_0",
            step="global_refine",
            elapsed_seconds=time.perf_counter() - global_started_at,
            status=str(global_refinement.get("refinement_status") or ""),
            dry_run=self.dry_run,
        )

        output_payload = {
            "ok": str(global_refinement.get("refinement_status") or "") in {"ok", "dry_run"},
            "question_id": context.get("question_id", ""),
            "db_id": context.get("db_id", ""),
            "input_path": str(input_path),
            "schema_linking_path": str(schema_linking_path),
            "output_path": str(resolve_path(output_path)),
            "log_dir": str(self.log_dir),
            "context": context,
            "initial_er": initial_er,
            "schema_evidence": schema_evidence,
            "unit_analyses": unit_analyses,
            "global_refinement": global_refinement,
            "elapsed_seconds": time.perf_counter() - started_at,
        }
        write_json(output_path, output_payload)
        write_json(self.log_dir / "output.json", output_payload)
        return output_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Two-stage demo for database-grounded ER refinement. It reads "
            "`nl2er_output.json` and `schema_linking.json`, analyzes implementation "
            "support per ER unit, then performs global ER refinement."
        )
    )
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--model-config", default=None)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--question-schema-linking-path", type=Path, default=None)
    parser.add_argument("--database-root", type=Path, default=None)
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument(
        "--sample-values-per-column",
        type=positive_int,
        default=DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    )
    parser.add_argument("--max-batch-workers", type=positive_int, default=1)
    parser.add_argument("--max-unit-concurrency", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run_single_case(
    *,
    case_input: CaseInput,
    batch_root: Path,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    del batch_root
    case_dir = case_input.case_dir
    output_path = case_dir / args.output_filename
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    context = read_sidecar_context(case_dir)
    question_id = str(context.get("question_id") or "").strip() or case_dir.name

    print(f"[NL2ER_REFINE_0] question_id={question_id}")
    print(f"[NL2ER_REFINE_0] case_dir={case_dir}")
    print(f"[NL2ER_REFINE_0] input_path={case_input.input_path}")
    print(f"[NL2ER_REFINE_0] schema_linking_path={case_input.schema_linking_path}")
    print(f"[NL2ER_REFINE_0] output_path={output_path}")
    print(f"[NL2ER_REFINE_0] log_dir={log_dir}")

    runner = NL2ERRefine0Runner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        model_config=args.model_config,
        database_root=args.database_root,
        spider2_root=args.spider2_root,
        sample_values_per_column=args.sample_values_per_column,
        dry_run=bool(args.dry_run),
    )
    payload = runner.run_case(
        input_path=case_input.input_path,
        schema_linking_path=case_input.schema_linking_path,
        output_path=output_path,
        question_schema_linking_path=args.question_schema_linking_path,
        max_unit_concurrency=args.max_unit_concurrency,
    )
    summary = {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id") or question_id,
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "input_path": str(case_input.input_path),
        "schema_linking_path": str(case_input.schema_linking_path),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "unit_analysis_count": len(payload.get("unit_analyses") or []),
        "global_refinement_status": str(
            (payload.get("global_refinement") or {}).get("refinement_status") or ""
        ),
        "dry_run": bool(args.dry_run),
    }
    write_json(log_dir / "case_summary.json", summary)
    return summary


def run_single_case_safe(
    *,
    case_input: CaseInput,
    batch_root: Path,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_path = case_input.case_dir / args.output_filename
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    try:
        return run_single_case(
            case_input=case_input,
            batch_root=batch_root,
            run_timestamp=run_timestamp,
            args=args,
        )
    except Exception as exc:
        log_dir.mkdir(parents=True, exist_ok=True)
        failure_payload = {
            "ok": False,
            "question_id": case_input.case_dir.name,
            "input_path": str(case_input.input_path),
            "schema_linking_path": str(case_input.schema_linking_path),
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(output_path, failure_payload)
        write_json(log_dir / "error.json", failure_payload)
        return failure_payload


def main() -> None:
    args = parse_args()
    batch_root, case_inputs = discover_case_inputs(args.metadata_dir)
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()

    print(f"[NL2ER_REFINE_0] metadata_dir={resolve_path(args.metadata_dir)}")
    print(f"[NL2ER_REFINE_0] discovered_cases={len(case_inputs)}")
    print(f"[NL2ER_REFINE_0] run_timestamp={run_timestamp}")
    print(f"[NL2ER_REFINE_0] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[NL2ER_REFINE_0] log_root={resolve_path(args.log_root)}")
    print(f"[NL2ER_REFINE_0] dry_run={bool(args.dry_run)}")

    completed_summaries: list[dict[str, Any]] = []
    worker_count = max(1, min(args.max_batch_workers, len(case_inputs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                run_single_case_safe,
                case_input=case_input,
                batch_root=batch_root,
                run_timestamp=run_timestamp,
                args=args,
            ): case_input
            for case_input in case_inputs
        }
        for future in as_completed(future_map):
            summary = future.result()
            completed_summaries.append(summary)
            print(
                "[NL2ER_REFINE_0] progress "
                f"completed={len(completed_summaries)}/{len(case_inputs)} "
                f"question_id={summary.get('question_id', '')} "
                f"ok={summary.get('ok', False)}"
            )

    success_count = sum(1 for item in completed_summaries if item.get("ok"))
    batch_summary = {
        "timestamp": run_timestamp,
        "metadata_dir": str(resolve_path(args.metadata_dir)),
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
        prefix="NL2ER_REFINE_0",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
