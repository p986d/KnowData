from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_parse
from src.utils.run_log import build_timestamp, resolve_run_dir, write_json


DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/nl2er_synth_eval")

DATA_TEMPLATE_NAME = "NL2ER_Synth_Data_st1_v1.0.md"
SQL_TEMPLATE_NAME = "NL2ER_Synth_SQL_st2_v1.0.md"
JUDGE_TEMPLATE_NAME = "NL2ER_Synth_Judge_st3_v1.0.md"

DATA_TEMPLATE_KEY = "nl2er_synth_data"
SQL_TEMPLATE_KEY = "nl2er_synth_sql"
JUDGE_TEMPLATE_KEY = "nl2er_synth_judge"

SIDECAR_INPUT_FILENAMES = ("input.json", "nl2er_input.json")
SAFE_SQL_SYMBOL_RE = re.compile(r"[^A-Za-z0-9_]+")
SQL_FENCE_RE = re.compile(r"```sql\s*(.*?)\s*```", flags=re.IGNORECASE | re.DOTALL)
CONSTANT_CONDITION_TYPE_RE = re.compile(r"[\s-]+")
QUOTED_LITERAL_RE = re.compile(r"""['"`]([^'"`\r\n]{1,120})['"`]""")
DATE_LITERAL_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NUMBER_LITERAL_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


def read_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {file_path}, got {type(payload).__name__}")
    return payload


def iter_sidecar_payloads(path: str | Path) -> list[tuple[Path, dict[str, Any]]]:
    input_path = Path(path)
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for candidate_name in SIDECAR_INPUT_FILENAMES:
        candidate_path = input_path.with_name(candidate_name)
        if not candidate_path.exists():
            continue
        try:
            payload = read_json(candidate_path)
        except Exception:
            continue
        payloads.append((candidate_path, payload))
    return payloads


def read_sidecar_context(path: str | Path) -> dict[str, str]:
    resolved = {
        "question_id": "",
        "db_id": "",
        "user_intent": "",
        "db_hint": "",
        "external_knowledge": "",
    }
    for _candidate_path, payload in iter_sidecar_payloads(path):
        if not resolved["question_id"]:
            resolved["question_id"] = str(
                payload.get("question_id") or payload.get("instance_id") or ""
            ).strip()
        if not resolved["db_id"]:
            resolved["db_id"] = str(payload.get("db_id") or "").strip()
        if not resolved["user_intent"]:
            resolved["user_intent"] = str(
                payload.get("user_intent") or payload.get("instruction") or ""
            ).strip()
        if not resolved["db_hint"]:
            resolved["db_hint"] = str(payload.get("db_hint") or "").strip()
        if not resolved["external_knowledge"]:
            candidate_knowledge = payload.get("external_knowledge")
            if isinstance(candidate_knowledge, str) and candidate_knowledge.strip():
                resolved["external_knowledge"] = candidate_knowledge.strip()
    return resolved


def ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def normalize_condition_type(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return CONSTANT_CONDITION_TYPE_RE.sub("_", text)


def normalize_constant_value_conditions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("`conditions` must be a list.")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"`conditions[{index}]` must be an object.")
        normalized_type = normalize_condition_type(item.get("condition_type"))
        if normalized_type != "constant_value":
            continue
        condition_name = str(item.get("condition_name") or "").strip()
        if not condition_name:
            continue
        normalized.append(
            {
                "condition_name": condition_name,
                "condition_type": str(item.get("condition_type") or "").strip(),
                "normalized_condition_type": normalized_type,
                "targets": unique_strings(item.get("targets")),
                "description": str(item.get("description") or "").strip(),
            }
        )
    return normalized


def to_safe_sql_symbol(value: str, *, fallback: str) -> str:
    raw_value = str(value or "").strip()
    text = SAFE_SQL_SYMBOL_RE.sub("_", raw_value).strip("_")
    if not text:
        text = fallback
    text = text.lower()
    if text[0].isdigit():
        text = "_" + text
    return text


def claim_unique_symbol(base: str, used: set[str], *, fallback: str) -> str:
    candidate = to_safe_sql_symbol(base, fallback=fallback)
    if candidate not in used:
        used.add(candidate)
        return candidate
    suffix = 2
    while True:
        numbered = f"{candidate}_{suffix}"
        if numbered not in used:
            used.add(numbered)
            return numbered
        suffix += 1


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().casefold()


