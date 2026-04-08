from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.prompt.prompt_builder import PromptBuilder
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


DEFAULT_INPUT_PATH = Path("data/input.json")
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/nl2er")
DEFAULT_METADATA_ROOT = Path("metadata")
DEFAULT_OUTPUT_FILENAME = "nl2er_output.json"


@dataclass(slots=True)
class NL2ERInput:
    question_id: str
    user_intent: str
    db_id: str
    db_hint: str = ""
    external_knowledge: str = ""


def read_input_payload(path: str | Path) -> NL2ERInput:
    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {input_path}, got {type(payload).__name__}")

    question_id = str(payload.get("question_id") or payload.get("instance_id") or "").strip()
    user_intent = str(payload.get("user_intent") or "").strip()
    db_id = str(payload.get("db_id") or "").strip()
    db_hint = payload.get("db_hint", "")
    external_knowledge = payload.get("external_knowledge", "")

    if not user_intent:
        raise ValueError(f"`user_intent` is required in {input_path}")
    if not db_id:
        raise ValueError(f"`db_id` is required in {input_path}")
    if not isinstance(db_hint, str):
        raise ValueError(f"`db_hint` must be a string in {input_path}")
    if not isinstance(external_knowledge, str):
        raise ValueError(f"`external_knowledge` must be a string in {input_path}")

    return NL2ERInput(
        question_id=question_id,
        user_intent=user_intent,
        db_id=db_id,
        db_hint=db_hint,
        external_knowledge=external_knowledge,
    )


def serialize_input_payload(input_payload: NL2ERInput) -> dict[str, str]:
    return {
        "question_id": input_payload.question_id,
        "user_intent": input_payload.user_intent,
        "db_id": input_payload.db_id,
        "db_hint": input_payload.db_hint,
        "external_knowledge": input_payload.external_knowledge,
    }


def build_output_payload(
    *,
    input_payload: NL2ERInput,
    er_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "conceptual_sql": str(er_result.get("conceptual_sql") or "").strip(),
        "entities": list(er_result.get("entities") or []),
        "relations": list(er_result.get("relations") or []),
        "conditions": list(er_result.get("conditions") or []),
    }


class NL2ER:
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

    def extract_erc(self) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Conceptual_st1_v5.5.md" #"NL2ER_ER_st1_v5.2.md"
        self.build_prompt.register_template(
            name="step_1_extract_ERC",
            template_name=template_name,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Extract the conceptual virtual SQL and ER skeleton from the user intent.",
        )
        prompt = self.build_prompt.build_text(
            "step_1_extract_ERC",
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
                "NL2ER LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return self.normalize_output(json_parse(response))

    @staticmethod
    def normalize_conceptual_query_plan(payload: dict[str, Any]) -> dict[str, str]:
        conceptual_sql = str(
            payload.get("virtual_sql") or payload.get("conceptual_sql") or ""
        ).strip()
        if not conceptual_sql:
            raise ValueError("NL2ER conceptual query plan is missing `virtual_sql`.")
        return {"conceptual_sql": conceptual_sql}

    def conceptual_query_plan(self) -> dict[str, str]:
        erc_payload = self.extract_erc()
        return self.normalize_conceptual_query_plan(erc_payload)

    def parse_conceptual_sql(self, conceptual_sql: str) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Parse_st2_v1.0.md"
        self.build_prompt.register_template(
            name="step_2_parse_conceptual_sql",
            template_name=template_name,
            required_vars=["user_intent", "conceptual_sql"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Parse conceptual SQL into the raw ER retrieval spec fields.",
        )
        prompt = self.build_prompt.build_text(
            "step_2_parse_conceptual_sql",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
                "conceptual_sql": conceptual_sql,
            },
        )

        step = 2
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        self._write_text_log(f"response_{step}.md", response)

        if not response.strip():
            raise RuntimeError(
                "NL2ER SQL parse LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return self.normalize_sql_parse_output(json_parse(response))

    @staticmethod
    def normalize_sql_parse_output(payload: dict[str, Any]) -> dict[str, Any]:
        def ensure_list(value: Any) -> list[Any]:
            if isinstance(value, list):
                return list(value)
            return []

        return {
            "entities": ensure_list(payload.get("entities")),
            "relations": ensure_list(payload.get("relations")),
            "conditions": ensure_list(payload.get("conditions")),
        }

    @staticmethod
    def _normalize_named_attr_list(value: Any) -> list[dict[str, str]]:
        normalized_attrs: list[dict[str, str]] = []
        seen_names: set[str] = set()

        if not isinstance(value, list):
            return normalized_attrs

        for item in value:
            attr_name = ""
            if isinstance(item, str):
                attr_name = item.strip()
            elif isinstance(item, dict):
                attr_name = str(item.get("name") or "").strip()

            if not attr_name or attr_name in seen_names:
                continue

            normalized_attrs.append({"name": attr_name})
            seen_names.add(attr_name)

        return normalized_attrs

    @staticmethod
    def _normalize_identifier_attrs(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    @classmethod
    def _normalize_entity_types(cls, value: Any) -> list[dict[str, Any]]:
        normalized_entities: list[dict[str, Any]] = []
        if not isinstance(value, list):
            return normalized_entities

        for entity in value:
            if not isinstance(entity, dict):
                continue

            entity_name = str(entity.get("name") or "").strip()
            if not entity_name:
                continue

            normalized_entity: dict[str, Any] = {
                "name": entity_name,
                "desc": str(entity.get("desc") or "").strip(),
                "grain": str(entity.get("grain") or "").strip(),
                "role": str(entity.get("role") or "").strip(),
                "attrs": cls._normalize_named_attr_list(entity.get("attrs")),
                "identifier_attrs": cls._normalize_identifier_attrs(
                    entity.get("identifier_attrs")
                ),
            }
            normalized_entities.append(normalized_entity)

        return normalized_entities

    @classmethod
    def _normalize_relationship_types(cls, value: Any) -> list[dict[str, Any]]:
        normalized_relationships: list[dict[str, Any]] = []
        if not isinstance(value, list):
            return normalized_relationships

        for relationship in value:
            if not isinstance(relationship, dict):
                continue

            relationship_name = str(relationship.get("name") or "").strip()
            if not relationship_name:
                continue

            raw_participants = relationship.get("participants", [])
            normalized_participants: list[dict[str, Any]] = []
            if isinstance(raw_participants, list):
                for participant in raw_participants:
                    if isinstance(participant, str):
                        entity_name = participant.strip()
                        if not entity_name:
                            continue
                        normalized_participants.append(
                            {
                                "entity": entity_name,
                                "role": "",
                                "cardinality": "unknown",
                                "identifier_attrs": [],
                            }
                        )
                        continue

                    if not isinstance(participant, dict):
                        continue

                    entity_name = str(
                        participant.get("entity")
                        or participant.get("name")
                        or ""
                    ).strip()
                    if not entity_name:
                        continue

                    normalized_participants.append(
                        {
                            "entity": entity_name,
                            "role": str(participant.get("role") or "").strip(),
                            "cardinality": str(participant.get("cardinality") or "unknown").strip()
                            or "unknown",
                            "identifier_attrs": cls._normalize_identifier_attrs(
                                participant.get("identifier_attrs")
                            ),
                        }
                    )

            normalized_relationships.append(
                {
                    "name": relationship_name,
                    "desc": str(relationship.get("desc") or "").strip(),
                    "participants": normalized_participants,
                    "attrs": cls._normalize_named_attr_list(relationship.get("attrs")),
                }
            )

        return normalized_relationships

    @staticmethod
    def _normalize_conditions(value: Any) -> list[dict[str, Any]]:
        normalized_conditions: list[dict[str, Any]] = []
        if not isinstance(value, list):
            return normalized_conditions

        for condition in value:
            if not isinstance(condition, dict):
                continue
            normalized_condition = dict(condition)
            raw_target = normalized_condition.get("target", [])
            if isinstance(raw_target, str):
                normalized_condition["target"] = [raw_target.strip()] if raw_target.strip() else []
            elif isinstance(raw_target, list):
                normalized_condition["target"] = [
                    item.strip()
                    for item in raw_target
                    if isinstance(item, str) and item.strip()
                ]
            else:
                normalized_condition["target"] = []
            normalized_conditions.append(normalized_condition)

        return normalized_conditions

    @classmethod
    def normalize_output(cls, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_payload: dict[str, Any] = dict(payload)
        normalized_payload["entity_types"] = cls._normalize_entity_types(
            payload.get("entity_types")
        )
        normalized_payload["relationship_types"] = cls._normalize_relationship_types(
            payload.get("relationship_types")
        )
        normalized_payload["conditions"] = cls._normalize_conditions(payload.get("conditions"))
        normalized_payload["virtual_sql"] = str(payload.get("virtual_sql") or "").strip()
        normalized_payload.pop("normalized_intent", None)
        normalized_payload.pop("user_original_requirement_coverage", None)
        return normalized_payload

    def run(self) -> dict[str, Any]:
        started_at = time.time()

        step_started_at = time.time()
        conceptual_plan = self.conceptual_query_plan()
        emit_step_done_log(
            prefix="NL2ER",
            step="conceptual_query_plan",
            elapsed_seconds=time.time() - step_started_at,
            has_conceptual_sql=bool(conceptual_plan.get("conceptual_sql")),
        )

        step_started_at = time.time()
        sql_parse_result = self.parse_conceptual_sql(conceptual_plan["conceptual_sql"])
        emit_step_done_log(
            prefix="NL2ER",
            step="parse_conceptual_sql",
            elapsed_seconds=time.time() - step_started_at,
            entity_count=len(sql_parse_result.get("entities") or []),
            relation_count=len(sql_parse_result.get("relations") or []),
            condition_count=len(sql_parse_result.get("conditions") or []),
        )

        elapsed_seconds = time.time() - started_at
        return {
            "result": {
                "conceptual_sql": conceptual_plan["conceptual_sql"],
                "entities": sql_parse_result.get("entities", []),
                "relations": sql_parse_result.get("relations", []),
                "conditions": sql_parse_result.get("conditions", []),
            },
            "elapsed_seconds": elapsed_seconds,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NL2ER from a parameterized JSON input.")
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
    input_payload = read_input_payload(args.input_path)
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

    write_json(metadata_dir / "input.json", serialize_input_payload(input_payload))
    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": input_payload.question_id,
            "db_id": input_payload.db_id,
            "timestamp": run_timestamp,
            "metadata_dir": str(metadata_dir),
            "log_dir": str(log_dir),
            "output_path": str(output_path),
        },
    )
    write_json(
        log_dir / "input.json",
        serialize_input_payload(input_payload),
    )

    print(f"[NL2ER] run_id={run_id}")
    print(f"[NL2ER] question_id={input_payload.question_id}")
    print(f"[NL2ER] db_id={input_payload.db_id}")

    nl2er = NL2ER(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=input_payload,
        model_config=args.model_config,
    )
    payload = nl2er.run()
    write_json(
        output_path,
        build_output_payload(
            input_payload=input_payload,
            er_result=payload["result"],
        ),
    )
    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": input_payload.question_id,
            "db_id": input_payload.db_id,
            "timestamp": run_timestamp,
            "metadata_dir": str(metadata_dir),
            "log_dir": str(log_dir),
            "output_path": str(output_path),
            "elapsed_seconds": payload["elapsed_seconds"],
        },
    )

    print(f"[NL2ER] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
    print(f"[NL2ER] output wrote to {output_path}")
    print(f"[NL2ER] logs wrote to {log_dir}")
    print(f"[NL2ER] metadata wrote to {metadata_dir}")


if __name__ == "__main__":
    main()