def extract_constant_condition_literals(conditions: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    literals: list[str] = []
    for condition in conditions:
        text = "\n".join(
            [
                str(condition.get("condition_name") or "").strip(),
                str(condition.get("description") or "").strip(),
            ]
        )
        for pattern in (QUOTED_LITERAL_RE, DATE_LITERAL_RE):
            for match in pattern.findall(text):
                literal = str(match).strip()
                if not literal:
                    continue
                normalized = normalize_scalar(literal)
                if normalized in seen:
                    continue
                seen.add(normalized)
                literals.append(literal)
        if any(token in text for token in ("=", ">", "<")):
            for match in NUMBER_LITERAL_RE.findall(text):
                literal = str(match).strip()
                normalized = normalize_scalar(literal)
                if not literal or normalized in seen:
                    continue
                seen.add(normalized)
                literals.append(literal)
    return literals


def normalize_cell_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported cell value type: {type(value).__name__}")


def infer_sqlite_type(values: list[Any]) -> str:
    kinds: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("integer")
        elif isinstance(value, int):
            kinds.add("integer")
        elif isinstance(value, float):
            kinds.add("real")
        else:
            kinds.add("text")
    if not kinds or kinds == {"integer"}:
        return "INTEGER"
    if kinds <= {"integer", "real"}:
        return "REAL"
    return "TEXT"


def coerce_sqlite_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def extract_sql_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    matches = SQL_FENCE_RE.findall(text)
    candidate = matches[-1] if matches else text
    return candidate.strip()


def build_placeholder_judge(
    *,
    verdict: str,
    can_answer_user_need: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "can_answer_user_need": can_answer_user_need,
        "verdict": verdict,
        "reason": reason,
        "matched_points": "",
        "missing_points": reason,
        "result_semantics": "",
    }


def build_sqlite_schema_from_er(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    entities = ensure_dict_list(payload.get("entities"))
    relations = ensure_dict_list(payload.get("relations"))
    connections = ensure_dict_list(payload.get("connections"))

    warnings: list[str] = []
    errors: list[str] = []
    tables: list[dict[str, Any]] = []

    if not entities and not relations and not connections:
        errors.append("NL2ER output does not contain any entities, relations, or connections.")

    used_table_names: set[str] = set()
    seen_entity_names: set[str] = set()
    entity_columns_by_name: dict[str, set[str]] = {}

    for index, entity in enumerate(entities):
        entity_name = str(entity.get("entity_name") or "").strip()
        if not entity_name:
            errors.append(f"entities[{index}].entity_name is required.")
            continue
        if entity_name in seen_entity_names:
            errors.append(f"Duplicate entity name `{entity_name}` found while building SQLite schema.")
            continue
        seen_entity_names.add(entity_name)

        primary_key = unique_strings(entity.get("primary_key"))
        attributes = unique_strings(entity.get("attributes"))
        merged_columns = list(primary_key)
        for attr_name in attributes:
            if attr_name not in merged_columns:
                merged_columns.append(attr_name)

        if not merged_columns:
            errors.append(f"Entity `{entity_name}` does not expose any columns for synthetic data.")
            continue

        table_name = claim_unique_symbol(
            f"entity_{entity_name}",
            used_table_names,
            fallback=f"entity_{index + 1}",
        )
        used_column_names: set[str] = set()
        columns: list[dict[str, Any]] = []
        primary_key_columns: list[str] = []

        for column_index, semantic_name in enumerate(merged_columns):
            physical_name = claim_unique_symbol(
                semantic_name,
                used_column_names,
                fallback=f"col_{column_index + 1}",
            )
            kind = "identifier" if semantic_name in primary_key else "attribute"
            columns.append(
                {
                    "physical_name": physical_name,
                    "semantic_name": semantic_name,
                    "kind": kind,
                }
            )
            if kind == "identifier":
                primary_key_columns.append(physical_name)

        entity_columns_by_name[entity_name] = {column["semantic_name"] for column in columns}
        tables.append(
            {
                "table_name": table_name,
                "source_type": "entity",
                "source_name": entity_name,
                "columns": columns,
                "primary_key_columns": primary_key_columns,
                "participant_bindings": [],
            }
        )

    def append_link_tables(units: list[dict[str, Any]], *, source_type: str, name_key: str) -> None:
        seen_names: set[str] = set()
        for index, unit in enumerate(units):
            unit_name = str(unit.get(name_key) or "").strip()
            if not unit_name:
                errors.append(f"{source_type}s[{index}].{name_key} is required.")
                continue
            if unit_name in seen_names:
                errors.append(
                    f"Duplicate {source_type} name `{unit_name}` found while building SQLite schema."
                )
                continue
            seen_names.add(unit_name)

            participants = ensure_dict_list(unit.get("participants"))
            attributes = unique_strings(unit.get("attributes"))
            table_name = claim_unique_symbol(
                f"{source_type}_{unit_name}",
                used_table_names,
                fallback=f"{source_type}_{index + 1}",
            )

            used_column_names: set[str] = set()
            columns: list[dict[str, Any]] = []
            participant_bindings: list[dict[str, Any]] = []

            for participant_index, participant in enumerate(participants):
                entity_name = str(participant.get("entity") or "").strip()
                role_name = str(participant.get("role") or "").strip()
                anchor_attributes = unique_strings(participant.get("anchor_attribute"))

                if not entity_name:
                    errors.append(
                        f"{source_type} `{unit_name}` participant[{participant_index}] is missing `entity`."
                    )
                    continue
                if not anchor_attributes:
                    errors.append(
                        f"{source_type} `{unit_name}` participant `{entity_name}` is missing `anchor_attribute`."
                    )
                    continue

                if entity_name not in entity_columns_by_name:
                    errors.append(
                        f"{source_type} `{unit_name}` references unknown entity `{entity_name}`."
                    )

                for anchor_index, anchor_attr in enumerate(anchor_attributes):
                    if (
                        entity_name in entity_columns_by_name
                        and anchor_attr not in entity_columns_by_name[entity_name]
                    ):
                        errors.append(
                            f"{source_type} `{unit_name}` anchor `{anchor_attr}` is not available in entity `{entity_name}`."
                        )
                    prefix_source = role_name or entity_name or f"participant_{participant_index + 1}"
                    physical_name = claim_unique_symbol(
                        f"{prefix_source}_{anchor_attr}",
                        used_column_names,
                        fallback=f"anchor_{participant_index + 1}_{anchor_index + 1}",
                    )
                    columns.append(
                        {
                            "physical_name": physical_name,
                            "semantic_name": anchor_attr,
                            "kind": "participant_anchor",
                            "participant_entity": entity_name,
                            "participant_role": role_name,
                        }
                    )
                    participant_bindings.append(
                        {
                            "column_name": physical_name,
                            "semantic_attr": anchor_attr,
                            "entity_name": entity_name,
                            "role_name": role_name,
                        }
                    )

            for attr_index, semantic_name in enumerate(attributes):
                physical_name = claim_unique_symbol(
                    semantic_name,
                    used_column_names,
                    fallback=f"attr_{attr_index + 1}",
                )
                columns.append(
                    {
                        "physical_name": physical_name,
                        "semantic_name": semantic_name,
                        "kind": "attribute",
                    }
                )

            if not columns:
                errors.append(f"{source_type} `{unit_name}` does not expose any columns for synthetic data.")
                continue

            tables.append(
                {
                    "table_name": table_name,
                    "source_type": source_type,
                    "source_name": unit_name,
                    "columns": columns,
                    "primary_key_columns": [],
                    "participant_bindings": participant_bindings,
                }
            )

    append_link_tables(relations, source_type="relation", name_key="relation_name")
    append_link_tables(connections, source_type="connection", name_key="connection_name")

    return {"tables": tables}, warnings, errors


def validate_generated_tables(
    generated_payload: dict[str, Any],
    schema_payload: dict[str, Any],
    constant_conditions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    schema_tables = ensure_dict_list(schema_payload.get("tables"))
    if not schema_tables:
        return [], [], ["SQLite schema is empty; no synthetic tables can be validated."]

    raw_tables = generated_payload.get("tables")
    if not isinstance(raw_tables, list):
        return [], [], ["Generated synthetic data payload must contain a `tables` list."]

    warnings: list[str] = []
    errors: list[str] = []
    generated_index: dict[str, dict[str, Any]] = {}

    for index, table in enumerate(raw_tables):
        if not isinstance(table, dict):
            errors.append(f"Generated tables[{index}] must be an object.")
            continue
        table_name = str(table.get("table_name") or "").strip()
        if not table_name:
            errors.append(f"Generated tables[{index}].table_name is required.")
            continue
        if table_name in generated_index:
            errors.append(f"Generated table `{table_name}` appears more than once.")
            continue
        generated_index[table_name] = table

    expected_names = {table["table_name"] for table in schema_tables}
    generated_names = set(generated_index)

    for missing_name in sorted(expected_names - generated_names):
        errors.append(f"Expected table `{missing_name}` is missing from generated synthetic data.")
    for unexpected_name in sorted(generated_names - expected_names):
        errors.append(f"Unexpected table `{unexpected_name}` was generated.")

    normalized_tables: list[dict[str, Any]] = []

    for table_spec in schema_tables:
        table_name = table_spec["table_name"]
        generated_table = generated_index.get(table_name)
        if generated_table is None:
            continue

        rows = generated_table.get("rows")
        if not isinstance(rows, list):
            errors.append(f"Generated table `{table_name}` must contain a `rows` list.")
            continue
        if not rows:
            errors.append(f"Generated table `{table_name}` must contain at least 1 row.")
            continue
        if len(rows) > 3:
            errors.append(f"Generated table `{table_name}` exceeds the 3-row limit.")

        allowed_columns = {column["physical_name"] for column in table_spec["columns"]}
        ordered_columns = [column["physical_name"] for column in table_spec["columns"]]

        normalized_rows: list[dict[str, Any]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"Generated row `{table_name}[{row_index}]` must be an object.")
                continue

            unknown_columns = sorted(set(row) - allowed_columns)
            if unknown_columns:
                errors.append(
                    f"Generated row `{table_name}[{row_index}]` uses unknown columns: {', '.join(unknown_columns)}."
                )

            normalized_row: dict[str, Any] = {}
            for column_name in ordered_columns:
                try:
                    normalized_row[column_name] = normalize_cell_value(row.get(column_name))
                except TypeError as exc:
                    errors.append(f"Generated row `{table_name}[{row_index}]`: {exc}")
                    normalized_row[column_name] = None

            for pk_column in table_spec.get("primary_key_columns") or []:
                if is_blank(normalized_row.get(pk_column)):
                    errors.append(
                        f"Generated row `{table_name}[{row_index}]` is missing entity primary key column `{pk_column}`."
                    )

            if table_spec.get("source_type") in {"relation", "connection"}:
                for binding in table_spec.get("participant_bindings") or []:
                    column_name = str(binding.get("column_name") or "")
                    if is_blank(normalized_row.get(column_name)):
                        errors.append(
                            f"Generated row `{table_name}[{row_index}]` is missing participant column `{column_name}`."
                        )

            normalized_rows.append(normalized_row)

        normalized_tables.append(
            {
                **table_spec,
                "rows": normalized_rows,
            }
        )

    entity_value_index: dict[str, dict[str, set[str]]] = {}
    for table in normalized_tables:
        if table.get("source_type") != "entity":
            continue
        entity_name = str(table.get("source_name") or "")
        value_index: dict[str, set[str]] = {}
        for column in table.get("columns") or []:
            semantic_name = str(column.get("semantic_name") or "")
            physical_name = str(column.get("physical_name") or "")
            observed = {
                normalize_scalar(row.get(physical_name))
                for row in table.get("rows") or []
                if not is_blank(row.get(physical_name))
            }
            value_index[semantic_name] = observed
        entity_value_index[entity_name] = value_index

    for table in normalized_tables:
        if table.get("source_type") not in {"relation", "connection"}:
            continue
        table_name = str(table.get("table_name") or "")
        for row_index, row in enumerate(table.get("rows") or []):
            for binding in table.get("participant_bindings") or []:
                entity_name = str(binding.get("entity_name") or "")
                semantic_attr = str(binding.get("semantic_attr") or "")
                column_name = str(binding.get("column_name") or "")
                available_values = entity_value_index.get(entity_name, {}).get(semantic_attr)
                if not available_values:
                    errors.append(
                        f"Synthetic validation cannot resolve entity `{entity_name}` anchor `{semantic_attr}` for table `{table_name}`."
                    )
                    continue
                row_value = normalize_scalar(row.get(column_name))
                if row_value not in available_values:
                    errors.append(
                        f"Generated row `{table_name}[{row_index}]` participant column `{column_name}` does not match any `{entity_name}.{semantic_attr}` value."
                    )

    condition_literals = extract_constant_condition_literals(constant_conditions)
    if condition_literals:
        observed_values = {
            normalize_scalar(value)
            for table in normalized_tables
            for row in table.get("rows") or []
            for value in row.values()
            if not is_blank(value)
        }
        for literal in condition_literals:
            if normalize_scalar(literal) not in observed_values:
                errors.append(
                    f"Constant-value condition literal `{literal}` is not observable in generated synthetic data."
                )

    return normalized_tables, warnings, errors


def load_tables_into_sqlite(
    schema_payload: dict[str, Any],
    validated_tables: list[dict[str, Any]],
) -> tuple[sqlite3.Connection, dict[str, Any]]:
    schema_tables = ensure_dict_list(schema_payload.get("tables"))
    table_index = {table["table_name"]: table for table in validated_tables}

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    sqlite_tables: list[dict[str, Any]] = []
    for table_spec in schema_tables:
        table_name = table_spec["table_name"]
        validated_table = table_index.get(table_name)
        if validated_table is None:
            raise ValueError(f"Validated synthetic table not found: {table_name}")

        column_specs = []
        column_definitions: list[str] = []
        ordered_columns: list[str] = []
        rows = validated_table.get("rows") or []
        for column in table_spec.get("columns") or []:
            physical_name = str(column.get("physical_name") or "").strip()
            values = [row.get(physical_name) for row in rows]
            sqlite_type = infer_sqlite_type(values)
            column_specs.append(
                {
                    **column,
                    "sqlite_type": sqlite_type,
                }
            )
            ordered_columns.append(physical_name)
            column_definitions.append(f"{quote_identifier(physical_name)} {sqlite_type}")

        ddl = f"CREATE TABLE {quote_identifier(table_name)} ({', '.join(column_definitions)})"
        connection.execute(ddl)

        if rows:
            placeholders = ", ".join("?" for _ in ordered_columns)
            insert_sql = (
                f"INSERT INTO {quote_identifier(table_name)} "
                f"({', '.join(quote_identifier(column_name) for column_name in ordered_columns)}) "
                f"VALUES ({placeholders})"
            )
            for row in rows:
                ordered_values = [coerce_sqlite_value(row.get(column_name)) for column_name in ordered_columns]
                connection.execute(insert_sql, ordered_values)

        sqlite_tables.append(
            {
                "table_name": table_name,
                "source_type": table_spec.get("source_type"),
                "source_name": table_spec.get("source_name"),
                "columns": column_specs,
                "ddl": ddl,
            }
        )

    connection.commit()
    return connection, {"tables": sqlite_tables}


def execute_sqlite_query(connection: sqlite3.Connection, sql: str) -> dict[str, Any]:
    stripped_sql = extract_sql_text(sql)
    if not stripped_sql:
        return {
            "sql": "",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": "SQL is empty.",
        }

    statement = stripped_sql.rstrip(";").strip()
    if not statement:
        return {
            "sql": "",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": "SQL is empty after trimming trailing semicolons.",
        }
    if ";" in statement:
        return {
            "sql": statement,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": "Only one SQLite SELECT statement is allowed.",
        }
    if not re.match(r"(?is)^(select|with)\b", statement):
        return {
            "sql": statement,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": "Only SQLite SELECT statements are allowed.",
        }

    try:
        cursor = connection.execute(statement)
        columns = [description[0] for description in cursor.description or []]
        rows = [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]
        return {
            "sql": statement,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "error": "",
        }
    except Exception as exc:
        return {
            "sql": statement,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "error": str(exc),
        }


class NL2ERSyntheticEvaluator:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        model_config: str | None = None,
        max_retry: int = 1,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_retry = max(0, int(max_retry))

        settings = load_settings()
        self.model_config_name = model_config or settings.llm.default_model
        self.llm = LLMClient(settings.llm.get(self.model_config_name))
        self.prompt_builder = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=True,
        )
        self.prompt_builder.register_template(
            name=DATA_TEMPLATE_KEY,
            template_name=DATA_TEMPLATE_NAME,
            required_vars=[
                "user_intent",
                "entities",
                "relations",
                "connections",
                "constant_conditions",
                "sqlite_schema",
            ],
            default_vars={
                "external_knowledge": "",
                "previous_response": "",
                "validation_errors": [],
            },
            description="Generate a compact synthetic SQLite witness dataset from NL2ER output.",
        )
        self.prompt_builder.register_template(
            name=SQL_TEMPLATE_KEY,
            template_name=SQL_TEMPLATE_NAME,
            required_vars=[
                "user_intent",
                "normalized_er",
                "constant_conditions",
                "sqlite_schema",
                "synthetic_tables",
            ],
            default_vars={
                "external_knowledge": "",
                "previous_sql": "",
                "execution_error": "",
            },
            description="Generate one SQLite SQL query against the synthetic witness dataset.",
        )
        self.prompt_builder.register_template(
            name=JUDGE_TEMPLATE_KEY,
            template_name=JUDGE_TEMPLATE_NAME,
            required_vars=[
                "user_intent",
                "normalized_er",
                "constant_conditions",
                "sqlite_schema",
                "synthetic_tables",
                "sql_payload",
                "execution_result",
            ],
            default_vars={
                "external_knowledge": "",
            },
            description="Judge whether the NL2ER structure can answer the user need on witness data.",
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    def _write_json_log(self, filename: str, payload: dict[str, Any]) -> None:
        write_json(self.log_dir / filename, payload)

    def _finalize_report(self, report: dict[str, Any]) -> dict[str, Any]:
        self._write_json_log("final_report.json", report)
        return report

    @staticmethod
    def _parse_data_generation_payload(raw_response: str) -> dict[str, Any]:
        payload = json_parse(raw_response)
        tables = payload.get("tables")
        if not isinstance(tables, list):
            raise ValueError("Data-generation payload must contain a `tables` list.")
        return payload

    @staticmethod
    def _parse_sql_generation_payload(raw_response: str) -> dict[str, Any]:
        payload = json_parse(raw_response)
        sql = extract_sql_text(str(payload.get("sql") or ""))
        if not sql:
            raise ValueError("SQL-generation payload is missing `sql`.")
        reason = str(payload.get("reason") or "").strip()
        return {
            "sql": sql,
            "reason": reason,
        }

    @staticmethod
    def _parse_judge_payload(raw_response: str) -> dict[str, Any]:
        payload = json_parse(raw_response)
        verdict = str(payload.get("verdict") or "").strip().casefold()
        if verdict not in {"pass", "partial", "fail"}:
            raise ValueError("Judge payload field `verdict` must be one of: pass, partial, fail.")
        can_answer_user_need = payload.get("can_answer_user_need")
        if not isinstance(can_answer_user_need, bool):
            raise ValueError("Judge payload field `can_answer_user_need` must be a bool.")
        return {
            "can_answer_user_need": can_answer_user_need,
            "verdict": verdict,
            "reason": str(payload.get("reason") or "").strip(),
            "matched_points": str(payload.get("matched_points") or "").strip(),
            "missing_points": str(payload.get("missing_points") or "").strip(),
            "result_semantics": str(payload.get("result_semantics") or "").strip(),
        }

    def _run_data_generation(
        self,
        *,
        user_intent: str,
        external_knowledge: str,
        normalized_er: dict[str, Any],
        constant_conditions: list[dict[str, Any]],
        sqlite_schema: dict[str, Any],
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        previous_response = ""
        validation_errors: list[str] = []

        for attempt in range(1, self.max_retry + 2):
            prompt = self.prompt_builder.build_text(
                DATA_TEMPLATE_KEY,
                vars={
                    "user_intent": user_intent,
                    "external_knowledge": external_knowledge,
                    "entities": normalized_er["entities"],
                    "relations": normalized_er["relations"],
                    "connections": normalized_er["connections"],
                    "constant_conditions": constant_conditions,
                    "sqlite_schema": sqlite_schema,
                    "previous_response": previous_response,
                    "validation_errors": validation_errors,
                },
            )
            self._write_text_log(f"data_gen_prompt_attempt_{attempt}.md", prompt)
            self._write_text_log("data_gen_prompt.md", prompt)

            raw_response = self.llm.single_turn(prompt)
            self._write_text_log(f"data_gen_response_attempt_{attempt}.md", raw_response or "")
            self._write_text_log("data_gen_response.md", raw_response or "")

            attempt_payload: dict[str, Any] = {
                "attempt": attempt,
                "raw_response": raw_response or "",
            }
            try:
                parsed_payload = self._parse_data_generation_payload(raw_response or "")
                validated_tables, validation_warnings, validation_errors = validate_generated_tables(
                    parsed_payload,
                    sqlite_schema,
                    constant_conditions,
                )
                attempt_payload["validation_warnings"] = validation_warnings
                attempt_payload["validation_errors"] = validation_errors
                if not validation_errors:
                    attempt_payload["ok"] = True
                    attempts.append(attempt_payload)
                    return validated_tables, attempts
            except Exception as exc:
                validation_errors = [str(exc)]
                attempt_payload["validation_errors"] = validation_errors

            attempt_payload["ok"] = False
            attempts.append(attempt_payload)
            previous_response = raw_response or ""

        return None, attempts

    def _run_sql_generation(
        self,
        *,
        user_intent: str,
        external_knowledge: str,
        normalized_er: dict[str, Any],
        constant_conditions: list[dict[str, Any]],
        sqlite_schema: dict[str, Any],
        synthetic_tables: list[dict[str, Any]],
        connection: sqlite3.Connection,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        previous_sql = ""
        execution_error = ""

        for attempt in range(1, self.max_retry + 2):
            prompt = self.prompt_builder.build_text(
                SQL_TEMPLATE_KEY,
                vars={
                    "user_intent": user_intent,
                    "external_knowledge": external_knowledge,
                    "normalized_er": normalized_er,
                    "constant_conditions": constant_conditions,
                    "sqlite_schema": sqlite_schema,
                    "synthetic_tables": synthetic_tables,
                    "previous_sql": previous_sql,
                    "execution_error": execution_error,
                },
            )
            self._write_text_log(f"sql_prompt_attempt_{attempt}.md", prompt)
            self._write_text_log("sql_prompt.md", prompt)

            raw_response = self.llm.single_turn(prompt)
            self._write_text_log(f"sql_response_attempt_{attempt}.md", raw_response or "")
            self._write_text_log("sql_response.md", raw_response or "")

            attempt_payload: dict[str, Any] = {
                "attempt": attempt,
                "raw_response": raw_response or "",
            }
            try:
                sql_payload = self._parse_sql_generation_payload(raw_response or "")
            except Exception as exc:
                execution_error = str(exc)
                attempt_payload["ok"] = False
                attempt_payload["error"] = execution_error
                attempts.append(attempt_payload)
                continue

            execution_result = execute_sqlite_query(connection, sql_payload["sql"])
            attempt_payload["sql"] = sql_payload["sql"]
            attempt_payload["reason"] = sql_payload["reason"]
            attempt_payload["execution_result"] = execution_result
            attempt_payload["ok"] = not bool(execution_result.get("error"))
            attempts.append(attempt_payload)

            if not execution_result.get("error"):
                return sql_payload, execution_result, attempts

            previous_sql = sql_payload["sql"]
            execution_error = str(execution_result.get("error") or "").strip()

        return None, None, attempts

    def _run_judge(
        self,
        *,
        user_intent: str,
        external_knowledge: str,
        normalized_er: dict[str, Any],
        constant_conditions: list[dict[str, Any]],
        sqlite_schema: dict[str, Any],
        synthetic_tables: list[dict[str, Any]],
        sql_payload: dict[str, Any],
        execution_result: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.max_retry + 2):
            prompt = self.prompt_builder.build_text(
                JUDGE_TEMPLATE_KEY,
                vars={
                    "user_intent": user_intent,
                    "external_knowledge": external_knowledge,
                    "normalized_er": normalized_er,
                    "constant_conditions": constant_conditions,
                    "sqlite_schema": sqlite_schema,
                    "synthetic_tables": synthetic_tables,
                    "sql_payload": sql_payload,
                    "execution_result": execution_result,
                },
            )
            self._write_text_log(f"judge_prompt_attempt_{attempt}.md", prompt)
            self._write_text_log("judge_prompt.md", prompt)

            raw_response = self.llm.single_turn(prompt)
            self._write_text_log(f"judge_response_attempt_{attempt}.md", raw_response or "")
            self._write_text_log("judge_response.md", raw_response or "")

            attempt_payload: dict[str, Any] = {
                "attempt": attempt,
                "raw_response": raw_response or "",
            }
            try:
                judge_payload = self._parse_judge_payload(raw_response or "")
                attempt_payload["ok"] = True
                attempts.append(attempt_payload)
                return judge_payload, attempts
            except Exception as exc:
                attempt_payload["ok"] = False
                attempt_payload["error"] = str(exc)
                attempts.append(attempt_payload)

        return None, attempts

    def run(
        self,
        *,
        input_path: str | Path,
        user_intent: str | None = None,
        question_id: str | None = None,
        external_knowledge: str = "",
    ) -> dict[str, Any]:
        resolved_input_path = Path(input_path)
        er_payload = read_json(resolved_input_path)
        sidecar_context = read_sidecar_context(resolved_input_path)

        resolved_question_id = str(question_id or sidecar_context.get("question_id") or "").strip()
        resolved_user_intent = str(
            user_intent or sidecar_context.get("user_intent") or er_payload.get("user_intent") or ""
        ).strip()
        resolved_external_knowledge = str(
            external_knowledge
            or sidecar_context.get("external_knowledge")
            or er_payload.get("external_knowledge")
            or ""
        ).strip()

        if not resolved_user_intent:
            raise ValueError(
                "`user_intent` is required. Provide it explicitly or place it in a sidecar input JSON."
            )

        all_conditions = er_payload.get("conditions")
        constant_conditions = normalize_constant_value_conditions(all_conditions)
        ignored_condition_count = (
            len(all_conditions) - len(constant_conditions) if isinstance(all_conditions, list) else 0
        )

        warnings: list[str] = []
        errors: list[str] = []
        if ignored_condition_count > 0:
            warnings.append(
                f"Ignored {ignored_condition_count} non-constant-value conditions during synthetic evaluation."
            )

        integrity_report = er_payload.get("integrity_report")
        if isinstance(integrity_report, dict) and not bool(integrity_report.get("passed")):
            warnings.append(
                "NL2ER integrity_report.passed is false; synthetic evaluation may be unstable."
            )
            for item in integrity_report.get("errors") or []:
                text = str(item).strip()
                if text:
                    warnings.append(f"integrity_report error: {text}")

        normalized_er = {
            "entities": ensure_dict_list(er_payload.get("entities")),
            "relations": ensure_dict_list(er_payload.get("relations")),
            "connections": ensure_dict_list(er_payload.get("connections")),
            "conditions": constant_conditions,
            "integrity_report": integrity_report if isinstance(integrity_report, dict) else {},
        }

        sqlite_schema, schema_warnings, schema_errors = build_sqlite_schema_from_er(er_payload)
        warnings.extend(schema_warnings)
        errors.extend(schema_errors)

        report: dict[str, Any] = {
            "status": "inconclusive",
            "question_id": resolved_question_id,
            "user_intent": resolved_user_intent,
            "input_path": str(resolved_input_path),
            "model_config": self.model_config_name,
            "normalized_er": normalized_er,
            "constant_value_conditions": constant_conditions,
            "synthetic_tables": [],
            "sqlite_schema": sqlite_schema,
            "sql_attempts": [],
            "final_sql": "",
            "execution_result": {
                "sql": "",
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": "",
            },
            "judge_result": build_placeholder_judge(
                verdict="inconclusive",
                can_answer_user_need=False,
                reason="Evaluation did not reach the judgement stage.",
            ),
            "warnings": warnings,
            "errors": errors,
        }

        self._write_json_log("sqlite_schema.json", sqlite_schema)

        if errors:
            report["judge_result"] = build_placeholder_judge(
                verdict="inconclusive",
                can_answer_user_need=False,
                reason="SQLite schema could not be built from the NL2ER output.",
            )
            return self._finalize_report(report)

        synthetic_tables, data_attempts = self._run_data_generation(
            user_intent=resolved_user_intent,
            external_knowledge=resolved_external_knowledge,
            normalized_er=normalized_er,
            constant_conditions=constant_conditions,
            sqlite_schema=sqlite_schema,
        )
        report["data_generation_attempts"] = data_attempts
        if synthetic_tables is None:
            report["errors"].append("Synthetic data generation failed after all retries.")
            report["judge_result"] = build_placeholder_judge(
                verdict="inconclusive",
                can_answer_user_need=False,
                reason="Synthetic data generation failed.",
            )
            return self._finalize_report(report)

        report["synthetic_tables"] = synthetic_tables
        self._write_json_log("synthetic_tables.json", {"tables": synthetic_tables})

        connection: sqlite3.Connection | None = None
        try:
            connection, realized_sqlite_schema = load_tables_into_sqlite(sqlite_schema, synthetic_tables)
        except Exception as exc:
            report["errors"].append(f"Failed to build the SQLite witness database: {exc}")
            report["judge_result"] = build_placeholder_judge(
                verdict="inconclusive",
                can_answer_user_need=False,
                reason="SQLite witness database creation failed.",
            )
            return self._finalize_report(report)

        report["sqlite_schema"] = realized_sqlite_schema
        self._write_json_log("sqlite_schema.json", realized_sqlite_schema)

        try:
            sql_payload, execution_result, sql_attempts = self._run_sql_generation(
                user_intent=resolved_user_intent,
                external_knowledge=resolved_external_knowledge,
                normalized_er=normalized_er,
                constant_conditions=constant_conditions,
                sqlite_schema=realized_sqlite_schema,
                synthetic_tables=synthetic_tables,
                connection=connection,
            )
            report["sql_attempts"] = sql_attempts
            if sql_payload is None or execution_result is None:
                report["errors"].append("SQL generation or SQL execution failed after all retries.")
                report["judge_result"] = build_placeholder_judge(
                    verdict="inconclusive",
                    can_answer_user_need=False,
                    reason="SQL generation or SQL execution failed.",
                )
                return self._finalize_report(report)

            report["final_sql"] = sql_payload["sql"]
            report["execution_result"] = execution_result
            self._write_json_log("execution_result.json", execution_result)

            judge_result, judge_attempts = self._run_judge(
                user_intent=resolved_user_intent,
                external_knowledge=resolved_external_knowledge,
                normalized_er=normalized_er,
                constant_conditions=constant_conditions,
                sqlite_schema=realized_sqlite_schema,
                synthetic_tables=synthetic_tables,
                sql_payload=sql_payload,
                execution_result=execution_result,
            )
            report["judge_attempts"] = judge_attempts
            if judge_result is None:
                report["errors"].append("Judge stage failed after all retries.")
                report["judge_result"] = build_placeholder_judge(
                    verdict="inconclusive",
                    can_answer_user_need=False,
                    reason="Judge stage failed.",
                )
                return self._finalize_report(report)

            report["judge_result"] = judge_result
            report["status"] = str(judge_result["verdict"])
            return self._finalize_report(report)
        finally:
            if connection is not None:
                connection.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run NL2ER synthetic evaluation from one nl2er_output.json file."
    )
    parser.add_argument(
        "input_path",
        help="Path to nl2er_output.json",
    )
    parser.add_argument(
        "--user-intent",
        dest="user_intent",
        default=None,
        help="Override user intent. If omitted, read from sidecar JSON or nl2er_output.json.",
    )
    parser.add_argument(
        "--question-id",
        dest="question_id",
        default=None,
        help="Override question_id. If omitted, read from sidecar JSON.",
    )
    parser.add_argument(
        "--external-knowledge",
        dest="external_knowledge",
        default="",
        help="Optional external knowledge text.",
    )
    parser.add_argument(
        "--prompt-dir",
        dest="prompt_dir",
        default=str(DEFAULT_PROMPT_DIR),
        help=f"Prompt template directory. Default: {DEFAULT_PROMPT_DIR}",
    )
    parser.add_argument(
        "--log-root",
        dest="log_root",
        default=str(DEFAULT_LOG_ROOT),
        help=f"Root directory for evaluation logs. Default: {DEFAULT_LOG_ROOT}",
    )
    parser.add_argument(
        "--run-name",
        dest="run_name",
        default="",
        help="Optional custom run directory name under log-root.",
    )
    parser.add_argument(
        "--model-config",
        dest="model_config",
        default=None,
        help="LLM model config name. If omitted, use settings.llm.default_model.",
    )
    parser.add_argument(
        "--max-retry",
        dest="max_retry",
        type=int,
        default=1,
        help="Retry times for each stage. Default: 1.",
    )
    parser.add_argument(
        "--print-report",
        dest="print_report",
        action="store_true",
        help="Print final report JSON to stdout.",
    )
    return parser


def resolve_cli_log_dir(
    *,
    log_root: str | Path,
    input_path: str | Path,
    question_id: str | None = None,
    run_name: str = "",
) -> Path:
    input_file = Path(input_path)
    ts = build_timestamp()

    base_name = (question_id or input_file.stem or "run").strip()
    safe_base_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._-") or "run"

    if run_name.strip():
        safe_run_name = re.sub(r"[^A-Za-z0-9._-]+", "_", run_name.strip()).strip("._-") or ts
        final_name = safe_run_name
    else:
        final_name = f"{safe_base_name}_{ts}"

    log_dir = Path(log_root) / final_name
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    input_path = Path(args.input_path).expanduser()
    if not input_path.exists():
        parser.error(f"Input file does not exist: {input_path}")

    if input_path.is_dir():
        parser.error(f"Input path must be a JSON file, got directory: {input_path}")

    try:
        sidecar_context = read_sidecar_context(input_path)
        resolved_question_id = str(
            args.question_id or sidecar_context.get("question_id") or ""
        ).strip()

        log_dir = resolve_cli_log_dir(
            log_root=args.log_root,
            input_path=input_path,
            question_id=resolved_question_id,
            run_name=args.run_name,
        )

        evaluator = NL2ERSyntheticEvaluator(
            prompt_dir=args.prompt_dir,
            log_dir=log_dir,
            model_config=args.model_config,
            max_retry=args.max_retry,
        )

        report = evaluator.run(
            input_path=input_path,
            user_intent=args.user_intent,
            question_id=args.question_id,
            external_knowledge=args.external_knowledge,
        )

        final_report_path = log_dir / "final_report.json"
        print(f"[nl2er_synth_eval] done")
        print(f"[nl2er_synth_eval] input       : {input_path}")
        print(f"[nl2er_synth_eval] log_dir     : {log_dir}")
        print(f"[nl2er_synth_eval] final_report: {final_report_path}")
        print(f"[nl2er_synth_eval] status      : {report.get('status', '')}")

        if args.print_report:
            print(json.dumps(report, ensure_ascii=False, indent=2))

        return 0

    except KeyboardInterrupt:
        print("[nl2er_synth_eval] interrupted by user")
        return 130
    except Exception as exc:
        print(f"[nl2er_synth_eval] failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())